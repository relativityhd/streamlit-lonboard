"""Convert lonboard layers into a single binary payload for the frontend.

Design (see IMPLEMENTATION_PLAN.md §2):
- Per-feature accessors (colors, radii, ...) live as columns in the Arrow
  table; scalar props go into a plain JSON dict.
- Only the geometry column(s) and accessor columns are shipped — attribute
  data never round-trips (see `StLonboardResult.clicked["index"]`, which lets
  callers look up the row in their own GeoDataFrame).
- `st.components.v2.component`'s automatic dataframe-in-dict serialization
  only fires one level deep and hands the frontend a Table parsed with
  Streamlit's *own* bundled apache-arrow build, which may not match the
  version `@geoarrow/deck.gl-geoarrow` expects. To keep full control over the
  Arrow version used for parsing, we instead ship everything as a single
  top-level `bytes` payload (the one shape CCv2 forwards verbatim) with a
  small custom framing: a 4-byte little-endian header length, a UTF-8 JSON
  header, then the concatenated per-layer Arrow IPC streams.
"""

from __future__ import annotations

import gzip
import io
import json
import struct
import warnings
from dataclasses import dataclass
from typing import Any, Literal
from weakref import WeakKeyDictionary, WeakSet

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.ipc
import pyarrow.parquet as pq
import traitlets

from ._perf import log, span

_EXTENSION_NAME_KEY = b"ARROW:extension:name"

# Bump when the header/body framing changes shape (not for additive,
# backwards-compatible fields). frontend/src/container.ts rejects a mismatch
# loudly instead of mis-parsing.
PAYLOAD_FORMAT_VERSION = 1

Compression = Literal["auto", "gzip", "zstd", "parquet"] | None

# How one layer's table is encoded into its byte range of the payload body:
# - "ipc": plain Arrow IPC stream (compression None, or "gzip", which gzips
#   the *whole body* after concatenation rather than the streams).
# - "ipc-zstd": Arrow IPC stream with ZSTD-compressed record-batch body
#   buffers (the IPC spec's own per-buffer compression) - decompressed
#   transparently inside the frontend's `tableFromIPC` via a registered codec.
# - "parquet": a complete Parquet file (ZSTD data pages), decoded in the
#   frontend by parquet-wasm back into Arrow.
BodyEncoding = Literal["ipc", "ipc-zstd", "parquet"]

# What `compression=` asks for, before per-layer resolution. "auto" is not a
# wire encoding: it picks between "ipc" and "parquet" per layer, once that
# layer's table size is known (see `resolve_body_encoding`).
RequestedBodyEncoding = Literal["ipc", "ipc-zstd", "parquet", "auto"]


def body_encoding_for(compression: Compression) -> RequestedBodyEncoding:
    """Map the user-facing `compression=` option to a requested body encoding."""
    if compression == "zstd":
        return "ipc-zstd"
    if compression == "parquet":
        return "parquet"
    if compression == "auto":
        return "auto"
    if compression in (None, "gzip"):
        return "ipc"
    raise ValueError(
        f"st_lonboard: compression={compression!r} is not supported; use 'auto', 'gzip', 'zstd', 'parquet', or None"
    )


def resolve_body_encoding(requested: RequestedBodyEncoding, table: pa.Table) -> BodyEncoding:
    """Resolve a requested encoding against one layer's finished table.

    Only "auto" depends on the table: it ships Parquet once the layer is big
    enough for the compression to pay for itself, and plain IPC below that.
    `table.nbytes` (the in-memory footprint, a close proxy for the IPC stream
    size) is used rather than encoding twice to measure.
    """
    if requested != "auto":
        return requested
    return "parquet" if table.nbytes >= AUTO_COMPRESSION_THRESHOLD else "ipc"


# `st_lonboard(tooltip=...)`: `True` ships every available non-geometry,
# non-accessor column; a tuple of names ships only those (best-effort - a
# name absent from this particular layer's table is silently skipped, since
# one `tooltip=` request is shared across every layer in the call and
# different layers commonly have different schemas); `False` ships none.
TooltipSpec = bool | tuple[str, ...]

# Per layer, "auto" ships Parquet only once that layer's in-memory table
# reaches this many bytes; below it the layer goes out as plain, near-zero-
# copy Arrow IPC. Two costs justify a floor rather than always compressing:
# the encode/decode CPU (pure overhead on localhost, where transfer is
# free - see IMPLEMENTATION_PLAN.md §2 and Phase 4d), and parquet-wasm's
# one-time ~1.8MB (gzipped) WASM download, which an app whose layers all
# stay under this threshold never pays at all. Chosen so the 10k-point
# baseline (~233KB, see benchmarks/RESULTS.md) stays uncompressed by default
# while 100k+ (~2.3MB+) does not.
AUTO_COMPRESSION_THRESHOLD = 1_000_000

