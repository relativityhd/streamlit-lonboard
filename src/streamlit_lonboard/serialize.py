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

import io
import json
import struct
from dataclasses import dataclass
from typing import Any

import pyarrow as pa
import pyarrow.ipc

_EXTENSION_NAME_KEY = b"ARROW:extension:name"

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
    """One lonboard layer, ready to be packed into the wire payload."""

    layer_id: str
    layer_type: str
    props: dict[str, Any]
    table: pa.Table


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


def serialize_layer(layer: Any, layer_id: str) -> SerializedLayer:
    """Serialize a single lonboard layer into scalar props + a minimal Arrow table."""
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

    return SerializedLayer(layer_id=layer_id, layer_type=layer_type, props=props, table=out_table)


def _table_to_ipc_bytes(table: pa.Table) -> bytes:
    sink = io.BytesIO()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


def pack_payload(
    serialized_layers: list[SerializedLayer],
    *,
    view_state: dict[str, Any] | None,
    map_options: dict[str, Any],
) -> bytes:
    """Pack layers + view state + map options into a single framed `bytes` blob.

    Layout: `<u32 header_len little-endian><utf-8 json header><concatenated ipc streams>`.
    """
    body = io.BytesIO()
    layer_headers = []
    for layer in serialized_layers:
        ipc_bytes = _table_to_ipc_bytes(layer.table)
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

    header = {
        "layers": layer_headers,
        "viewState": view_state,
        "mapOptions": map_options,
    }
    header_json = json.dumps(header).encode("utf-8")

    return struct.pack("<I", len(header_json)) + header_json + body.getvalue()
