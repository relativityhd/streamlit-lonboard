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
from dataclasses import dataclass
from typing import Any, Literal
from weakref import WeakKeyDictionary

import pyarrow as pa
import pyarrow.ipc
import traitlets

from ._perf import log, span

_EXTENSION_NAME_KEY = b"ARROW:extension:name"

# Bump when the header/body framing changes shape (not for additive,
# backwards-compatible fields). frontend/src/container.ts rejects a mismatch
# loudly instead of mis-parsing.
PAYLOAD_FORMAT_VERSION = 1

Compression = Literal["auto", "gzip"] | None

# "auto" compresses only above this many raw body bytes. Below it, gzip's
# fixed CPU cost (compress here, decompress + extra round trip in the
# browser) isn't worth the transfer savings - and on localhost, IPC transfer
# is fast enough that even large payloads often aren't worth compressing
# (see IMPLEMENTATION_PLAN.md §2 and Phase 4d). Chosen so the 10k-point
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
}

# Traits that are never forwarded as props (internal/comm/ipywidgets plumbing).
_IGNORED_TRAITS = {
    "table",
    "_layer_type",
    "comm",
    "log",
    "keys",
    "extensions",
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

    `ipc_bytes` is precomputed (not derived from `table` on demand) so that
    caching a `SerializedLayer` (see `serialize_layer_cached`) also caches the
    Arrow IPC write, not just the table-building step.
    """

    layer_id: str
    layer_type: str
    props: dict[str, Any]
    table: pa.Table
    ipc_bytes: bytes


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

        if name.startswith("get_") and _is_arrow_like(value):
            accessor_columns[name] = pa.chunked_array(value)
            props[_snake_to_camel(name)] = {"@@arrowColumn": name}
        elif _is_json_safe(value):
            props[_snake_to_camel(name)] = value

    return props, accessor_columns


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


def _table_to_ipc_bytes(table: pa.Table) -> bytes:
    sink = io.BytesIO()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


def serialize_layer(layer: Any, layer_id: str) -> SerializedLayer:
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

        keep_columns = _geometry_column_names(table.schema)
        if not keep_columns:
            raise ValueError(f"layer {layer_id} has no GeoArrow-encoded geometry column")

        out_table = table.select(keep_columns)
        for name, chunked in accessor_columns.items():
            out_table = out_table.append_column(name, chunked)
        out_table = _canonicalize_table(out_table)
        ipc_bytes = _table_to_ipc_bytes(out_table)

        return SerializedLayer(
            layer_id=layer_id,
            layer_type=layer_type,
            props=props,
            table=out_table,
            ipc_bytes=ipc_bytes,
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


def _evict_cache_on_trait_change(change: dict[str, Any]) -> None:
    _layer_cache.pop(change["owner"], None)


def serialize_layer_cached(layer: Any, layer_id: str) -> SerializedLayer:
    """Like `serialize_layer`, but memoized on layer object identity.

    A cache hit skips table-building *and* the Arrow IPC write entirely.
    Invalidated automatically if any trait on the layer changes (lonboard
    layers are traitlets `HasTraits` instances), or if the same object is
    reused at a different `layer_id` (e.g. the app reordered its layers list).
    """
    cached = _layer_cache.get(layer)
    if cached is not None and cached.layer_id == layer_id:
        log("serialize_layer_cached[%s]: hit", layer_id)
        return cached

    serialized = serialize_layer(layer, layer_id)
    _layer_cache[layer] = serialized
    if layer not in _observed_layers:
        layer.observe(_evict_cache_on_trait_change, names=traitlets.All)
        _observed_layers[layer] = None
    return serialized


def _compress_body(body: bytes, compression: Compression) -> tuple[bytes, str | None]:
    """Returns `(possibly-compressed body, "gzip" or None)`.

    `byteOffset`/`byteLength` in the layer headers are computed against the
    *uncompressed* body before this runs, so the frontend must decompress the
    whole body before slicing per-layer ranges out of it (see container.ts).
    """
    if compression is None:
        return body, None
    if compression == "auto":
        if len(body) < AUTO_COMPRESSION_THRESHOLD:
            return body, None
        compression = "gzip"
    if compression != "gzip":
        raise ValueError(
            f"st_lonboard: compression={compression!r} is not supported; use 'auto', 'gzip', or None"
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

    Layout: `<u32 header_len little-endian><utf-8 json header><concatenated ipc streams,
    gzip-compressed as a whole if header["compression"] == "gzip">`.
    """
    with span("pack_payload"):
        body = io.BytesIO()
        layer_headers = []
        for layer in serialized_layers:
            ipc_bytes = layer.ipc_bytes
            layer_headers.append(
                {
                    "id": layer.layer_id,
                    "type": layer.layer_type,
                    "props": layer.props,
                    "byteOffset": body.tell(),
                    "byteLength": len(ipc_bytes),
                }
            )
            body.write(ipc_bytes)

        raw_body = body.getvalue()
        compressed_body, used_compression = _compress_body(raw_body, compression)

        header = {
            "v": PAYLOAD_FORMAT_VERSION,
            "layers": layer_headers,
            "viewState": view_state,
            "mapOptions": map_options,
            "compression": used_compression,
        }
        header_json = json.dumps(header).encode("utf-8")

        payload = struct.pack("<I", len(header_json)) + header_json + compressed_body

    log(
        "pack_payload: %d bytes total (%s), %d layers",
        len(payload),
        used_compression or "uncompressed",
        len(serialized_layers),
    )

    return payload
