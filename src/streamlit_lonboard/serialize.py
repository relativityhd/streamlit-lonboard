"""Convert lonboard layers into (Arrow IPC bytes, JSON-safe props).

Design (see IMPLEMENTATION_PLAN.md §2):
- Per-feature accessors (colors, radii, ...) live as columns in the Arrow
  table; scalar props go into a plain dict.
- The table is shipped as an Arrow IPC stream. No GeoJSON anywhere.
"""

from __future__ import annotations

import io
from typing import Any

import pyarrow as pa
import pyarrow.ipc


def table_to_ipc(table: pa.Table) -> bytes:
    """Serialize a pyarrow Table to Arrow IPC stream bytes."""
    sink = io.BytesIO()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


def serialize_layer(layer: Any) -> tuple[bytes, dict]:
    """Serialize a lonboard layer to (ipc_bytes, props).

    TODO(phase-1):
    - extract layer.table (GeoArrow-encoded pyarrow.Table)
    - merge per-feature accessor traitlets into the table as columns
    - collect scalar traitlets into a JSON-safe props dict
    - return layer type tag for frontend dispatch
    """
    raise NotImplementedError
