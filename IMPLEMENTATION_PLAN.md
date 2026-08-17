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
- [x] Frontend build (Vite + TS) producing the CCv2 asset bundle; packaged via manifest (`asset_dir`, js/css paths) — see `pyproject.toml` and the mirrored `src/streamlit_lonboard/pyproject.toml`. Automated via a Hatchling build hook (`hatch_build.py`) that runs `npm install && npm run build` on `uv sync`/`uv build`; verified a wheel built this way installs and renders with zero Node/source-tree access (`artifacts` in `[tool.hatch.build.targets.wheel]` force-includes the gitignored `frontend_dist/` output).
- [x] Prop extraction from lonboard traitlets → JSON (scalar props) / Arrow columns (per-feature accessors) — generic, trait-name-driven (`serialize.build_layer_props`).
- [x] MapLibre basemap, height option (width uses CCv2's stretch default).

### Phase 2 — Layer coverage & styling parity
- [x] `PathLayer`, `PolygonLayer`, `SolidPolygonLayer`, `HeatmapLayer` (heatmap wired but not yet exercised in the example app). `BitmapLayer`/`RasterLayer` are **not** supported — `@geoarrow/deck.gl-geoarrow` doesn't export a GeoArrow bitmap layer (raster data isn't GeoArrow feature data); unsupported types raise a clear `ValueError` from `serialize_layer`.
- [x] DGGS layers (`H3HexagonLayer`, `S2Layer`, `A5Layer`, `GeohashLayer`) plus `ArcLayer`, `ColumnLayer`, `PointCloudLayer`, `TripsLayer` (static frame only, no animation loop). These carry geometry in accessor columns (cell IDs / point pairs) rather than a GeoArrow geometry column of `layer.table`, so `serialize_layer`'s "no geometry column" check is relaxed for exactly this set (`ACCESSOR_GEOMETRY_LAYER_TYPES` in `serialize.py`) and a scalar-accessor footgun (e.g. `S2Layer(get_s2_token="89c25c")`, valid in lonboard but unrenderable as a JSON scalar) raises a clear error instead. `TripsLayer` additionally rescales `get_timestamps` into deck.gl's float32 attribute range, mirroring lonboard's own `serialize_timestamp_accessor`. `TextLayer` stays unsupported (provisional `_`-prefixed export upstream); `BitmapLayer`/`RasterLayer`/`BitmapTileLayer` for the reason above.
- [x] Multi-layer maps, `lonboard.Map` passthrough (basemap style, view state, `picking_radius`, `parameters`, `use_device_pixels`, `custom_attribution`, `controls` - `GeocoderControl` excepted - and hover `tooltip=`).
- [x] Layer extensions: `PathStyleExtension`, `DataFilterExtension`, `BrushingExtension`, `CollisionFilterExtension` (`serialize.py` `serialize_extensions`/`serialize_controls`, `frontend/src/extensions.ts`).
- [ ] Color helpers (`apply_continuous_cmap` etc.) — should work for free since they produce Arrow columns, but not explicitly tested.

### Phase 3 — Bidirectionality
- [x] Click picking → `.clicked` (index + coordinates + layer id), with per-batch row offsets resolved back to the original table's row index.
- [x] Hover (throttled ~200ms) behind `on_hover` flag.
- [x] View state reporting behind `return_view_state` flag (throttled ~200ms; each report still triggers a rerun — documented cost, not yet mitigated).

### Phase 4 — Performance & robustness

Ordered so that measurement comes first: several of the assumptions behind the
original one-liners turned out to be wrong or already-solved when we read
Streamlit's internals, so each optimization below is gated on a measured
baseline proving it matters.

**Confirmed at runtime (see `benchmarks/RESULTS.md` for full numbers/methodology):**

- Streamlit's `ForwardMsgCache` (`runtime/forward_msg_cache.py`) does dedupe
  our BidiComponent payload: verified via `--logger.level=debug` that a rerun
  with byte-identical `st_lonboard()` output logs `Sending cached message ref`
  with a stable hash. **"Rerun re-transfers the full dataset" — the headline
  fear in §2 — is already solved by Streamlit itself, gated entirely on our
  serialization being byte-deterministic.**
