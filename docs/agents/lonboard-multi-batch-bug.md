# Bug report: multi-batch polygon layers render nothing in streamlit-lonboard

**Target repo:** `streamlit-lonboard` (local sibling: `/home/tobias/Documents/Repositories/streamlit-lonboard`)
**Found:** 2026-07-19, while building the darts-analysis dashboard vertical slice (Overview page, A5 level-9 choropleth).
**Status:** Fixed. `serialize.py::_table_to_ipc_bytes` now calls
`table.combine_chunks()` before writing the IPC stream (Option A below),
verified live with a `_rows_per_chunk=20` `SolidPolygonLayer` (60 rows / 3
chunks): renders correctly, no `deck.gl: assertion failed` console errors.
Regression test: `tests/test_serialize.py::
test_serialize_layer_produces_valid_ipc_for_multi_chunk_polygon_layer`
(round-trips a `_rows_per_chunk=2` polygon layer and asserts every read-back
batch validates, and that the batch count collapses to 1). Note: the exact
byte-level corruption from the minimal repro below no longer reproduces
against this repo's pinned pyarrow (24.0.0 vs. this report's 20.0.0) - the
upstream bug appears fixed in later pyarrow releases, but the fix here is
applied unconditionally since `pyproject.toml` still permits older,
vulnerable pyarrow versions (`pyarrow>=14,!=25.0.0`). The darts-analysis
`_rows_per_chunk=len(gdf)` workaround can be removed once this fix ships.

## Summary

Any lonboard layer whose Arrow table has **more than one chunk** and a **variable-size (list-offset) geometry** — Polygon, MultiPolygon, and by construction Path/LineString — serializes to an **invalid Arrow IPC stream**. The frontend then throws `deck.gl: assertion failed` for every affected sub-layer and draws **nothing**, silently (the Streamlit app keeps running; the map is just empty).

lonboard auto-rechunks large tables, so this triggers implicitly the moment a layer crosses the size threshold: in practice, a `SolidPolygonLayer.from_geopandas` over ~53k+ rows works at 40k rows and silently breaks at 160k. Fixed-size geometries (points / ScatterplotLayer) are **not** affected.

