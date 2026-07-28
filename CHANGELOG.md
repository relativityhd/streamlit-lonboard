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