- **Bigger than expected: on an unchanged rerun, the client doesn't just reuse
  cached bytes — it skips invoking our frontend's `mount()` callback
  entirely.** Confirmed directly (temporary diagnostic log, since removed):
  zero frontend perf spans and zero `mount()` invocations on a byte-identical
  rerun, at both 10k and 1M points, even though Python re-runs the script and
  re-serializes every time. This means Phase 4b's frontend cache is *not*
  needed for the "nothing changed" case — only for "one of several layers
  changed," where a real `mount()` fires and can still avoid rebuilding the
  *unchanged* layers.
- Two per-rerun costs remain on the Python side regardless of the above,
  because Python has no way to know the output will be identical without
  actually producing it and having Streamlit hash it:
  1. our own serialization (`serialize_layers` + `pack_payload`'s IPC write) —
     measured ~25ms at 1M points, and *is* skippable with a cache (4a).
  2. Streamlit's own identity/`ForwardMsgCache` hashing inside `_mount()` —
     measured **100-280ms at 1M points (23MB payload)**, i.e. slower than one
     BLAKE2b pass over that data "should" take, consistent with it being two
     full hash passes (once for our component-identity computation, once
     inside `populate_hash_if_needed` after re-serializing the whole protobuf
     message). This is entirely inside Streamlit's code path — not ours to
     skip, and the single largest fixed per-rerun cost at scale.
- On the frontend, `tableFromIPC`/`parseContainer` scales with N (~15ms at 1M)
  but `buildDeckLayers`/`setProps` stay flat and near-zero (~0.2-0.3ms even at
  1M points) — GeoArrow's zero-copy design means layer construction hands
  deck.gl typed-array views directly rather than iterating per point in JS.
  The earcut/`SolidPolygonLayer` re-tessellation cost anticipated below was
  not exercised by this baseline (single-layer scatterplot); still relevant
  for 4b's "partial layer change" case.

#### 4.0 — Baseline measurements (do first; no optimization until this lands)

- [x] Instrument, behind a `ST_LONBOARD_PERF=1` env var: Python-side
  `perf_counter` spans for serialize/pack (report per-layer), and frontend
  `performance.mark`/`measure` spans for parse/buildLayers/setProps (readable
  from the browser console and Playwright). (`_perf.py`; Streamlit's own
  `--logger.level` doesn't reach third-party logger namespaces, so `_perf.py`
  attaches its own stderr handler when the env var is set.)
- [x] Verify the `ForwardMsgCache` hypothesis end-to-end — done via
  `--logger.level=debug` server-side log inspection rather than WebSocket
  frame capture (CCv2's dev tooling doesn't expose raw frames easily); see
  findings above.
- [x] Check whether our serialization is already byte-deterministic — it was
  **not**: found and fixed a real bug (arro3's extension metadata key order
  is randomized per construction; see `_canonicalize_table` and
  `test_serialize_layer_is_byte_deterministic`).
- [x] Record the baseline table (10k / 100k / 1M points) — `benchmarks/RESULTS.md`.
- Exit criteria: a table in `benchmarks/RESULTS.md` we can diff optimizations
  against, and a definitive yes/no on wire-level re-transfer. **Met** — wire
  re-transfer is deduped (yes), and frontend re-parse is *also* skipped
  entirely for unchanged data (better than the exit criteria asked for).

#### 4a — Python-side serialization cache

- [x] Cache `serialize_layer` output keyed on layer identity
  (`serialize_layer_cached` in `serialize.py`): `WeakKeyDictionary[layer,
  SerializedLayer]`, hits when the user reuses the layer object across
  reruns (i.e. built it under `st.cache_resource`). A hit skips table-building
  *and* the Arrow IPC write (moved into `serialize_layer` itself so caching
  captures both). Invalidation: layers are traitlets `HasTraits` instances, so
  a `layer.observe(..., names=traitlets.All)` hook attached on first cache
  populate evicts the entry on any trait mutation; a `layer_id` mismatch
  (e.g. the app reordered its layers list) is also treated as a miss. L2
  (content-fingerprint) skipped — not needed, see measurement below.
- [x] Documented the `st.cache_resource`-wrapped-layer-factory pattern in the
  README ("Performance" section) and in `examples/app.py` (all three demo
  layers now built via `@st.cache_resource` functions).
- [x] Re-measured against 4.0 baseline: verified live (`ST_LONBOARD_PERF=1`)
  that with the example app's layers cache-resource'd, a rerun logs
  `serialize_layer_cached[layer-N]: hit` for all layers and
  `serialize_layers` drops from ~1.9ms to ~0.2-0.3ms.
- Exit criteria met: rerun with unchanged, cache-resource'd layers spends
  ~0ms in `serialize_layer`/IPC-write on a hit; example app demonstrates the
  pattern.

#### 4b — Frontend cache: skip redundant parse and layer rebuild

**Scope reduced by the 4.0 finding**: CCv2 already skips calling `mount()` at
all when the whole payload is byte-identical, so the "unrelated rerun redoes
everything" case (including every `return_view_state` rerun with nothing
else changing) is already free. What's left for 4b is the case where
`mount()` *does* fire because *something* changed — e.g. one of several
layers — and rebuilds every layer instead of just the changed one.

