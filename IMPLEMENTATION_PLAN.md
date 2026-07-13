# Implementation Plan: streamlit-lonboard

A Streamlit custom component that renders [lonboard](https://github.com/developmentseed/lonboard) layers via deck.gl using Apache Arrow (GeoArrow) end to end — no GeoJSON.

## 1. Background

### The problem

- `st.pydeck_chart` / [streamlit-deckgl](https://pypi.org/project/streamlit-deckgl/) serialize all data through pydeck as JSON/GeoJSON. Encoding, transfer, and browser-side parsing dominate render time; large datasets (>100k features) are impractical.
- Lonboard solves this in Jupyter: GeoArrow tables are shipped as binary Arrow buffers and consumed directly by [`@geoarrow/deck.gl-layers`](https://github.com/geoarrow/deck.gl-layers) without parsing. But lonboard is an [anywidget](https://anywidget.dev/), and Streamlit has no Jupyter-widget support.
- [Discussion developmentseed/lonboard#342](https://github.com/developmentseed/lonboard/discussions/342): maintainer suggests `Map.to_html()` (static, unidirectional, re-renders fully on each Streamlit rerun) and declares a connector out of scope for lonboard itself — while explicitly welcoming a third-party connector. That is this project.

### Why now

Streamlit's [custom components v2](https://docs.streamlit.io/develop/concepts/custom-components/components-v2) (`st.components.v2.component`) removed the two blockers that made earlier attempts (iframe-based v1 components) painful:

- **Raw bytes and Arrow-serializable data** can be passed to the frontend directly — no base64 detours.
- **Bidirectional state** (`BidiComponentResult`) without full-page component reload semantics, enabling picking events and view-state feedback into Python.

### Key architectural insight

Lonboard's JS runtime is a thin layer over `@geoarrow/deck.gl-layers`. We do **not** need to emulate anywidget or reuse lonboard's ESM bundle (the fragile "fake widget manager" approach). We reuse:

- **Python side of lonboard** (its `Layer` classes, GeoArrow conversion, prop validation) as-is — it's our data model and public API.
- **The same JS libraries lonboard uses** (`@geoarrow/deck.gl-layers`, `apache-arrow`, `deck.gl`, `maplibre-gl`) in our own small frontend bundle.

What we replace is only the transport: anywidget binary comm → Streamlit CCv2 `data` bytes.

## 2. Architecture

```
┌─ Python ────────────────────────────────────────────────┐
│ st_lonboard(layers=[lonboard.Layer, ...], ...)          │
│   1. for each layer:                                    │
│      - extract pyarrow.Table (GeoArrow-encoded)         │
│      - serialize → Arrow IPC stream bytes               │
│      - collect deck.gl props (colors, radii, ...) as    │
│        JSON-safe dict; per-vertex accessors stay in     │
│        the Arrow table as columns                       │
│   2. st.components.v2.component(data={...}, ...)        │
└──────────────────────────┬──────────────────────────────┘
                           │ Arrow IPC bytes + JSON props
┌─ Frontend (TS) ──────────▼──────────────────────────────┐
│   1. apache-arrow: tableFromIPC(bytes)                  │
│   2. geometry column → pick GeoArrow*Layer class        │
│      (Scatterplot / Path / Polygon / ...)               │
│   3. deck.gl Deck + MapLibre basemap                    │
│   4. events → setState: clicked / hovered / view_state  │
└──────────────────────────┬──────────────────────────────┘
                           │ picking + view state
                     back to Python (BidiComponentResult)
```

### Serialization format

Per layer: one Arrow IPC stream (all record batches, geometry + accessor columns) plus a JSON prop dict. Container format sent through `data`:

```python
data = {
    "layers": [
        {"id": "...", "type": "scatterplot", "props": {...}, "table_key": "t0"},
        ...
    ],
    "tables": {"t0": <bytes>, "t1": <bytes>},   # Arrow IPC streams
    "view_state": {...},
    "map_options": {...},
}
```

Note: CCv2 documents bytes support; **Phase 0 must verify bytes nested in dicts work**. Fallback if not: concatenate all IPC streams into a single top-level `bytes` value with an offsets index in JSON.

Compression: lonboard uses Parquet for smaller wire size (WASM decode in browser). Start with plain Arrow IPC (zero-copy, no WASM dependency); measure; add optional Parquet/zstd later if transfer size matters more than decode time. Streamlit runs are often localhost, where IPC wins.

### Rerun model (the hard part)

Streamlit reruns the whole script on every interaction; Jupyter widgets don't. Naive implementation re-serializes and re-ships the full dataset on every widget click anywhere in the app. Mitigations:

1. **Serialization cache**: memoize table→IPC bytes keyed by `id(table)`/content hash (`st.cache_data`-compatible helper).
2. **Frontend data cache**: CCv2 components keep their DOM/JS state across reruns when `key` is stable; frontend caches parsed tables by content hash and skips re-parse when the hash is unchanged.
3. **View-state preservation**: user's pan/zoom must survive reruns — frontend holds view state, Python only sets it on first render or explicit override.

## 3. Public API (target)

```python
def st_lonboard(
    map: lonboard.Map | None = None,          # or:
    layers: list[lonboard.BaseLayer] | None = None,
    view_state: dict | None = None,
    basemap_style: str = CartoLayout.PositronNoLabels,
    height: int = 500,
    on_click: bool = True,
    on_hover: bool = False,
    return_view_state: bool = False,
    key: str | None = None,
) -> StLonboardResult
```

`StLonboardResult`: `.clicked` (layer id, feature index, coordinate), `.hovered`, `.view_state`. Feature index lets users look up the row in their own GeoDataFrame — we never round-trip attribute data.

## 4. Phases

### Phase 0 — Spike (validate assumptions)
- [x] Minimal CCv2 component passing bytes; latency not yet benchmarked at scale, but bytes-in-dict was found to auto-parse through *Streamlit's own* bundled Arrow build (version we don't control), so we ship one framed top-level `bytes` blob instead (see `serialize.pack_payload`) — pinned `streamlit>=1.59` (tested against 1.59.2).
- [x] Render a hardcoded GeoArrow table with `@geoarrow/deck.gl-geoarrow` (the renamed successor to `@geoarrow/deck.gl-layers`) inside that component.
- [x] Confirm component DOM survives reruns with stable `key`; view-state persistence verified live (pan/zoom + camera survive a real Streamlit rerun; see `frontend/src/index.ts` `ensureMount`).
- Exit criteria (partial): scatterplot + path + polygon layers render and survive reruns without re-shipping/remounting; not yet load-tested at 1M points.

### Phase 1 — MVP
- [x] `st_lonboard(layers=[ScatterplotLayer])` renders with fill color/radius props (scalar and per-feature accessor).
- [x] Frontend build (Vite + TS) producing the CCv2 asset bundle; packaged via manifest (`asset_dir`, js/css paths) — see `pyproject.toml` and the mirrored `src/streamlit_lonboard/pyproject.toml`.
- [x] Prop extraction from lonboard traitlets → JSON (scalar props) / Arrow columns (per-feature accessors) — generic, trait-name-driven (`serialize.build_layer_props`).
- [x] MapLibre basemap, height option (width uses CCv2's stretch default).

### Phase 2 — Layer coverage & styling parity
- [x] `PathLayer`, `PolygonLayer`, `SolidPolygonLayer`, `HeatmapLayer` (heatmap wired but not yet exercised in the example app). `BitmapLayer`/`RasterLayer` are **not** supported — `@geoarrow/deck.gl-geoarrow` doesn't export a GeoArrow bitmap layer (raster data isn't GeoArrow feature data); unsupported types raise a clear `ValueError` from `serialize_layer`.
- [x] Multi-layer maps, `lonboard.Map` passthrough (basemap style, view state; `parameters`/`controls`/`tooltip` not yet forwarded).
- [ ] Color helpers (`apply_continuous_cmap` etc.) — should work for free since they produce Arrow columns, but not explicitly tested.

### Phase 3 — Bidirectionality
- [x] Click picking → `.clicked` (index + coordinates + layer id), with per-batch row offsets resolved back to the original table's row index.
- [x] Hover (throttled ~200ms) behind `on_hover` flag.
- [x] View state reporting behind `return_view_state` flag (throttled ~200ms; each report still triggers a rerun — documented cost, not yet mitigated).

### Phase 4 — Performance & robustness
- [ ] Serialization cache + frontend content-hash cache (skip redundant transfer/parse).
- [ ] Benchmarks vs `st.pydeck_chart` (10k / 100k / 1M / 10M features) — publish results in README.
- [ ] Optional Parquet compression for remote deployments.
- [x] Error surfaces: unsupported layer types raise a clear `ValueError` (`serialize.serialize_layer`). Missing CRS / mixed geometry types not yet handled explicitly.

### Phase 5 — Release
- [x] Unit tests for `serialize.py` (`tests/test_serialize.py`, pytest). Frontend/playwright smoke tests, CI, docs site not yet done.
- [ ] Publish to PyPI; announce in lonboard discussion #342 and Streamlit forum.
- [ ] Aim for a mention in lonboard's [ecosystem docs](https://developmentseed.org/lonboard/latest/ecosystem/) alongside Shiny.

## 5. Risks

| Risk                                                | Impact                                 | Mitigation                                                                            |
| --------------------------------------------------- | -------------------------------------- | ------------------------------------------------------------------------------------- |
| CCv2 API still maturing / min version too new       | Users on older Streamlit can't install | Document min version; no v1 fallback (v1 + JSON defeats the purpose)                  |
| Bytes-in-dict unsupported in `data`                 | Container format broken                | Phase 0 spike; single-blob + offsets fallback                                         |
| Lonboard internal APIs (traitlet extraction) change | Breakage on lonboard upgrades          | Depend on public attrs where possible; pin compatible range; CI against lonboard main |
| Rerun-triggered re-transfer of large data           | Perf regression vs Jupyter             | Content-hash caching both sides (Phase 4)                                             |
| View-state feedback loop (report → rerun → reset)   | Janky UX                               | Frontend owns view state; Python override only on explicit change                     |

## 6. Repo layout

```
streamlit-lonboard/
├── src/streamlit_lonboard/
│   ├── __init__.py          # st_lonboard entry point
│   ├── component.py         # CCv2 declaration + result wrapper
│   ├── serialize.py         # lonboard Layer -> (framed bytes payload: header + Arrow IPC)
│   ├── pyproject.toml       # manifest mirror - see the comment inside for why this exists
│   └── frontend_dist/       # built by `npm run build`; gitignored, not checked in
├── frontend/
│   ├── package.json         # deck.gl, @geoarrow/deck.gl-geoarrow, apache-arrow, maplibre-gl
│   ├── vite.config.ts       # single-file ESM + CSS output into ../src/streamlit_lonboard/frontend_dist
│   └── src/
│       ├── index.ts         # CCv2 mount entry point (idempotent across reruns)
│       ├── container.ts     # parse the framed bytes payload
│       └── layers.ts        # layer-type -> GeoArrow*Layer dispatch, accessor resolution
├── examples/app.py
├── tests/test_serialize.py
└── pyproject.toml
```
