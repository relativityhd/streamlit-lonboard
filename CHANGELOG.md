# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

<!--
  The release workflow (.github/workflows/release.yml, via
  scripts/sync_version.py) rewrites the "## [Unreleased]" heading below into a
  dated, versioned section and opens a fresh empty one above it.

  Two rules keep that automation working:
    1. Keep the heading exactly `## [Unreleased]`.
    2. Write your changes under it as you merge them - the release workflow
       refuses to run if the Unreleased section is empty, so the changelog can
       never silently fall behind a published version.
-->

## [Unreleased]

## [0.2.0] - 2026-08-20

### Added

- Support for lonboard's DGGS layers - `H3HexagonLayer`, `S2Layer`, `A5Layer`, and
  `GeohashLayer` - plus `ArcLayer`, `ColumnLayer`, `PointCloudLayer`, and
  `TripsLayer` (static frame only; drive `layer._current_time` yourself for
  animation). ([#1](https://github.com/relativityhd/streamlit-lonboard/pull/1))
- Layers whose geometry lives in accessor columns (DGGS cell IDs, Arc position
  pairs) no longer require a GeoArrow geometry column in `layer.table`. A clear
  error is raised if a required accessor is a scalar instead of a per-row array
  (e.g. `S2Layer(get_s2_token="89c25c")`), which lonboard accepts but cannot be
  rendered from a JSON prop.
- `st_lonboard()` warns when it cannot compute a default view state for these
  layer types (lonboard has no auto-viewport for S2/A5/Geohash/Arc, and H3
  auto-centering needs `h3-py` installed) - pass an explicit `view_state=`.
- New `examples/dggs_app.py` demonstrating H3 and Arc layers.
- Support for lonboard's layer extensions: `PathStyleExtension` (dashed/offset
  paths), `DataFilterExtension` (GPU-side numeric/category filtering - pairs
  well with `st.slider` driving `layer.filter_range`), `BrushingExtension`, and
  `CollisionFilterExtension`. The layer-side props an extension injects (e.g.
  `get_dash_array`, `filter_range`) already shipped before this change; what
  was missing was instantiating the extension itself on the frontend, which is
  what actually activates deck.gl's shader-level behavior.
- `st_lonboard()` now forwards more of `lonboard.Map`: `picking_radius`,
  `parameters` (deck.gl GPU parameters), `use_device_pixels`, and
  `custom_attribution`, each as a new keyword argument defaulting to the
  passed `map`'s own value. `map.controls` (a fullscreen button, zoom/compass
  buttons, and a scale bar by default - lonboard's own default) is now always
  rendered; there's no way to opt out short of passing `Map(controls=[])`.
  `GeocoderControl` isn't supported (it needs a Python-side async handler with
  no Streamlit equivalent) and is skipped with a warning if present.
- Hover tooltips: `st_lonboard(tooltip=True)` shows every non-geometry column
  of each layer's own data; `tooltip=["name", "population"]` shows only those
  columns (best-effort per layer - a name absent from a particular layer's
  data is silently skipped). Falls back to the passed `map`'s `show_tooltip`
  when `tooltip` is left at its default `False`. Requires `pickable=True` on
  the layer (lonboard's own default).

### Fixed

- A rerun that changed only a layer's JSON props (e.g. a slider driving `opacity`
  or `filter_range`) while its Arrow geometry/accessor bytes stayed identical was
  silently ignored - the frontend's per-layer cache fingerprinted only the Arrow
  IPC bytes, so it reused the previous render verbatim, props and all. The cache
  now fingerprints props separately from the Arrow bytes, so prop-only changes
  are still picked up while byte-identical layers still skip the expensive
  `tableFromIPC` parse.

## [0.1.0] - 2026-07-28

### Added

- `st_lonboard()`: render [lonboard](https://developmentseed.org/lonboard/) layers in
  Streamlit over Arrow IPC end to end - no GeoJSON round-trip.
- Accepts either a `lonboard.Map` (reusing its layers, view state, and basemap) or a
  plain list of lonboard layers.
- Interaction results via `StLonboardResult`: click, hover, and view-state feedback,
  toggled with `on_click`, `on_hover`, and `return_view_state`.
- Optional gzip compression of the Arrow payload: `compression="auto"` (compress above
  1 MB), `"gzip"` (always), or `None` (never).
- Serialization cache on the Python side and a per-layer content-hash cache on the
  frontend, so unchanged layers are neither re-encoded nor re-uploaded across reruns.
- Payload size guard that fails with an actionable message instead of a truncated
  websocket frame when the payload exceeds Streamlit's `server.maxMessageSize`.
- Automatic frontend bundling through a Hatchling build hook, so `uv build`,
  `uv sync`, and `pip install [-e] .` build `frontend_dist/` without a manual
  `npm run build`. Consumers installing the published wheel never need Node.
- Optional performance instrumentation behind `ST_LONBOARD_PERF=1`.
- Benchmarks comparing `st_lonboard` against `st.pydeck_chart` and lonboard's own
  `Map.to_html()` (see `benchmarks/RESULTS.md`).

### Fixed

- Multi-batch polygon and path layers rendered nothing: the Arrow IPC stream written
  for layers split across several record batches was invalid.

### Notes

- `pyarrow==25.0.0` is excluded: it bundles mimalloc 3.3.1, which segfaults when
  libarrow is first loaded on a non-main thread that then exits - exactly what
  Streamlit's per-rerun `ScriptRunner` threads do. See
  [apache/arrow#50471](https://github.com/apache/arrow/issues/50471).
- Requires Streamlit >= 1.59 (custom components v2 / `st.components.v2`).