- [x] Per-layer granularity: `container.ts`'s `parseContainer` now takes a
  `previousFingerprints` map and computes an FNV-1a fingerprint per layer's
  raw byte slice *before* parsing, returning `{status: "unchanged"}` (no
  `tableFromIPC` call at all) for a match instead of just skipping layer
  construction — `tableFromIPC` is the dominant frontend cost at scale (4.0
  baseline), so skipping the parse matters more than skipping layer
  construction alone. `index.ts`'s `mount()` keeps a
  `layerCache: Map<layerId, {fingerprint, deckLayers, subLayerEntries}>` on
  the mount state and reuses the prior deck.gl `Layer` instances verbatim
  (same object references, not just same id) for unchanged layers.
- [x] `subLayerLookup` stays consistent: cached `subLayerEntries` are
  re-inserted for reused layers on every `mount()` call, so picking/hover
  resolve correctly for layers served entirely from cache.
- [x] Cache lives on `MountState`, which already resets on remount (new `key`
  or first mount) - no separate guard needed.
- Exit criteria met - verified live with a 2-layer app (one `st.cache_resource`
  static layer, one layer rebuilt fresh each rerun): a rerun that changes only
  the dynamic layer's data produces perf spans for `tableFromIPC[layer-0]`
  and `buildDeckLayers[layer-0]` only - zero entries for the unchanged
  `layer-1`, confirming it was neither re-parsed nor rebuilt. Picking on the
  reused layer still resolves to the correct lonboard layer id and row index
  afterward.

#### 4c — Benchmarks vs `st.pydeck_chart` (publish in README)

- [x] `benchmarks/` directory: data generators (synthetic points at 10k /
  100k / 1M / 10M - `benchmarks/datagen.py`), one Streamlit app per contender
  (`benchmarks/contenders/{lonboard_app,pydeck_app,tohtml_app}.py`).
  **Polygon/tessellation dataset not built** - out of scope for this pass;
  the point-scatter comparison already surfaced the headline findings below,
  and a polygon set would mainly change absolute numbers, not the
  conclusions. Flagging as unfinished rather than claiming coverage that
  isn't there.