# lonboard `_layer_type` -> frontend GeoArrow*Layer tag (see frontend/src/layers.ts)
SUPPORTED_LAYER_TYPES = {
    "scatterplot": "scatterplot",
    "path": "path",
    "polygon": "polygon",
    "solid-polygon": "solid-polygon",
    "heatmap": "heatmap",
    "h3-hexagon": "h3-hexagon",
    "s2": "s2",
    "a5": "a5",
    "geohash": "geohash",
    "arc": "arc",
    "column": "column",
    "point-cloud": "point-cloud",
    "trip": "trip",
}

# Layer types whose geometry lives entirely in accessor columns (DGGS cell
# IDs, source/target point pairs, ...) rather than a GeoArrow-encoded column
# of `layer.table` - these are exempt from the "no geometry column" check in
# `serialize_layer`.
ACCESSOR_GEOMETRY_LAYER_TYPES = {"h3-hexagon", "s2", "a5", "geohash", "arc"}

# For accessor-geometry layer types, the accessor(s) that must arrive as a
# per-row Arrow array (not a JSON scalar) since they carry the geometry.
_REQUIRED_ACCESSORS: dict[str, tuple[str, ...]] = {
    "h3-hexagon": ("get_hexagon",),
    "s2": ("get_s2_token",),
    "a5": ("get_pentagon",),
    "geohash": ("get_geohash",),
    "arc": ("get_source_position", "get_target_position"),
}

# lonboard `BaseExtension._extension_type` values this frontend knows how to
# instantiate (see frontend/src/extensions.ts). The wire "type" IS the
# extension_type string - no renaming table needed, unlike layers.
SUPPORTED_EXTENSION_TYPES = {"brushing", "collision-filter", "data-filter", "path-style"}

# lonboard `BaseControl._control_type` -> that control's own trait names (see
# frontend/src/index.ts for the matching maplibregl.*Control registry).
# `GeocoderControl` is deliberately absent: it requires a Python-side async
# `client` callback wired over lonboard's ipywidgets comm channel, which has
# no Streamlit equivalent - `serialize_controls` skips it with a warning.
_CONTROL_OWN_TRAITS: dict[str, tuple[str, ...]] = {
    "scale": ("max_width", "unit"),
    "navigation": ("show_compass", "show_zoom", "visualize_pitch", "visualize_roll"),
    "fullscreen": (),
}

# deck.gl prop names that don't follow the generic snake_case -> camelCase
# rule (see `_snake_to_camel`) - e.g. a leading underscore trait.
_PROP_NAME_OVERRIDES = {"_current_time": "currentTime"}

# deck.gl attributes must fit in float32. lonboard's own ipywidgets path
# (lonboard._serialization.serialize_timestamp_accessor) rescales trip
# timestamps by this offset before casting to float32; we mirror that here
# so `GeoArrowTripsLayer` receives the same values it expects from lonboard
# proper. -16777216 is -(2**24), the largest magnitude a float32 can
# represent exactly as an integer.
_TRIP_TIMESTAMP_OFFSET_BASE = -16777216

# Traits that are never forwarded as generic props (internal/comm/ipywidgets
# plumbing, or - like `extensions` - handled by their own dedicated
# serialization path instead of the generic one below).
_IGNORED_TRAITS = {
    "table",
    "_layer_type",
    "comm",
    "log",
    "keys",
    "extensions",  # see `serialize_extensions`
    "before_id",
    "selected_index",
    "_model_module",
    "_model_module_version",
    "_model_name",
    "_view_module",
    "_view_module_version",
    "_view_name",
    "_msg_callbacks",
    "_property_lock",
    "_states_to_send",
    "_view_count",
}


def _snake_to_camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


def _is_arrow_like(value: Any) -> bool:
    """True if `value` is an arro3/pyarrow array-like exportable via the Arrow C interface."""
    return hasattr(value, "__arrow_c_stream__") or hasattr(value, "__arrow_c_array__")


def _is_json_safe(value: Any) -> bool:
    if value is None or isinstance(value, (bool, int, float, str)):
        return True
    if isinstance(value, (list, tuple)):
        return all(_is_json_safe(v) for v in value)
    return False