The corruption happens in **pyarrow's C++ IPC writer**, which mis-serializes the Rust-style sliced list arrays that lonboard's `arro3` rechunk produces — a known upstream bug: [apache/arrow#46407 "Rust sliced ListArrays get corrupted by C++ IPC serialization"](https://github.com/apache/arrow/issues/46407). streamlit-lonboard is the right place to defend against it, because it owns the `arro3 → pyarrow → IPC bytes` pipeline.

## Environment

| Component | Version |
|---|---|
| streamlit-lonboard | local checkout (serialize path: `serialize.py::serialize_layer` → `_table_to_ipc_bytes`) |
| lonboard | 0.16.0 (`layer.table` is an `arro3.core.Table`) |
| pyarrow | 20.0.0 |
| Python | 3.13 (darts-analysis `.venv`) |
| Frontend | `frontend/src/layers.ts` + `@geoarrow/deck.gl-layers` |

## Symptoms

- Browser console, repeated per render attempt:
  ```
  deck: update of GeoArrowSolidPolygonLayer({id: 'layer-0-0'}): deck.gl: assertion failed.
      at Xe (index.js)               // deck.gl assert()
      at oy._renderMultiPolygonLayer
      at oy.renderLayers
      ...
  ```
  Note it fails for **`layer-0-0`, the first sub-layer** — not just the later batches — and no polygons are drawn at all.
- No Python-side error, no Streamlit exception. The map shows only the basemap.
- First observed with the darts-analysis level-9 A5 cell layer: 158,936 MultiPolygon rows → lonboard infers `_rows_per_chunk = 52,979` → 3 chunks → empty map. The same data at levels 6–8 (≤ 40,819 rows, single chunk) renders fine.

## Minimal reproduction (6 rows, no external data)

`_rows_per_chunk` forces the same rechunk that big tables get automatically:

```python
import io
import geopandas as gpd
import numpy as np
import pyarrow as pa, pyarrow.ipc
from lonboard import SolidPolygonLayer
from shapely.geometry import MultiPolygon, Polygon

def sq(x):
    return Polygon([(x, 0), (x + 0.5, 0), (x + 0.5, 0.5), (x, 0.5)])

gdf = gpd.GeoDataFrame(geometry=[MultiPolygon([sq(i)]) for i in range(6)], crs="EPSG:4326")
layer = SolidPolygonLayer.from_geopandas(
    gdf, get_fill_color=np.full((6, 4), 128, dtype="uint8"), _rows_per_chunk=2
)

# identical to streamlit_lonboard.serialize._table_to_ipc_bytes
table = pa.table(layer.table).select(["geometry"])
sink = io.BytesIO()
with pa.ipc.new_stream(sink, table.schema) as w:
    w.write_table(table)

for i, batch in enumerate(pa.ipc.open_stream(io.BytesIO(sink.getvalue()))):
    try:
        batch.validate(full=True)
        print(f"batch {i}: valid")
    except Exception as e:
        print(f"batch {i} INVALID: {e}")
```

Output (pyarrow 20.0.0):

```
batch 0: valid
batch 1 INVALID: In column 0: Invalid: Offset invariant failure: offset for slot 1 out of bounds: 3 > 2
batch 2 INVALID: In column 0: Invalid: Offset invariant failure: offset for slot 1 out of bounds: 5 > 2
```

The same check with plain `Polygon` geometry: 2 of 3 batches invalid. With `Point` / `ScatterplotLayer`: all batches valid (fixed-size list, no offsets buffer to corrupt).

Real-data repro (the case that surfaced this): `darts-analysis/data/_dashboard_cache/DARTS_v2_S2mos_v3_iter2/geometry_level9.parquet` (158,936 MultiPolygon rows), `SolidPolygonLayer.from_geopandas` with no `_rows_per_chunk` override, rendered via `st_lonboard`. Batch offsets after IPC round-trip:

```
batch 0: rows=52979  outer offsets[0]=0       offsets[-1]=53058   child_len=53058   ← valid
batch 1: rows=52979  outer offsets[0]=53058   offsets[-1]=106037  child_len=52979   ← offsets not rebased
batch 2: rows=52978  outer offsets[0]=106037  offsets[-1]=159035  child_len=52998   ← offsets not rebased
```

Batches 1 and 2 keep their **absolute** offsets from the original unsliced array while the child arrays were truncated to just the slice's window — internally inconsistent, hence the validator failure and the frontend assertion.

## Root-cause chain

1. **lonboard rechunks large tables.** `lonboard/layer/_base.py` (0.16.0): `rows_per_chunk = infer_rows_per_chunk(table)` (byte-size heuristic; 52,979 for the level-9 table), then `table_o3 = table_o3.rechunk(max_chunksize=rows_per_chunk)`. `arro3` is Rust; its rechunk produces **zero-copy slices** — each chunk after the first is a list array whose offsets don't start at 0 (Rust-style slicing keeps the parent buffers and a window into them).
2. **streamlit-lonboard converts and writes IPC.** `serialize.py::serialize_layer` does `pa.table(layer.table)` (arro3 → pyarrow via the Arrow C data interface — the sliced representation survives this and is *valid in memory*; `table.validate(full=True)` passes), then `_table_to_ipc_bytes` writes it with `pa.ipc.new_stream(...).write_table(table)`, one record batch per chunk.
3. **pyarrow's C++ IPC writer corrupts the sliced chunks.** For a sliced variable-size list, the spec-compliant options are "rebase offsets to 0" or "ship the full child + original offsets". pyarrow 20.0.0 does neither consistently: it truncates the child arrays to the slice window **but keeps the absolute, un-rebased offsets**. This is [apache/arrow#46407](https://github.com/apache/arrow/issues/46407). The result is an IPC stream that pyarrow itself rejects on `validate(full=True)` after read-back.
4. **The frontend fails, correctly.** `frontend/src/layers.ts` maps each `RecordBatch` to its own deck.gl sub-layer (`layer-0-0`, `layer-0-1`, …). `@geoarrow/deck.gl-layers` walks the nested lists and hits `assert()` in `_renderMultiPolygonLayer` because offsets point past the child length. deck.gl catches the throw per sub-layer, logs `deck: update of GeoArrowSolidPolygonLayer(...)`, and skips drawing. (That even `layer-0-0` — the *valid* batch 0 — fails is presumably a follow-on: the assertion in the shared render path aborts the whole layer family; the net effect either way is an empty map.)

## Why levels 6–8 worked in the dashboard

`infer_rows_per_chunk` returned ≥ the table length for those sizes (≤ 40,819 rows), so the table stayed single-chunk: no slicing, nothing for the IPC writer to corrupt.

## Suggested fix (in `streamlit_lonboard/serialize.py`)

Defend at the IPC boundary — this keeps working regardless of what lonboard/arro3 hand over, and regardless of when the upstream pyarrow bug is fixed:

- **Option A (simplest): combine before writing.** In `_table_to_ipc_bytes` (or just before it), `table = table.combine_chunks()`. Verified to produce a fully valid single-batch stream for the 159k-row table. Cost: one memory copy at serialization time, and the frontend gets one sub-layer instead of N — `layers.ts`'s `rowOffset` picking arithmetic degenerates to offset 0, so picking indices stay correct. (deck.gl handles a single 159k-polygon GeoArrow layer fine — verified visually in the darts dashboard.)
- **Option B (preserves chunking): rebase each chunk.** If per-batch sub-layers are worth keeping (e.g. for very large tables where one giant batch is undesirable), copy-compact each chunk so its offsets start at 0: for each column chunk, `pa.concat_arrays([chunk])` forces a compacted copy; rebuild each `RecordBatch` from the compacted arrays and `write_batch` them. More code, same total copy cost as A.
- **Either way, add a cheap guard + regression test.** A unit test that round-trips a `_rows_per_chunk=2` polygon layer through `serialize_layer` and asserts `batch.validate(full=True)` for every read-back batch would have caught this. Optionally, in debug/dev mode, validate batches after writing and raise a loud Python-side error instead of letting the frontend fail silently — the silent empty map is the worst part of this bug.

Upstream (optional): comment on [apache/arrow#46407](https://github.com/apache/arrow/issues/46407) with this repro; consider an issue on lonboard about `rechunk` producing sliced chunks that pyarrow can't IPC-write faithfully.

## Current workaround in darts-analysis

`dashboard/maps.py::a5_choropleth_layer` passes `_rows_per_chunk=len(gdf)` to `SolidPolygonLayer.from_geopandas`, forcing a single chunk. Remove once streamlit-lonboard ships the fix.

## Related (secondary, unresolved) observation

Even with single-batch tables, a handful of the same `deck.gl: assertion failed` messages appear **transiently** when an already-mounted map swaps to a different dataset (e.g. switching A5 level 6 → 9 re-renders the component with a new table). Rendering succeeds afterwards and the errors stop — so this looks like the old layer briefly updating against the new/partial payload during remount, not data corruption. Harmless but noisy; worth a look while in `layers.ts`/`container.ts` (e.g. tearing down old deck layers before applying a new payload).