- [x] Driver: `benchmarks/playwright_driver.py`. Measures time-to-first-render
  (navigation → canvas appears and its bounding box stops changing - not
  literally deck.gl's `onAfterRender`, which isn't exposed the same way by
  all three contenders; canvas-settle is the closest signal all three share),
  rerun-with-unchanged-data latency, and interaction-triggered rerun latency
  (`st_lonboard` only - the other two never round-trip view state to Python
  at all, so there's nothing to measure there). Uses Streamlit's own
  `data-test-script-state` DOM attribute (the same signal Streamlit's own
  test suite uses) to know precisely when a rerun starts/ends. Each
  `(contender, N)` measurement runs in its own subprocess with a hard
  wall-clock timeout - required in practice, not just defensive: pydeck's
  headless rendering intermittently stalls the browser's main thread hard
  enough that even a trivial `page.evaluate()` call can hang indefinitely
  (see `benchmarks/RESULTS.md`), and only a subprocess boundary can recover
  from that.
- [x] pydeck did DNF, but not the way predicted (not an OOM/slow-parse at
  large N - it renders fine interactively at every scale tested). It's a
  headless-Chromium-automation-specific stall (WebGL/GPU readback, console
  warnings included), reported honestly as an environment limitation rather
  than chased further or extrapolated. `Map.to_html()` DNF'd too, but for a
  completely different, scale-independent, and load-bearing reason: it
  doesn't render at all when embedded via `components.v1.html`, root-caused
  to `document.location.href` being `"about:srcdoc"` inside the sandboxed
  iframe, which breaks requirejs/anywidget's module loading. Full detail in
  `benchmarks/RESULTS.md`.
- [x] Environment (hardware, versions, localhost transport) and the
  comparison tables are published in the README's "vs. `st.pydeck_chart` and
  `Map.to_html()`" subsection; full numbers, methodology, and root-cause
  writeups are in `benchmarks/RESULTS.md`.
- Exit criteria: reproducible one-command benchmark run (`uv run python
  benchmarks/playwright_driver.py` / `benchmarks/payload_sizes.py`, both
  under `uv sync --extra bench`); README table. Met, with the one honest gap
  noted above (no polygon dataset).

#### 4d — Optional compression for remote deployments

- [x] **Confirmed constraint:** arrow-js (v17, bundled) throws
  `"Record batch compression not implemented"` — Arrow IPC buffer compression
  (zstd/lz4) is a dead end for the frontend. Went with whole-payload gzip:
  `gzip.compress` in Python, native `DecompressionStream('gzip')` in the
  browser — zero new dependencies on either side. Parquet/`parquet-wasm` not
  pursued (see measurement below — gzip's ratio ceiling on this data isn't a
  gzip-specific problem, so a different codec is unlikely to help much either
  without a fundamentally different encoding like delta/quantization).
- [x] Format-version field landed in 4.0 (`"v": 1`); added a `"compression":
  "gzip" | null` header field alongside it in this phase.
- [x] API: `st_lonboard(compression="auto" | "gzip" | None)`, default
  `"auto"` = compress only above `serialize.AUTO_COMPRESSION_THRESHOLD` (1MB
  raw). `pack_payload`'s `compression` param backs it; three dedicated
  Python tests plus a live-browser check per mode (see below).
- [x] Measured ratio + added latency on 1M-point uniform *and* clustered
  datasets (`benchmarks/bench_app.py` now takes `BENCH_COMPRESSION`/
  `BENCH_CLUSTERED`); full numbers and discussion in `benchmarks/RESULTS.md`.
  Headline: ~11% size reduction (clustering doesn't meaningfully improve this
  — gzip needs repeated byte sequences, not spatial proximity, and jittered
  float64 coordinates stay high-entropy either way) for ~900ms-1s Python
  compress + ~200ms JS decompress at 1M points. Verified live in the browser
  for all three modes (`auto`-small stays uncompressed, `none` never
  compresses, `gzip` always compresses even when tiny) — correct rendering
  and picking, no console errors, in each case.
- Exit criteria met, decision is **situational, not "always compress"**: keep
  the feature since it's a real win on slow/high-latency networks, but the
  CPU cost is large enough relative to the size win that it's a net loss on
  localhost or fast links — document this plainly (done, in `RESULTS.md` and
  the README) rather than claim compression is a general improvement. The
  1MB `"auto"` threshold is a starting point to tune per-deployment, not a
  validated-optimal default.

#### 4e — Robustness & error surfaces

- [x] Unsupported layer types raise a clear `ValueError` (`serialize.serialize_layer`).
- [x] Payload size guard: fail in Python with an actionable message when the
  payload exceeds Streamlit's `server.maxMessageSize` (default 200 MB) instead
  of letting the WebSocket die opaquely; mention the config knob and
  downsampling in the error text. Implemented as `serialize.check_payload_size`,
  called from `st_lonboard()` with the live `server.maxMessageSize` option;
  tested in `tests/test_serialize.py` and live-verified with an artificially
  low limit.
- [x] CRS: investigated whether to warn on missing/non-EPSG:4326 CRS in a
  layer's GeoArrow extension metadata. **Finding: not needed, and not added.**
  This plan originally assumed "`from_geopandas` reprojects, but raw-table
  construction doesn't" — that's wrong. Reprojection to EPSG:4326 (with
  lonboard's own `warnings.warn`) happens unconditionally in
  `BaseArrowLayer.__init__` (`reproject_table(table_o3, to_crs=OGC_84)`,
  called before the default-viewport calculation), regardless of whether the
  layer was built via `from_geopandas` or directly from a raw Arrow table.
  Verified by round-tripping real coordinates through EPSG:3857 into a raw
  table and confirming both the warning fires and the stored coordinates come
  back out in degrees. Since every lonboard layer object is therefore
  guaranteed to already carry EPSG:4326 geometry by the time it reaches
  `serialize_layer()`, our own CRS check would be unreachable defensive code
  for a scenario that cannot occur.
- [x] Geometry edge cases with tests: empty table (0 rows), null geometries,
  Point vs MultiPoint in one dataset (GeoArrow columns are homogeneous by
  construction, so this is about verifying the Multi* variants render, not
  about mixed columns).
  - Empty table (0 rows): rejected by **lonboard itself** at layer
    construction (`from_geopandas` raises "Geometry type combination is not
    supported ([])") before a layer object even exists, so `serialize_layer`
    never sees it. Nothing for us to handle.
  - Null geometries: `serialize_layer` handles these fine (row kept as-is).
    But mixing a null geometry into a layer's geometry column makes
    lonboard's own auto-computed view state come back as `nan`/`nan`
    (`Map.view_state`'s centroid/bbox math propagates NaN through nulls
    rather than skipping them). `json.dumps` then emits a bare `NaN` token,
    which is invalid JSON and crashed the frontend's `JSON.parse` with a
    cryptic error, taking down the whole component (confirmed live in the
    browser). **Fixed**: `pack_payload` now encodes the header with
    `json.dumps(..., allow_nan=False)` and turns the resulting `ValueError`
    into a clear, actionable Python-side error naming the likely cause and
    the fix (pass an explicit `view_state=`, or drop null geometries).
    Verified live: same repro now surfaces a clean Streamlit error box
    instead of a `BidiComponent Error`; passing an explicit `view_state=`
    lets the layer render normally. Regression tests in
    `tests/test_serialize.py`.
  - MultiPoint: renders correctly (`ScatterplotLayer`, `geoarrow.multipoint`
    extension type) — verified in Python and live in the browser.
- [x] Frontend errors: wrap per-layer build in try/catch so one bad layer
  degrades to a console error + skipped layer instead of taking down the whole
  component via the BidiComponent error boundary; include layer id and type in
  every error message. Implemented in `frontend/src/index.ts`'s per-layer loop
  around `buildDeckLayers`; not cached, so a fingerprint-unchanged rerun
  retries (and re-logs) rather than silently staying broken. Live-verified by
  temporarily forcing one layer to throw (alongside a normal layer) and
  confirming: the good layer still renders, the console error names the
  layer's id and type, and there's no BidiComponent crash - then reverted the
  test hook and rebuilt (bundle size matches pre-change exactly).
- [x] Multiple `st_lonboard` instances in one app (with and without keys) —
  verify no identity collisions or cross-instance state bleed; add a smoke
  test. Live-verified three scenarios: (1) two maps with different data, no
  key - both render independently, no bleed; (2) two maps with
  byte-identical data, no key - Streamlit itself raises
  `StreamlitDuplicateElementId` (same as any duplicate widget call; not a
  streamlit-lonboard bug, but a real gotcha, so the `key` docstring in
  `component.py` now calls it out explicitly); (3) two maps with distinct
  explicit keys - both render independently. No cross-instance state bleed
  found: frontend state lives per-`parentElement` (one per component
  instance, see `index.ts`'s `MOUNT_KEY`), and the Python
  `serialize_layer_cached` cache is keyed on `(layer object identity,
  layer_id)`, which is exactly the granularity needed for correctness even
  when the same layer object is reused across multiple `st_lonboard()` calls.
- Exit criteria: every failure mode above produces a message that names the
  offending layer and says what to do about it; none of them crash the app.

### Phase 5 — Release
- [x] Unit tests for `serialize.py` (`tests/test_serialize.py`, pytest). Frontend/playwright smoke tests, CI, docs site not yet done.
- [x] Publish to PyPI; announce in lonboard discussion #342 and Streamlit forum.
- [ ] Aim for a mention in lonboard's [ecosystem docs](https://developmentseed.org/lonboard/latest/ecosystem/) alongside Shiny.

## 5. Risks

| Risk                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | Impact                                                                       | Mitigation                                                                                                                                                                                                                                                                                                   |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| CCv2 API still maturing / min version too new                                                                                                                                                                                                                                                                                                                                                                                                                           | Users on older Streamlit can't install                                       | Document min version; no v1 fallback (v1 + JSON defeats the purpose)                                                                                                                                                                                                                                         |
| Bytes-in-dict unsupported in `data`                                                                                                                                                                                                                                                                                                                                                                                                                                     | Container format broken                                                      | Phase 0 spike; single-blob + offsets fallback                                                                                                                                                                                                                                                                |
| Lonboard internal APIs (traitlet extraction) change                                                                                                                                                                                                                                                                                                                                                                                                                     | Breakage on lonboard upgrades                                                | Depend on public attrs where possible; pin compatible range; CI against lonboard main                                                                                                                                                                                                                        |
| Rerun-triggered re-work on large data — reading Streamlit internals suggests wire re-transfer is already deduped by `ForwardMsgCache` (`ref_hash`), leaving per-rerun CPU as the real cost: our re-serialization, Streamlit's double BLAKE2b hash of the payload, frontend re-parse + deck.gl rebuild/re-tessellation                                                                                                                                                   | Perf regression vs Jupyter                                                   | Verify `ForwardMsgCache` behavior at runtime, then cache both sides (Phase 4.0/4a/4b); the double hash is Streamlit-internal and only mitigable by keeping payloads byte-deterministic                                                                                                                       |
| View-state feedback loop (report → rerun → reset)                                                                                                                                                                                                                                                                                                                                                                                                                       | Janky UX                                                                     | Frontend owns view state; Python override only on explicit change                                                                                                                                                                                                                                            |
| pyarrow 25.0.0 bundles mimalloc 3.3.1, which SIGSEGVs when libarrow is first loaded on a non-main thread that then exits — Streamlit's exact execution model (fresh ScriptRunner thread per run). Found while debugging "dev server crashes when panning"; upstream [apache/arrow#50471](https://github.com/apache/arrow/issues/50471) / [microsoft/mimalloc#1287](https://github.com/microsoft/mimalloc/issues/1287), deterministic 5-line repro, no fixed release yet | Crashes the whole Python process, no traceback unless `PYTHONFAULTHANDLER=1` | `pyarrow>=14,!=25.0.0` in `pyproject.toml` (A/B-verified: pa25+mimalloc crashes ≤3 reruns; pa25+system-pool, pa24 on py3.13, and pa24 on py3.14 each survive 190-240 scripted reruns). Drop the exclusion when a fixed release ships; `ARROW_DEFAULT_MEMORY_POOL=system` works around it if 25.0.0 is forced |

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