def _is_json_safe_value(value: Any) -> bool:
    """Like `_is_json_safe`, but also allows string-keyed dicts.

    `_is_json_safe` deliberately excludes dicts: it validates a single deck.gl
    *prop leaf* (a scalar or array), and no layer/extension prop in this
    codebase is ever shaped like a raw JS object. `parameters=` (deck.gl GPU
    parameters) is the exception - it's inherently a free-form nested
    structure (e.g. `{"blendFunc": [...], "depthTest": False}`), so it needs
    the more general JSON-value check.
    """
    if isinstance(value, dict):
        return all(isinstance(k, str) and _is_json_safe_value(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return all(_is_json_safe_value(v) for v in value)
    return value is None or isinstance(value, (bool, int, float, str))


def check_parameters_json_safe(parameters: Any) -> None:
    """Raise a clear error if `parameters` (deck.gl GPU parameters, forwarded
    to the frontend as-is) can't survive a JSON round-trip.

    Unlike layer/extension traits, `parameters` isn't traitlets-validated on
    `lonboard.Map` (it's a bare `t.Any`), so an unsupported value (e.g. a
    numpy array, or one of the `GL.*` constants lonboard's own docstring
    warns aren't real JSON) would otherwise only surface much later as a
    cryptic error from `json.dumps` inside `pack_payload`.
    """
    if parameters is not None and not _is_json_safe_value(parameters):
        raise ValueError(
            "st_lonboard: `parameters` must be JSON-safe (a nested dict/list/tuple of "
            "None, bool, int, float, or str) - got a value that isn't. If you're using "
            "one of luma.gl's `GL.*` constants, pass the underlying integer instead (see "
            f"the `parameters` docstring on `lonboard.Map`). Got: {parameters!r}"
        )


def _geometry_column_names(schema: pa.Schema) -> list[str]:
    names = []
    for field in schema:
        ext_name = (field.metadata or {}).get(_EXTENSION_NAME_KEY)
        if ext_name and ext_name.startswith(b"geoarrow."):
            names.append(field.name)
    return names


@dataclass
class SerializedLayer:
    """One lonboard layer, ready to be packed into the wire payload.

    `body_bytes` is precomputed (not derived from `table` on demand) so that
    caching a `SerializedLayer` (see `serialize_layer_cached`) also caches the
    Arrow IPC / Parquet write, not just the table-building step.
    """

    layer_id: str
    layer_type: str
    props: dict[str, Any]
    extensions: list[dict[str, Any]]
    # The raw `tooltip=` request this was serialized with (for
    # `serialize_layer_cached`'s cache-hit comparison - cheap, no `layer.table`
    # access needed) and what it actually resolved to *for this layer*
    # (shipped on the wire - see `_resolve_tooltip_columns`).
    tooltip_request: TooltipSpec
    tooltip_columns: tuple[str, ...]
    table: pa.Table
    # What `compression=` asked for (the cache-hit comparison in
    # `serialize_layer_cached`, since changing `compression=` between reruns
    # must re-encode even when the table itself didn't change) and what that
    # resolved to for *this* layer - they differ only for "auto", which is
    # decided per layer against `table.nbytes` (see `resolve_body_encoding`).
    encoding_request: RequestedBodyEncoding
    encoding: BodyEncoding
    body_bytes: bytes


def build_layer_props(layer: Any) -> tuple[dict[str, Any], dict[str, pa.ChunkedArray]]:
    """Split a lonboard layer's traits into JSON-safe scalar props and Arrow accessor columns.

    Returns `(props, accessor_columns)` where `props` uses deck.gl's camelCase
    naming, and accessor columns still use the original snake_case trait name
    (matched via the `{"@@arrowColumn": name}` marker in `props`).
    """
    props: dict[str, Any] = {}
    accessor_columns: dict[str, pa.ChunkedArray] = {}

    trait_names = set(layer.trait_names()) - _IGNORED_TRAITS
    for name in sorted(trait_names):
        value = getattr(layer, name, None)
        if value is None:
            continue

        prop_name = _PROP_NAME_OVERRIDES.get(name) or _snake_to_camel(name)
        if name.startswith("get_") and _is_arrow_like(value):
            accessor_columns[name] = pa.chunked_array(value)
            props[prop_name] = {"@@arrowColumn": name}
        elif _is_json_safe(value):
            props[prop_name] = value

    return props, accessor_columns


def serialize_extensions(layer: Any) -> list[dict[str, Any]]:
    """Serialize a layer's `extensions` tuple into wire-format extension headers.

    Each entry is `{"type": <lonboard _extension_type>, "props": {...}}`, where
    `props` holds only the extension object's *own* config traits (e.g.
    `PathStyleExtension.dash`, `DataFilterExtension.filter_size`), camelCased
    like any other prop.

    This deliberately does *not* handle the layer-side traits an extension
    injects (e.g. `get_dash_array`, `filter_range`) - by the time
    `BaseLayer.__init__` returns, those are ordinary traits *on the layer*
    (lonboard's `_add_extension_traits` calls `HasTraits.add_traits(self,
    **extension._layer_traits)`), so `build_layer_props()` already picks them
    up with no changes needed here.
    """
    extensions: list[dict[str, Any]] = []
    for ext in getattr(layer, "extensions", ()) or ():
        extension_type = ext._extension_type
        if extension_type not in SUPPORTED_EXTENSION_TYPES:
            raise ValueError(
                f"streamlit-lonboard does not yet support the {extension_type!r} layer "
                f"extension. Supported extensions: {sorted(SUPPORTED_EXTENSION_TYPES)}"
            )

        # `_layer_traits` names never actually appear in `ext.trait_names()`
        # (they're injected onto the *layer*, not the extension) - excluded
        # here anyway as a defensive mirror of lonboard's own layer/extension
        # trait split, in case a future lonboard version changes that.
        own_trait_names = set(ext.trait_names()) - _IGNORED_TRAITS - {"_extension_type"}
        own_trait_names -= set(type(ext)._layer_traits)

        props: dict[str, Any] = {}
        for name in sorted(own_trait_names):
            value = getattr(ext, name, None)
            if value is None:
                continue
            if _is_json_safe(value):
                props[_PROP_NAME_OVERRIDES.get(name) or _snake_to_camel(name)] = value

        extensions.append({"type": extension_type, "props": props})

    return extensions


def serialize_controls(controls: Any) -> list[dict[str, Any]]:
    """Serialize a `lonboard.Map.controls` tuple into wire-format control headers.

    Each entry is `{"type": <lonboard _control_type>, "position": ..., "options": {...}}`.
    An unrecognized control type (currently just `GeocoderControl` - see
    `_CONTROL_OWN_TRAITS`) is skipped with a warning rather than raised as an
    error: unlike an unsupported layer or extension, a control is decorative
    (zoom buttons, a scale bar, ...) and the map is still fully usable without
    it, so failing the whole render over one skipped control would be worse
    than just not drawing it.
    """
    serialized: list[dict[str, Any]] = []
    for ctrl in controls or ():
        control_type = ctrl._control_type
        own_trait_names = _CONTROL_OWN_TRAITS.get(control_type)
        if own_trait_names is None:
            warnings.warn(
                f"st_lonboard: skipping unsupported map control {control_type!r} - "
                "only 'scale', 'navigation', and 'fullscreen' controls are supported",
                stacklevel=2,
            )
            continue

        options: dict[str, Any] = {}
        for name in own_trait_names:
            value = getattr(ctrl, name, None)
            if value is not None:
                options[_snake_to_camel(name)] = value

        serialized.append({"type": control_type, "position": ctrl.position, "options": options})

    return serialized


def _resolve_tooltip_columns(table: pa.Table, tooltip: TooltipSpec, *, used_names: set[str]) -> tuple[str, ...]:
    """Pick which columns of `table` (the layer's full, pre-geometry-selection
    data table) to ship as hover-tooltip content, per `tooltip` (see
    `TooltipSpec`). `used_names` (the geometry + accessor column names already
    going into the output table) is excluded from consideration - re-shipping
    the geometry column as "tooltip data" is redundant, and an attribute
    column that happens to share a name with an accessor column would collide
    when appended to the output table.
    """
    if tooltip is False:
        return ()

    available = [name for name in table.schema.names if name not in used_names]
    if tooltip is True:
        return tuple(sorted(available))

    available_set = set(available)
    # dict.fromkeys: preserves the caller's requested order while silently
    # collapsing an accidental duplicate name (a second `append_column` with
    # the same name would otherwise produce an invalid/ambiguous table).
    return tuple(dict.fromkeys(name for name in tooltip if name in available_set))


def _canonicalize_table(table: pa.Table) -> pa.Table:
    """Force deterministic IPC bytes for identical content.

    arro3 builds field extension metadata (GeoArrow CRS JSON etc.) from a Rust
    HashMap, whose iteration order is randomized per construction - so the
    *same* GeoDataFrame serialized twice can get `{name, metadata}` vs.
    `{metadata, name}` key order, which changes the Arrow IPC FlatBuffers byte
    layout even though the content is identical. That breaks every
    content-hash cache downstream (ours and Streamlit's own ForwardMsgCache),
    so we sort each field's metadata keys here. Also drops schema-level
    metadata (e.g. the "pandas" blob from geopandas) - we never read it and
    it's pure payload bloat.
    """
    fields = []
    for field in table.schema:
        if field.metadata:
            sorted_metadata = dict(sorted(field.metadata.items()))
            field = field.with_metadata(sorted_metadata)
        fields.append(field)
    schema = pa.schema(fields)
    return pa.Table.from_arrays(table.columns, schema=schema)


def _table_to_ipc_bytes(table: pa.Table, *, buffer_compression: str | None = None) -> bytes:
    """Write `table` as a single-record-batch Arrow IPC stream.

    `buffer_compression="zstd"` enables the IPC spec's per-buffer body
    compression (each record-batch buffer compressed independently, with an
    8-byte uncompressed-length prefix). pyarrow's C++ writer always compresses
    every buffer when this is set (it has no "store uncompressed if bigger"
    fallback unless `min_space_savings` is configured, which we don't) - which
    matters because the frontend's registered decode codec only handles the
    compressed case (see frontend/src/container.ts).

    `combine_chunks()` first: lonboard auto-rechunks large tables (via
    arro3's Rust rechunk), which produces zero-copy slices - each chunk after
    the first is a variable-size-list array (Polygon/MultiPolygon/Path
    geometry, or any list-offset column) whose offsets are absolute into a
    since-truncated child array. pyarrow's C++ IPC writer doesn't always
    rebase those offsets when writing a sliced chunk, producing a stream
    that's invalid on read-back (`batch.validate(full=True)` fails) - this is
    an upstream bug (https://github.com/apache/arrow/issues/46407), not
    something arro3 or our own table-building does wrong. The frontend has no
    way to detect the corruption; it just throws inside deck.gl's assert()
    and silently renders nothing. `combine_chunks()` forces a single,
    freshly-compacted chunk per column with correctly-rebased offsets,
    sidestepping the bug regardless of which pyarrow version is installed
    (fixed-size geometry, e.g. Point, was never affected - no offsets buffer
    to corrupt in the first place).
    """
    table = table.combine_chunks()
    sink = io.BytesIO()
    options = pa.ipc.IpcWriteOptions(compression=buffer_compression)
    with pa.ipc.new_stream(sink, table.schema, options=options) as writer:
        writer.write_table(table)
    return sink.getvalue()


def _table_to_parquet_bytes(table: pa.Table) -> bytes:
    """Write `table` as a complete in-memory Parquet file (ZSTD data pages).

    pyarrow stores the full Arrow schema (including the GeoArrow extension
    field metadata the frontend's layer builders key on) in the Parquet
    footer's `ARROW:schema` key by default (`store_schema=True`), and
    parquet-wasm's Rust reader reconstructs it faithfully - verified against
    a `geoarrow.point` column with CRS metadata.

    `combine_chunks()` for the same sliced-offsets reason as
    `_table_to_ipc_bytes` (the Parquet writer goes through the same C++
    write path that mis-handles lazily-sliced list offsets).

    Encoding choice (measured in benchmarks/wire_options.py and
    wire_options_real.py, see benchmarks/RESULTS.md): per leaf column,
    - *flat and point-like* numeric leaves (max_repetition_level <= 1:
      scalar stats/accessors, point coordinates, fixed-size color lists)
      get BYTE_STREAM_SPLIT, which regroups each value's bytes by
      significance before compression - the only encoding here that
      shrinks high-entropy float coordinate buffers (~21% under raw IPC on
      the synthetic scatter benchmark, where dictionary+plain-ZSTD Parquet
      came out *larger* than raw IPC);
    - *deeply-nested* numeric leaves (max_repetition_level > 1: Path/
      Polygon/MultiPolygon vertex coordinates) get plain+ZSTD instead.
      Real boundary geometry repeats vertices across neighboring features,
      and ZSTD exploits those byte-sequence repeats directly - BSS's byte
      transposition destroys them (measured on real A5 pentagon polygons:
      BSS 3.10MB vs. plain 1.79MB vs. 4.37MB raw at L07);
    - everything else (string/binary tooltip columns) keeps dictionary
      encoding (BSS and dictionary are mutually exclusive per column).
    """
    table = table.combine_chunks()
    bss_leaves, dict_leaves = _split_parquet_leaf_paths(table.schema)
    sink = io.BytesIO()
    pq.write_table(
        table,
        sink,
        compression="zstd",
        # Leaves in neither list are written plain (no dictionary, no BSS).
        use_dictionary=dict_leaves,
        use_byte_stream_split=bss_leaves,
    )
    return sink.getvalue()


def _split_parquet_leaf_paths(schema: pa.Schema) -> tuple[list[str], list[str]]:
    """Partition a schema's Parquet leaf-column paths into (BSS leaves,
    dictionary leaves) per the policy documented in `_table_to_parquet_bytes`;
    numeric leaves nested deeper than one repetition level land in neither
    list and are written plain.

    Path strings must match what the Parquet writer expects for nested
    columns (e.g. a GeoArrow point column becomes "geometry.list.element"),
    so they're read back from an actual (zero-row, in-memory) Parquet write
    of the schema rather than reconstructed by hand from the Arrow schema -
    which also provides each leaf's max_repetition_level directly.
    """
    sink = io.BytesIO()
    pq.write_table(schema.empty_table(), sink)
    parquet_schema = pq.ParquetFile(pa.BufferReader(sink.getvalue())).schema
    bss: list[str] = []
    dictionary: list[str] = []
    for i in range(len(parquet_schema)):
        column = parquet_schema.column(i)
        # The physical types BYTE_STREAM_SPLIT supports, minus
        # FIXED_LEN_BYTE_ARRAY (half-precision floats etc. - none of our
        # columns produce it, and BSS gains there are unproven).
        if column.physical_type in ("FLOAT", "DOUBLE", "INT32", "INT64"):
            if column.max_repetition_level <= 1:
                bss.append(column.path)
            # else: deeply-nested coordinates, written plain - see docstring.
        else:
            dictionary.append(column.path)
    return bss, dictionary


def _encode_table(table: pa.Table, encoding: BodyEncoding) -> bytes:
    if encoding == "ipc":
        return _table_to_ipc_bytes(table)
    if encoding == "ipc-zstd":
        return _table_to_ipc_bytes(table, buffer_compression="zstd")
    if encoding == "parquet":
        return _table_to_parquet_bytes(table)
    raise ValueError(f"unknown body encoding {encoding!r}")


def _rescale_trip_timestamps(chunked: pa.ChunkedArray) -> pa.ChunkedArray:
    """Rescale a trip layer's `get_timestamps` column to `list<float32>`.

    deck.gl uploads path/trip vertex attributes as float32, so timestamps
    (typically microsecond-precision, far outside float32's exact-integer
    range) must first be shifted down into that range - mirroring lonboard's
    own `serialize_timestamp_accessor` (see `_TRIP_TIMESTAMP_OFFSET_BASE`).
    """
    arr = chunked.combine_chunks()  # ChunkedArray.combine_chunks() -> a single Array
    flat = pc.cast(arr.flatten(), pa.int64())
    min_value = pc.min(flat).as_py() if len(flat) else 0
    offset = _TRIP_TIMESTAMP_OFFSET_BASE - min_value
    shifted = pc.cast(pc.add(flat, pa.scalar(offset, pa.int64())), pa.float32())
    rescaled = pa.ListArray.from_arrays(arr.offsets, shifted)
    return pa.chunked_array([rescaled])


def serialize_layer(
    layer: Any, layer_id: str, tooltip: TooltipSpec = False, encoding: RequestedBodyEncoding = "auto"
) -> SerializedLayer:
    """Serialize a single lonboard layer into scalar props + a minimal Arrow table.

    Not cached - see `serialize_layer_cached` for the memoized entry point
    `st_lonboard()` actually uses.
    """
    with span(f"serialize_layer[{layer_id}]"):
        layer_type = SUPPORTED_LAYER_TYPES.get(layer._layer_type)
        if layer_type is None:
            raise ValueError(
                f"streamlit-lonboard does not yet support layer type "
                f"{layer._layer_type!r} (layer {layer_id}). Supported types: "
                f"{sorted(SUPPORTED_LAYER_TYPES)}"
            )

        table = pa.table(layer.table)
        props, accessor_columns = build_layer_props(layer)
        extensions = serialize_extensions(layer)

        keep_columns = _geometry_column_names(table.schema)
        if not keep_columns and layer._layer_type not in ACCESSOR_GEOMETRY_LAYER_TYPES:
            raise ValueError(f"layer {layer_id} has no GeoArrow-encoded geometry column")

        for accessor_name in _REQUIRED_ACCESSORS.get(layer._layer_type, ()):
            if accessor_name not in accessor_columns:
                raise ValueError(
                    f"layer {layer_id}: `{accessor_name}` must be set to an array with "
                    f"one value per row (got a scalar) - streamlit-lonboard cannot "
                    f"render a {layer._layer_type!r} layer without it"
                )

        if layer._layer_type == "trip" and "get_timestamps" in accessor_columns:
            accessor_columns["get_timestamps"] = _rescale_trip_timestamps(accessor_columns["get_timestamps"])

        out_table = table.select(keep_columns)
        for name, chunked in accessor_columns.items():
            out_table = out_table.append_column(name, chunked)

        tooltip_columns = _resolve_tooltip_columns(table, tooltip, used_names=set(keep_columns) | set(accessor_columns))
        for name in tooltip_columns:
            out_table = out_table.append_column(name, table.column(name))

        out_table = _canonicalize_table(out_table)
        resolved_encoding = resolve_body_encoding(encoding, out_table)
        body_bytes = _encode_table(out_table, resolved_encoding)

        return SerializedLayer(
            layer_id=layer_id,
            layer_type=layer_type,
            props=props,
            extensions=extensions,
            tooltip_request=tooltip,
            tooltip_columns=tooltip_columns,
            table=out_table,
            encoding_request=encoding,
            encoding=resolved_encoding,
            body_bytes=body_bytes,
        )


# WeakKeyDictionary so cache entries disappear once the user's layer object
# does (no manual cache size management needed). Keyed on the layer object
# itself: two lonboard Layer instances are never "the same" just because
# their content matches, so this only ever helps when the *same* object is
# passed again - e.g. built once under `st.cache_resource` (see README /
# examples/app.py). See IMPLEMENTATION_PLAN.md Phase 4a.
_layer_cache: "WeakKeyDictionary[Any, SerializedLayer]" = WeakKeyDictionary()
# Tracks which layer objects already have our invalidation observer attached,
# so we don't re-attach it (and re-evict) on every cache hit.
_observed_layers: "WeakKeyDictionary[Any, None]" = WeakKeyDictionary()
# Same, but for extension objects (`ext.dash = False` etc. must also evict
# the owning layer's cache entry - the layer-level observer above only fires
# for traits *on the layer itself*, not on a separate extension object it
# merely references).
_observed_extensions: "WeakKeyDictionary[Any, None]" = WeakKeyDictionary()
# Which layer(s) currently reference a given extension object, so its
# trait-change observer knows which cache entries to evict. A `WeakSet` of
# layers (not a plain set) so this mapping doesn't itself keep layers alive -
# an extension is, in principle, shareable across more than one layer.
_extension_owners: "WeakKeyDictionary[Any, WeakSet]" = WeakKeyDictionary()


def _evict_cache_on_trait_change(change: dict[str, Any]) -> None:
    _layer_cache.pop(change["owner"], None)


def _evict_cache_for_extension_trait_change(change: dict[str, Any]) -> None:
    for layer in _extension_owners.get(change["owner"], ()):
        _layer_cache.pop(layer, None)


def serialize_layer_cached(
    layer: Any, layer_id: str, tooltip: TooltipSpec = False, encoding: RequestedBodyEncoding = "auto"
) -> SerializedLayer:
    """Like `serialize_layer`, but memoized on layer object identity.

    A cache hit skips table-building *and* the Arrow IPC/Parquet write
    entirely. Invalidated automatically if any trait on the layer changes
    (lonboard layers are traitlets `HasTraits` instances), if any trait on one
    of its `extensions` changes, if `tooltip` or `encoding` differs from what
    this layer was last serialized with (plain equality checks against the
    request - like `layer_id`, these need no `layer.table` access, so a cache
    *hit* never pays for them), or if the same object is reused at a different
    `layer_id` (e.g. the app reordered its layers list).
    """
    cached = _layer_cache.get(layer)
    if (
        cached is not None
        and cached.layer_id == layer_id
        and cached.tooltip_request == tooltip
        and cached.encoding_request == encoding
    ):
        log("serialize_layer_cached[%s]: hit", layer_id)
        return cached

    serialized = serialize_layer(layer, layer_id, tooltip, encoding)
    _layer_cache[layer] = serialized
    if layer not in _observed_layers:
        layer.observe(_evict_cache_on_trait_change, names=traitlets.All)
        _observed_layers[layer] = None

    for ext in getattr(layer, "extensions", ()) or ():
        owners = _extension_owners.setdefault(ext, WeakSet())
        owners.add(layer)
        if ext not in _observed_extensions:
            ext.observe(_evict_cache_for_extension_trait_change, names=traitlets.All)
            _observed_extensions[ext] = None

    return serialized


def _compress_body(body: bytes, compression: Compression) -> tuple[bytes, str | None]:
    """Returns `(possibly-compressed body, "gzip" or None)`.

    `byteOffset`/`byteLength` in the layer headers are computed against the
    *uncompressed* body before this runs, so the frontend must decompress the
    whole body before slicing per-layer ranges out of it (see container.ts).
    """
    if compression in (None, "auto", "zstd", "parquet"):
        # Every mode except "gzip" compresses inside each layer's own body
        # bytes (see `BodyEncoding`) - no whole-body pass on top. "auto"
        # included: it now reaches for Parquet per layer rather than gzipping
        # the concatenated body (see `resolve_body_encoding`).
        return body, None
    if compression != "gzip":
        raise ValueError(
            f"st_lonboard: compression={compression!r} is not supported; use 'auto', 'gzip', 'zstd', 'parquet', or None"
        )

    with span("pack_payload.compress"):
        compressed = gzip.compress(body, compresslevel=6)

    if len(compressed) >= len(body):
        # Already-compact/incompressible data (e.g. tiny or high-entropy
        # payloads) - compressing would only add decode latency for nothing.
        return body, None
    return compressed, "gzip"


def pack_payload(
    serialized_layers: list[SerializedLayer],
    *,
    view_state: dict[str, Any] | None,
    map_options: dict[str, Any],
    compression: Compression = "auto",
) -> bytes:
    """Pack layers + view state + map options into a single framed `bytes` blob.

    Layout: `<u32 header_len little-endian><utf-8 json header><concatenated
    per-layer body byte ranges (Arrow IPC streams or Parquet files, per that
    layer's header["bodyEncoding"]), gzip-compressed as a whole if
    header["compression"] == "gzip">`.

    `bodyEncoding` is per *layer* rather than per payload because "auto"
    resolves against each layer's own table size, so one map can legitimately
    mix a Parquet-encoded 2M-row layer with a plain-IPC 500-row one.
    """
    requested_encoding = body_encoding_for(compression)
    with span("pack_payload"):
        body = io.BytesIO()
        layer_headers = []
        for layer in serialized_layers:
            if layer.encoding_request != requested_encoding:
                # A stale-cache/programming error, not a user error: component.py
                # derives both from the same `compression=` argument.
                raise ValueError(
                    f"pack_payload: layer {layer.layer_id!r} was serialized for "
                    f"encoding={layer.encoding_request!r} but compression={compression!r} "
                    f"requires {requested_encoding!r}"
                )
            body_bytes = layer.body_bytes
            layer_header = {
                "id": layer.layer_id,
                "type": layer.layer_type,
                "props": layer.props,
                "byteOffset": body.tell(),
                "byteLength": len(body_bytes),
            }
            # Omitted for plain IPC (the pre-existing encoding) so payloads
            # from that path stay byte-identical with older frontend builds.
            if layer.encoding != "ipc":
                layer_header["bodyEncoding"] = layer.encoding
            if layer.extensions:
                layer_header["extensions"] = layer.extensions
            if layer.tooltip_columns:
                layer_header["tooltipColumns"] = layer.tooltip_columns
            layer_headers.append(layer_header)
            body.write(body_bytes)

        raw_body = body.getvalue()
        compressed_body, used_compression = _compress_body(raw_body, compression)

        header = {
            "v": PAYLOAD_FORMAT_VERSION,
            "layers": layer_headers,
            "viewState": view_state,
            "mapOptions": map_options,
            "compression": used_compression,
        }
        try:
            header_json = json.dumps(header, allow_nan=False).encode("utf-8")
        except ValueError as e:
            # Python's json.dumps otherwise happily emits bare `NaN`/`Infinity`
            # tokens, which are invalid JSON and crash the frontend's
            # `JSON.parse` with a cryptic error, taking down the whole
            # component. The usual cause: a layer's auto-computed view state
            # (or a prop derived from data) is NaN because the layer's
            # geometry column contains null/missing entries - lonboard's own
            # centroid/bbox math propagates NaN through nulls rather than
            # skipping them.
            raise ValueError(
                "st_lonboard: payload contains a non-finite number (NaN/Infinity), which "
                "is not valid JSON. This usually happens when a layer's auto-computed view "
                "state is derived from data with null/missing geometries. Fix: pass an "
                "explicit `view_state=` to st_lonboard(), or drop null geometries from your "
                "data before building the layer."
            ) from e

        payload = struct.pack("<I", len(header_json)) + header_json + compressed_body

    log(
        "pack_payload: %d bytes total (%s), %d layers",
        len(payload),
        used_compression or "uncompressed",
        len(serialized_layers),
    )

    return payload


def check_payload_size(payload: bytes, *, max_mb: float) -> None:
    """Fail fast with an actionable message instead of letting Streamlit
    silently substitute a generic "message too large" error for the whole
    component once the payload hits the WebSocket layer (`server.maxMessageSize`,
    default 200MB - see `streamlit.runtime.runtime_util.MessageSizeError`).

    `max_mb` is passed in (rather than read here via `st.get_option`) so this
    stays testable without a running Streamlit app.
    """
    max_bytes = int(max_mb * 1_000_000)
    if len(payload) > max_bytes:
        raise ValueError(
            f"st_lonboard: serialized payload is {len(payload) / 1e6:.1f}MB, which exceeds "
            f"Streamlit's server.maxMessageSize limit of {max_mb}MB. To fix this:\n"
            "  - Downsample your data (fewer rows, coarser geometry) - usually the right fix "
            "for an interactive app.\n"
            "  - Try compression='zstd' or 'parquet' (see benchmarks/RESULTS.md for when "
            "each actually helps - none is always a win).\n"
            "  - Raise the limit by setting `server.maxMessageSize` in .streamlit/config.toml "
            "(increases memory/latency for every client, so prefer downsampling first)."
        )
