# Phase 4.0 baseline measurements

Recorded with `benchmarks/bench_app.py` (single `ScatterplotLayer`, per-point
color + radius accessors) via `ST_LONBOARD_PERF=1`, on the dev sandbox VM
(Python 3.14.6, pyarrow 24.0.0, Streamlit 1.59.x, localhost transport). These
are single-run wall-clock numbers on a shared/constrained VM, not averaged
over multiple trials — treat them as order-of-magnitude baselines to diff
future optimizations against, not precise benchmarks.

How to reproduce: `ST_LONBOARD_PERF=1 BENCH_N=<n> uv run streamlit run
benchmarks/bench_app.py`, then use the "Rerun (new data)" button (forces a
genuinely different payload) or "Rerun (unchanged data)" button (byte-identical
payload) and read the `streamlit_lonboard.perf` stderr log plus the browser
console's perf table.

## Headline finding: unchanged-data reruns already cost ~0 frontend CPU

Before optimizing anything, we checked whether Streamlit's `ForwardMsgCache`
(hash-based dedup for messages ≥10KB, see `IMPLEMENTATION_PLAN.md` 4.0) applies
to our BidiComponent payload, and how far the savings actually reach.

Verified via `--logger.level=debug`: a rerun with byte-identical
`st_lonboard()` output logs `Sending cached message ref (hash=...)` with the
same stable hash every time - confirming the wire-level payload is deduped, as
predicted. This *requires* our serialization to be deterministic (see
"Determinism bug" below); it silently would not have worked before that fix.

What we did not predict: the client doesn't just reuse cached *bytes* - it
skips invoking the frontend's `mount()` JS callback **entirely**. We confirmed
this directly (temporary unconditional `console.log` at the top of `mount()`,
since removed) at both 10k and 1M points: clicking "Rerun (unchanged data)"
produces a new Python-side script run (new perf spans logged server-side every
time) but zero frontend perf entries and zero mount() invocations - the
browser does no parsing, no deck.gl layer reconstruction, no GPU re-upload at
all. This means Phase 4b's frontend content-hash cache is unnecessary for the
"nothing changed" case (Streamlit already handles it for free); 4b still
matters for the "one of several layers changed" case, where a mount() *does*
fire and could otherwise re-parse/rebuild every layer instead of just the
changed one.

The cost that remains on an unchanged rerun is entirely Python-side (see
below) - Streamlit has no way to know the output will be identical without
actually running the script and re-hashing the result.

## Timings by scale

All times in ms, single run. "wire bytes" = the framed payload
(`serialize.pack_payload` output) actually sent.

|  N points | wire bytes | serialize_layers (py) | pack_payload (py, incl. IPC write) | mount (py, Streamlit hash+enqueue) | st_lonboard.total (py) | parseContainer (js) | buildDeckLayers (js) | setProps (js) | mount total (js) |
| --------: | ---------: | --------------------: | ---------------------------------: | ---------------------------------: | ---------------------: | ------------------: | -------------------: | ------------: | ---------------: |
|    10,000 |    233,033 |                  ~0.5 |                               ~0.4 |                               ~3.5 |                     ~7 |                ~1.0 |                 ~0.1 |          ~0.3 |             ~1.4 |
|   100,000 |  2,303,033 |                  ~0.6 |                               ~2.5 |                                ~17 |                    ~23 |                ~1.7 |                 ~0.2 |          ~0.2 |             ~2.2 |
| 1,000,000 | 23,003,658 |                  ~0.5 |                                ~25 |                           ~100-280 |               ~130-320 |                 ~15 |                 ~0.2 |          ~0.3 |            ~15.5 |

### Reading this table

- **`serialize_layers` (Python) is essentially free and flat.** It's building
  a new `pa.Table` view + column select/append, which are zero-copy
  metadata operations in Arrow regardless of row count - the actual bytes
  aren't touched until IPC write.
- **`pack_payload`'s IPC write is the first real O(N) Python cost**: ~0.4ms
  at 10k, ~25ms at 1M - roughly linear, as expected for a buffer copy +
  framing pass.
- **`mount` (Streamlit-internal identity hash + `ForwardMsgCache` hash) is
  the dominant Python-side cost at scale**, and it's *not* linear-looking in
  a good way: ~3.5ms at 10k but 100-280ms at 1M for a 23MB payload - far
  slower than a single BLAKE2b pass over 23MB should take (BLAKE2b runs
  ~1-3 GB/s; 23MB should be ~10-25ms for *one* pass). This matches the
  "double hash" risk called out in `IMPLEMENTATION_PLAN.md`: `calc_hash` runs
  once for our own component-identity computation
  (`_build_bidi_identity_kwargs`) and again inside `enqueue()` via
  `populate_hash_if_needed`, which also fully re-serializes the protobuf
  message (`SerializeToString(deterministic=True)`) before hashing it - i.e.
  this is closer to 2 full copy+hash passes over the payload, not one, and
  entirely inside Streamlit's code path (nothing we can skip from
  `st_lonboard()` itself). **This is the single largest fixed cost per rerun
  at large N and is Streamlit-internal, not ours to optimize** - worth
  reporting upstream if it becomes a real bottleneck for users.
- **`parseContainer`/`tableFromIPC` (frontend) also scales with N** (~1ms at
  10k, ~15ms at 1M) but stays far cheaper than the Python-side hash cost -
  `tableFromIPC` is close to zero-copy (it wraps the IPC buffer's typed
  arrays directly rather than copying per value).
- **`buildDeckLayers` and `setProps` are flat and near-zero regardless of
  N** (~0.1-0.3ms even at 1M points). This confirms the GeoArrow zero-copy
  design works as intended on the frontend: constructing
  `GeoArrowScatterplotLayer` doesn't iterate per point in JS, it hands
  deck.gl typed-array views directly. The per-rerun frontend cost is
  dominated entirely by `tableFromIPC`, not layer construction.

## Determinism bug found and fixed during this baseline

While verifying the precondition for the `ForwardMsgCache` hypothesis above
(byte-identical output for byte-identical input), found that
`serialize_layer()` was *not* deterministic: arro3 builds GeoArrow extension
metadata (the CRS JSON blob) from a Rust `HashMap`, whose key iteration order
is randomized per construction. The same `GeoDataFrame` serialized twice
independently (not the same layer object, a fresh equivalent one) produced
different Arrow IPC bytes purely from `{name, metadata}` vs. `{metadata,
name}` field-metadata key order - identical content, different bytes, and
therefore a different hash. This would have silently defeated
`ForwardMsgCache` (and any content-hash cache we add ourselves) roughly 50% of
the time. Fixed in `_canonicalize_table` (sorts field metadata keys, also
drops the unused "pandas" schema-level metadata blob as a size bonus);
verified deterministic across 20 fresh constructions and across separate
process invocations. Regression test:
`test_serialize_layer_is_byte_deterministic`.

## What this means for the rest of Phase 4

- **4a (Python serialization cache)** is worth doing for exactly the
  "unchanged rerun" case measured above: at 1M points, an app with
  `return_view_state=True` (or any other frequent-rerun trigger) pays
  ~130-320ms of pure Python-side waste on every single rerun where the data
  didn't actually change, almost all of it in Streamlit's own hashing that we
  can't skip - but our own `serialize_layers`/`pack_payload` re-work (~25ms at
  1M) *is* skippable with a cache keyed on layer identity, and every ms not
  spent there is a ms less before Streamlit gets to start its (unavoidable)
  hash pass.
- **4b (frontend content-hash cache)** is lower priority than originally
  assumed for the "nothing changed" case (already free), but still matters
  for partial changes across a multi-layer map.
- **4c (benchmarks vs. `st.pydeck_chart`)** should specifically highlight the
  `mount`/hash cost at large N, since that's a fixed tax pydeck's
  JSON-based path doesn't pay in the same way (different bottleneck
  entirely - JSON encode/decode - but worth comparing where each approach's
  wall drops).

## Phase 4d: gzip compression - measured, and it's a real tradeoff, not a clear win

`st_lonboard(..., compression="auto" | "gzip" | None)`; "auto" (default)
gzips the concatenated Arrow IPC body as a single blob only above
`serialize.AUTO_COMPRESSION_THRESHOLD` (1MB raw). Measured with
`BENCH_COMPRESSION=gzip BENCH_N=1000000 uv run streamlit run benchmarks/bench_app.py`
(add `BENCH_CLUSTERED=1` for the second row):

| data           | uncompressed | gzip'd     | ratio | compress (py) | decompress (js) |
| -------------- | -----------: | ---------: | ----: | -------------: | ---------------: |
| uniform-random |   23,003,658 | 20,561,626 |  ~89% |       ~900-1030ms |            ~211ms |
| clustered (20 clusters + Gaussian jitter) | ~23,003,658 | 20,477,387 | ~89% | ~800-950ms | ~193ms |

Two things worth calling out, one a correction to the plan's assumption:

- **Clustering barely changes the ratio.** The hypothesis going in was that
  real (spatially clustered) geographic data would compress much better than
  uniform-random synthetic data. It doesn't, at least not for raw coordinate
  bytes: gzip (LZ77 + Huffman) finds compressible structure in *repeated byte
  sequences*, not numeric/spatial proximity. Per-point float64 coordinates -
  even tightly clustered ones - still have high-entropy mantissa bits once
  you add any continuous jitter, so the byte stream doesn't actually repeat
  much. Real gains on coordinate data would need a different approach
  entirely (delta/quantized encoding, spatial sorting) - general-purpose
  gzip on IEEE-754 float64 arrays has a low ceiling regardless of the data's
  real-world semantics. (uint8 color/other low-cardinality accessor columns
  likely compress better than coordinates do, since gzip *can* exploit
  repeated byte values there - not separately measured here.)
- **The CPU cost is large relative to the size win.** ~11% smaller for
  ~900ms-1s of Python-side compression plus ~200ms of JS-side decompression -
  over a full second of added latency, every rerun the data actually
  changes, to save ~2.4MB. Whether that's worth it depends entirely on the
  deployment: on a slow/high-latency connection, 2.4MB less to transfer can
  easily outweigh 1.2s of CPU (e.g. under 5 Mbps, 2.4MB alone takes ~4s to
  transfer). On localhost or a fast link - probably the common case for
  Streamlit apps - the CPU cost dominates and compression is a net loss, as
  §2 originally predicted. **This is not a settled "always compress large
  payloads" result** - it's why `compression` is a user-facing parameter
  instead of always-on, and why `"auto"`'s threshold is a starting point to
  tune, not a validated-optimal default. Users on constrained networks with
  large (1M+ point) datasets should explicitly measure both ways rather than
  trust the default.
- Small payloads correctly skip compression under `"auto"` (verified at 5,000
  points / ~98KB, well under the 1MB threshold - stays uncompressed) and
  `compression=None` correctly never compresses regardless of size (verified
  at 200,000 points / ~3.8MB). `compression="gzip"` correctly forces
  compression even for tiny payloads (verified at 3 points). All three modes
  verified end-to-end in a live browser: correct rendering, correct picking,
  no console errors.
- Exit criteria from the plan ("measured decision documented, even if the
  decision is 'not worth it'"): met. The decision is **situational** - keep
  the feature (it's a real, sometimes-large win on slow networks) but treat
  the default threshold as provisional and document the tradeoff clearly
  rather than claim compression is a general improvement.

## Phase 4c: benchmarks vs. `st.pydeck_chart` and `Map.to_html()`

Environment: dev sandbox VM, Python 3.14.6, Streamlit 1.59.1, lonboard
0.16.0, pydeck 0.9.3, pyarrow 24.0.0, Playwright 1.61.0 (Chromium, headless),
24 vCPUs / 78GB RAM, localhost transport. Single-run wall-clock numbers, not
averaged over multiple trials - order-of-magnitude comparisons, not precise
benchmarks. Reproduce with `uv run python benchmarks/playwright_driver.py`
(timing) and `uv run python benchmarks/payload_sizes.py` (wire size); both
under `uv sync --extra bench` (adds `playwright`, `pydeck` - `uv run
playwright install chromium` once).

Three contenders, all rendering the same N-point `ScatterplotLayer` (same
lon/lat, color, radius, seed - see `benchmarks/datagen.py`):
`benchmarks/contenders/lonboard_app.py` (`st_lonboard`),
`pydeck_app.py` (`st.pydeck_chart`), `tohtml_app.py` (lonboard's
`Map.to_html()` embedded via `st.components.v1.html` - the workaround people
use today without a custom component).

### Wire payload size - the one comparison all three contenders support

"Logical payload size" - the bytes of the actual data structure shipped to
the frontend (Arrow IPC frame for `st_lonboard`, `pydeck.Deck.to_json()` for
pydeck, the full HTML string for `to_html()`) - before any
transport-level framing (WebSocket, HTTP). No compression on any side.

| N points  | st_lonboard  | pydeck         | pydeck / lonboard | to_html      | to_html / lonboard |
| --------: | -----------: | -------------: | -----------------: | -----------: | ------------------: |
|    10,000 |     232,777 B|      1,921,536 B|               8.3x |    4,249,134 B|                18.3x |
|   100,000 |   2,302,778 B|     19,210,266 B|               8.3x |    8,342,785 B|                 3.6x |
| 1,000,000 |  23,003,691 B|    192,099,187 B|               8.3x |   47,589,834 B|                 2.1x |
| 10,000,000| 230,012,332 B|  1,920,982,816 B|               8.3x |  430,318,722 B|                 1.9x |

- **pydeck's JSON encoding is a consistent, flat ~8.3x larger than
  `st_lonboard`'s binary Arrow IPC, at every scale.** This isn't a surprise -
  JSON represents each float as ASCII digits (10-20 bytes) instead of 8 raw
  bytes, plus per-row key repetition - but it's useful to have the exact,
  stable multiplier measured rather than assumed. At 10M points this is the
  difference between a 230MB and a 1.9GB payload.
- **`to_html()`'s ratio shrinks from 18x to ~1.9x as N grows** - it has a
  large *fixed* cost (~3.8MB: the inlined MapLibre GL JS + deck.gl JS runtime
  bundle, present regardless of data size) that dominates at small N, but
  converges toward roughly 2x lonboard's size at large N as the actual data
  comes to dominate the fixed overhead. Its per-row encoding (Arrow IPC via
  `arro3`, base64-wrapped for inline embedding) is close to `st_lonboard`'s,
  just with ~33% base64 inflation plus the one-time JS runtime tax.

### Render/rerun timing - measured for `st_lonboard` only

Using Playwright to drive real Chromium: `time_to_first_render` (navigation
to a fresh `streamlit run` process until the map canvas appears and its
bounding box stops changing), `rerun_unchanged` (click "Rerun (unchanged
data)" until the same settle condition), `interaction_rerun` (simulate a
canvas drag, until settle again - only meaningful with
`return_view_state=True`).

| N points  | time_to_first_render | rerun (unchanged) | interaction rerun |
| --------: | --------------------: | -----------------: | ------------------: |
|    10,000 |                3,937ms|               815ms|             9,543ms |
|   100,000 |                8,862ms|               945ms|            33,872ms |
| 1,000,000 |               59,998ms|             1,443ms|           290,703ms |
| 10,000,000|                    DNF (script/canvas not ready within 90s) |

- **`time_to_first_render` includes full cold-start overhead** - launching a
  fresh `streamlit run` process, downloading the ~2.5MB frontend JS bundle,
  WASM/Arrow runtime init - not just render cost at that N. Treat the
  `rerun (unchanged)` column as the better proxy for steady-state cost: it
  excludes all of that and stays under 1.5s even at 1M points, consistent
  with the Phase 4.0 finding that CCv2 skips `mount()` entirely for
  byte-identical output (see above) - the residual cost here is Streamlit's
  own per-rerun script execution + hashing, not ours to remove.
- **`interaction_rerun` scales far worse than anything else measured in this
  project - and it's very likely measuring more than one round trip.** A
  simulated drag triggers MapLibre's pan/fly easing animation, which keeps
  firing `onViewStateChange` for a couple of seconds after mouseup; each
  event is throttled to at most one Streamlit rerun per 200ms
  (`frontend/src/index.ts`), but *each of those reruns* still re-runs
  `pack_payload()` from scratch in Python - which unconditionally re-copies
  and re-frames the (per-layer-cached, but not payload-cached) IPC bytes
  every single call, at full cost, regardless of whether the underlying
  layer changed. That cost is what scales with N here, and a multi-second
  easing animation multiplies it by however many throttled reruns fire
  during that window. **This is a real, previously-undocumented optimization
  opportunity** for a future phase: cache the fully-packed payload (not just
  the per-layer serialization) keyed on `(layers, view_state)`, or skip
  re-framing entirely when only `view_state` changed. Flagging it here
  rather than fixing it - out of scope for a benchmarking phase, and Phase 4
  is measurement-first by design.
- **10M points DNF'd**: the page's `data-test-script-state` never reached
  `"notRunning"` within the 90s startup budget. Plausibly genuine (a 230MB
  payload plus the same `pack_payload` cost above), not investigated further
  in this pass - if this matters for a real deployment, it's the first place
  to look.
- pydeck and `to_html()` have **no equivalent metric for `interaction_rerun`
  at all** - neither ever sends a view-state change back to Python (pydeck's
  `st.pydeck_chart` has no such callback; `to_html()`'s output has no
  Python<->JS channel whatsoever). This is architectural, not a limitation of
  this benchmark: panning either of those costs Streamlit **nothing**, in
  exchange for never being able to react to it server-side.

### pydeck: not reliably measurable under headless Chromium automation here

`st.pydeck_chart` renders correctly under normal interactive use (confirmed
manually via a real browser session earlier in this project). Under
Playwright's headless Chromium automation in this environment, though, it
hit a reproducible stall: `page.locator("canvas")` never finds a stable
canvas within the 90s startup budget, and even a trivial `page.evaluate(()
=> 1+1)` call can hang indefinitely a couple of seconds after the page
loads. Console logs show `GL Driver Message ... GPU stall due to
ReadPixels` warnings around the same time, consistent with deck.gl's WebGL
picking readback stalling the renderer's main thread under this
environment's (sandboxed, likely software-rendered) GPU path. This is
reported as an **environment-specific limitation of automated headless
measurement**, not a claim that pydeck itself is broken - and it's exactly
why `playwright_driver.py`'s measurements run each `(contender, N)` in its
own subprocess with a hard wall-clock timeout (see the module docstring):
without that boundary, one stalled pydeck measurement would hang the entire
benchmark run indefinitely. Not investigated further past confirming the
stall is reproducible and bounded.

### `Map.to_html()`: a hard, scale-independent DNF when embedded

Unlike pydeck, this isn't an automation artifact - `Map.to_html()`'s output
**never renders at all** when embedded via `st.components.v1.html`, at any
scale, confirmed by direct inspection rather than timing:

- The same `to_html()` output, served standalone (outside any iframe), earlier renders correctly (2
  canvases, confirmed with Playwright).
- Embedded via `components.v1.html`, only require.js and the
  `@jupyter-widgets/html-manager` bootstrap script ever load; the actual
  widget bundle, the anywidget runtime, and the WASM parquet reader never
  fire a single network request, and no error is surfaced anywhere - the map
  area just stays blank forever.
- Root cause, confirmed directly: `document.baseURI` inside the sandboxed
  `srcdoc` iframe Streamlit uses correctly resolves to the parent page's real
  URL, but `document.location.href` is the opaque literal string
  `"about:srcdoc"`. anywidget/require.js's dynamic module loader apparently
  depends on `location.href` (not `baseURI`) to resolve the URLs of
  subsequently-loaded modules, which breaks unconditionally inside *any*
  `srcdoc`-based iframe embedding - not specific to Streamlit, but Streamlit
  is exactly what `components.v1.html` uses.

This means "just call `Map.to_html()` and embed it" - the workaround this
project exists to replace - **doesn't work at all** for lonboard's current
(anywidget-based) export format, for any dataset size. `playwright_driver.py`
records this as a fixed DNF for every scale rather than attempting to time
something that never renders.

### Summary

- **Payload size**: `st_lonboard`'s binary format is a measured, consistent
  ~8.3x smaller than pydeck's JSON at every scale tested (10k-10M points).
- **Render/rerun timing**: only fully measurable for `st_lonboard` in this
  environment; steady-state reruns stay sub-1.5s through 1M points, but
  interaction-triggered reruns (`return_view_state=True`) reveal a real,
  scale-dependent cost worth optimizing in a future phase (see above).
- **pydeck**: works interactively, but its headless-automation profile in
  this specific sandboxed environment made timing unreliable - reported
  honestly as an environment limitation rather than forced or extrapolated.
- **`Map.to_html()` embedded via `components.v1.html`**: doesn't render at
  all, at any scale - a hard architectural gap in the workaround this
  project replaces, not a performance finding.

## Phase 4g: skipping the ipywidgets comm at layer construction

Recorded with `benchmarks/bench_widget_init.py` on an AMD Ryzen AI 9 HX 370
(24 threads, 78 GB RAM, Fedora 6.17.8, Python 3.14.6, lonboard 0.16.0,
ipywidgets 8.1.8). No Streamlit and no browser involved — this is pure Python
layer-construction cost. Best of 5 per cell, single session; the two scenarios
were measured back-to-back in the same process.

How to reproduce: `uv run python benchmarks/bench_widget_init.py 200000`.

| scenario (200,000 rows) | stock | patched | speedup |
| --- | --- | --- | --- |
| `A5Layer` (pre-built Arrow table) | 62.1 ms | 0.6 ms | 98x |
| `ScatterplotLayer.from_geopandas` | 134.9 ms | 76.8 ms | 2x |

The two rows bracket what to expect. `A5Layer` receives an Arrow table that is
already built, so essentially *all* of its construction time was the discarded
comm message — removing it leaves almost nothing. `from_geopandas` additionally
converts a GeoDataFrame to Arrow and reprojects it, which the patch cannot
remove; roughly half the time was comm overhead. Real apps land between the two
depending on how much of the pipeline they do inside lonboard.

The saving scales with table size and accessor count, because the work removed
is a ZSTD-7 Parquet encode of the table plus every accessor column. On the
dashboard that prompted this (A5 level 9, 359,600 cells, several accessors) the
measured construction cost was ~0.76 s per layer, paid on every rerun that did
not hit `@st.cache_resource`.

Excluded from these numbers: serialization by `st_lonboard()` itself (unchanged
— the payload is byte-identical with and without the patch, asserted in
`tests/test_widget_patch.py`), and any browser-side cost.

## Phase 4f: colour-only reruns

Recorded with `benchmarks/bench_recolor_app.py` driven by
`benchmarks/playwright_driver.py --recolor`, on an AMD Ryzen AI 9 HX 370 (24
threads, 78 GB RAM, Fedora 6.17.8, Python 3.14.6), headless Chromium 149 pinned
to the SwiftShader software GL path. Single run per cell, not averaged.

The metric is **main-thread longtask total** — the sum of `PerformanceObserver`
`longtask` entries during the rerun — not wall clock. Wall clock also contains
Python re-serialization, transport, and GPU time; what a user experiences as "the
tab froze" is main-thread blocking, and that is what this phase set out to remove.

How to reproduce:

```
uv pip install h3
uv run python benchmarks/playwright_driver.py --recolor h3,polygon,scatterplot --scales 160000
```

| layer | cells | first render | **recolour only** | new geometry |
| --- | --- | --- | --- | --- |
| `h3` (high precision) | 160,000 | 14,530 ms | **0 ms** | 2,120 ms |
| `h3` (high precision) | 40,000 | 1,017 ms | **0 ms** | 544 ms |
| `polygon` (GeoArrow geometry) | 40,000 | 701 ms | **0 ms** | 220 ms |
| `scatterplot` (control) | 160,000 | 461 ms | **0 ms** | 0 ms |

"New geometry" is the control that matters most: it must stay expensive. A
recolour that costs nothing is only correct if a genuine geometry change still
rebuilds — otherwise the map has quietly gone stale.

### Measuring this correctly

Two methodology traps produced badly wrong numbers before the table above:

- **Waiting for the canvas is not waiting for the render.** The existing
  `_wait_canvas_settled` only watches the canvas bounding box, which stabilises as
  soon as the element is sized — while deck.gl still has seconds of tessellation
  ahead of it. Timing a rerun from that point charges the *previous* render's tail
  to it, which made a genuinely free recolour measure at 12.7 s.
  `_wait_main_thread_quiet` now waits for the longtask stream itself to go quiet.
- **Headless Chromium's default GL path varies.** Left unpinned it picked a
  fallback roughly 8x slower here, enough to swamp the effect being measured. The
  driver now passes explicit SwiftShader flags.

### A5, measured directly in the browser

A5 is the family that motivated this work (a 158,927-cell level-9 choropleth cost
~8.9 s per colour change) but has no published Python bindings — only the `a5-js`
that deck.gl itself uses — so it cannot be driven from a Streamlit app here. It was
measured instead in a standalone page against the same deck.gl/geoarrow versions,
instrumented to count `cellToBoundary` calls and tessellator rebuilds directly:

| phase, 160,000 A5 cells | wall | longtask | `cellToBoundary` calls | tessellations |
| --- | --- | --- | --- | --- |
| initial render | 9,462 ms | 9,459 ms | 480,000 | 1 |
| **recolour (same geometry)** | **37 ms** | **0 ms** | **0** | **0** |
| geometry changed | 14,997 ms | 14,989 ms | 480,000 | 1 |

The 9.5 s initial render reproduces the originally reported ~8.9 s. An A/B at
20,000 cells with the mechanism disabled gives the direct before/after: recolour
costs 1,067 ms of longtask and 60,000 `cellToBoundary` calls without it, 0 ms and
0 calls with it — and the two runs produced **identical canvas hashes**, so the
saving is pixel-for-pixel free. Picking still resolved to the correct row index
after a recolour in both cases.

### Why the benchmark forces `high_precision=True` on H3

deck.gl's `H3HexagonLayer` defaults to `highPrecision: "auto"`, which instances a
single hexagon geometry through `ColumnLayer` instead of polygonizing each cell.
That path is cheap enough that 160,000 cells cost only ~0.6 s and the problem never
appears at all. A5, S2 and geohash have no equivalent fast path, which is precisely
why they were the families that hurt. Benchmarking H3 in its default mode would
have measured the wrong thing.

Excluded from these numbers: Python-side serialization (unchanged by this phase),
and the byte-identical rerun path, where Streamlit's `ForwardMsgCache` means the
frontend's `mount()` is never invoked (measured as 0 ms here too, and verified as
still 0 ms after this change).

## Phase 4h: wire-format shootout - four `compression=` options measured

`st_lonboard(compression=...)` now supports four wire formats (see
`serialize.BodyEncoding`):

- `None` / `"auto"` / `"gzip"`: plain Arrow IPC per layer; gzip modes apply
  whole-body gzip (level 6) on top - unchanged from Phase 4d.
- `"zstd"` (new): Arrow IPC with the IPC spec's own per-buffer ZSTD
  compression (`IpcWriteOptions(compression="zstd")`). Decompressed
  transparently *inside* the frontend's `tableFromIPC` by a ZSTD codec
  registered with arrow-js's `compressionRegistry` (`fzstd`, a ~8KB pure-JS
  decoder). Needed apache-arrow 17 -> 21 in the frontend (the registry API
  landed after the arrow-js repo split); `@geoarrow/deck.gl-geoarrow`
  peer-accepts `>=15`, so no other frontend changes.
- `"parquet"` (new): each layer's table shipped as a complete in-memory
  Parquet file - ZSTD data pages, BYTE_STREAM_SPLIT encoding on numeric
  leaf columns, dictionary encoding kept for string/binary leaves - decoded
  in the browser by parquet-wasm (v0.7.2) back into Arrow, then parsed by
  the same `tableFromIPC` path as everything else.

Environment: same dev sandbox VM as Phase 4c (24 vCPUs/78GB, Python 3.14.6,
pyarrow 24.0.0, Streamlit 1.59.x, apache-arrow JS 21.2.0, headless Chromium
via Playwright, localhost transport). Reproduce with
`uv run python benchmarks/wire_options.py` (add `--skip-browser` for Part 1
only; browser rows drive `bench_app.py` with `BENCH_COMPRESSION=<mode>` and
read the ST_LONBOARD_PERF console table for a forced fresh-payload rerun).
Data is `datagen.points_geodataframe`: uniform-random float64 points + uint8
r/g/b + float32 radius - close to the *worst case* for every compressor
(high-entropy mantissas; see the Phase 4d clustering note, which applies
here unchanged).

### Part 1: wire size + Python-side encode cost (median of 3)

`encode_ms` = `_encode_table` alone (table -> body bytes for that mode,
excluding table building, which is identical across modes).
`+gzip_ms` = the whole-body gzip pass (gzip mode only).

|         N | mode    | wire bytes | vs. none | encode_ms | +gzip_ms |
| --------: | ------- | ---------: | -------: | --------: | -------: |
|    10,000 | none    |    232,777 |     1.00 |      ~0.0 |        - |
|    10,000 | gzip    |    208,039 |     0.89 |      ~0.0 |      6.8 |
|    10,000 | zstd    |    213,069 |     0.92 |       0.6 |        - |
|    10,000 | parquet |    188,573 |     0.81 |       1.9 |        - |
|   100,000 | none    |  2,302,778 |     1.00 |       0.2 |        - |
|   100,000 | gzip    |  2,058,095 |     0.89 |       0.1 |     69.6 |
|   100,000 | zstd    |  2,104,542 |     0.91 |       2.8 |        - |
|   100,000 | parquet |  1,820,569 |     0.79 |      13.6 |        - |
| 1,000,000 | none    | 23,002,779 |     1.00 |       3.5 |        - |
| 1,000,000 | gzip    | 20,558,620 |     0.89 |       4.1 |    696.8 |
| 1,000,000 | zstd    | 21,017,887 |     0.91 |      28.8 |        - |
| 1,000,000 | parquet | 18,153,922 |     0.79 |     119.5 |        - |

Reading this table:

- **ZSTD-IPC buys (almost) gzip's ratio at ~1/24th the CPU.** At 1M points:
  9% smaller for 29ms, vs. gzip's 11% for 697ms. The per-buffer ZSTD write
  happens inside pyarrow's C++ IPC writer; Python's `gzip` module was the
  bottleneck of the old path, not the idea of compressing.
- **Parquet only beats raw IPC because of BYTE_STREAM_SPLIT.** With
  pyarrow's default dictionary+PLAIN encodings, the 100k Parquet payload
  measured *larger* than uncompressed IPC (2.57MB vs 2.30MB - random
  float64 mantissas don't dictionary- or ZSTD-compress, uint8 colors are
  stored as INT32, and list rep/def levels add overhead). BYTE_STREAM_SPLIT
  (regrouping float bytes by significance before ZSTD) is what gets Parquet
  to 0.79x - it's the only encoding in this shootout that engages with
  float structure at all, which is also why it's the only mode that would
  gain much more on real-world (lower-entropy) coordinates.
- **All three compressed modes stay byte-deterministic** (asserted in
  `test_new_encodings_are_byte_deterministic`), so Streamlit's
  ForwardMsgCache dedup and our layer cache keep working; the layer cache
  now keys on encoding too (`serialize_layer_cached(..., encoding)`).

### Part 2: browser-side decode cost

Measured on a forced fresh-payload rerun (so the WASM module and page are
already warm - parquet's one-time WASM instantiation is NOT in these
numbers). `decode` = mode-specific decompression (whole-body gzip /
parquet-wasm) + `tableFromIPC` (which for zstd *includes* the per-buffer
ZSTD decompression, via the registered codec). `mount` = everything through
deck.gl layer construction. All renders verified visually per (mode, N) via
saved screenshots - the four 1M-point maps are pixel-identical.

|         N | mode    | decode ms | mount ms |
| --------: | ------- | --------: | -------: |
|    10,000 | none    |       1.2 |      2.3 |
|    10,000 | gzip    |      11.3 |     14.1 |
|    10,000 | zstd    |       5.5 |      6.6 |
|    10,000 | parquet |       7.9 |     30.5 |
|   100,000 | none    |       2.0 |     50.1 |
|   100,000 | gzip    |      22.6 |     71.2 |
|   100,000 | zstd    |      28.4 |     75.4 |
|   100,000 | parquet |      18.9 |     79.2 |
| 1,000,000 | none    |      12.2 |    454.7 |
| 1,000,000 | gzip    |     200.3 |    700.1 |
| 1,000,000 | zstd    |     261.4 |    708.4 |
| 1,000,000 | parquet |     155.9 |    762.6 |

Reading this table:

- **Uncompressed IPC's near-zero-copy story holds**: 12ms to parse 23MB
  into a usable Arrow table at 1M points. Every compressed mode pays
  10-20x that to decode.
- **Parquet decodes *fastest* of the three compressed modes at scale**
  (156ms vs gzip's 200ms vs zstd's 261ms at 1M) - parquet-wasm's Rust ZSTD
  + BYTE_STREAM_SPLIT reassembly runs at native WASM speed, beating even
  the browser's native `DecompressionStream("gzip")`.
- **ZSTD-IPC's decode is the slowest, and it's an implementation artifact,
  not inherent**: the registered codec is `fzstd`, a pure-JS decoder, plus
  an alignment copy (see below). Parquet's own WASM ZSTD path proves
  ~150ms is achievable for the same data; swapping the codec for a WASM
  ZSTD build (e.g. zstd-codec, at the cost of its download size) would
  close most of the 100ms gap. At 100k and below the difference is noise.

### End-to-end at 1M points: added CPU vs. bytes saved (vs. `None`)

| mode    | added encode+decode | bytes saved | break-even link speed |
| ------- | ------------------: | ----------: | --------------------: |
| gzip    |              ~886ms |      2.44MB |              ~22 Mbps |
| zstd    |              ~275ms |      1.98MB |              ~58 Mbps |
| parquet |              ~260ms |      4.85MB |             ~150 Mbps |

("Break-even link speed": the bandwidth below which the transfer-time
saving exceeds the added CPU. Above it, `None` is faster end-to-end.)

**Parquet dominates gzip and zstd at this scale on both axes at once** -
more bytes saved AND less total CPU - so among the compressed modes the
choice at 1M is really parquet vs. nothing, with zstd as the
no-WASM-download middle ground and gzip now strictly dominated (kept for
compatibility). Remember this is worst-case (uniform-random float64) data:
on real clustered/quantized data every ratio improves, and parquet's
BYTE_STREAM_SPLIT + dictionary encodings improve fastest.

### Costs not in the tables

- **`"parquet"` adds a one-time ~1.8MB WASM download** (parquet-wasm;
  6.5MB on disk, but Streamlit's gzip middleware covers the
  `_stcore/bidi-components/` asset route - measured 1,908,319 bytes
  `encodedBodySize` in a real browser). Fetched lazily on the first parquet
  payload only, then browser-cached - apps that never ship a parquet layer
  don't pay it at all. It is deliberately
  NOT base64-inlined into index.js (a vite lib-mode default we had to
  disable twice: once via a non-literal URL in `src/parquet.ts`, once by
  stripping the glue's own `new URL(...)` fallback in `vite.config.ts`).
- **The arrow-js upgrade + fzstd + parquet-wasm JS glue grew index.js
  ~2.5MB -> ~3.2MB** (minified; the WASM stays external).
- **arrow-js's compressed-IPC reader needs an alignment workaround**: codec
  output is wrapped directly in typed arrays, which require 8-byte
  alignment, but `fzstd` returns views at arbitrary offsets - the
  registered codec copies to a fresh buffer when misaligned (see
  container.ts). Worth rechecking on arrow-js upgrades.
- **Multi-map perf-mark hazard fixed in passing**: measures that span an
  `await` (gzip decompress, parquet decode, mount) now use timestamp-based
  `performance.measure` - named marks get wiped by another concurrently
  mounting map's `clearMarks()` (surfaced as a real crash with 4 maps on
  one page in `benchmarks/wire_verify_app.py`).

### Which mode to pick

- **localhost / fast LAN**: `None` (or the `"auto"` default) - decode is
  near-zero-copy and transfer is free; every compressed mode only adds CPU.
- **remote server (links up to ~150 Mbps), data at 100k+ points**:
  `"parquet"` - the best ratio (0.79x on worst-case data, better on real
  data) *and* the cheapest compressed decode, at the price of a one-time
  ~1.8MB (gzipped) WASM download on first render.
- **remote server, want zero extra frontend weight**: `"zstd"` - ~24x
  cheaper to encode than gzip for nearly the same ratio, no WASM, no
  bundle-relevant additions (fzstd is ~8KB); its decode-side gap vs.
  parquet is an artifact of the pure-JS codec and closable later.
- **`"gzip"`**: now strictly dominated by both new modes at every scale
  measured (worse CPU than zstd, worse ratio than parquet) - kept as the
  Phase 4d-era option, but there's no configuration where it wins.
- A representation-level change (float32/quantized coordinates - deck.gl
  renders float32 anyway) would halve the dominant buffer *before* any of
  this and remains the biggest untapped lever (not prototyped here).

## Phase 4i: wire formats on real data - an Arctic EO cube on the A5 DGGS

Same four `compression=` modes as Phase 4h, but on real data:
`/home/tobias/Data/eo-analysis-ready/a5` (machine-local; override with
`A5_DATA_ROOT`), an analysis-ready Arctic EO cube aggregated onto A5 cells
(zarr v3, one group per level L06-L09, one array per per-cell statistic,
plus a GeoParquet index with each cell's pentagon boundary). Reproduce with
`uv run python benchmarks/wire_options_real.py` (scenarios defined in
`benchmarks/real_scenarios.py`, browser rows via `bench_real_app.py`).

Two scenarios bracket how real payloads differ from Phase 4h's
uniform-random synthetic data:

- **a5-cells** - what a dashboard on this data actually ships: uint64 A5
  cell ids (`get_pentagon`; geometry derived in-browser, no coordinates on
  the wire), a uint8 RGB color accessor from ArcticDEM elevation, and six
  float32 per-cell DEM statistics as tooltip columns.
- **pentagons** - the same cells shipped the "classic" way: real GeoArrow
  polygon geometry (pentagon boundary rings, float64 vertices) + the same
  color accessor and one tooltip column.

### Part 1: wire size + Python-side encode cost (median of 3)

| scenario  | level |       N | mode    | wire bytes | vs. none | encode_ms | +gzip_ms |
| --------- | ----- | ------: | ------- | ---------: | -------: | --------: | -------: |
| a5-cells  | L07   |  22,722 | none    |    796,786 |     1.00 |       0.1 |        - |
| a5-cells  | L07   |  22,722 | gzip    |    196,952 |     0.25 |       0.1 |     20.4 |
| a5-cells  | L07   |  22,722 | zstd    |    189,846 |     0.24 |       1.2 |        - |
| a5-cells  | L07   |  22,722 | parquet |    165,936 |     0.21 |       5.8 |        - |
| a5-cells  | L08   |  90,124 | none    |  3,155,859 |     1.00 |       0.2 |        - |
| a5-cells  | L08   |  90,124 | gzip    |    711,904 |     0.23 |       0.1 |     75.1 |
| a5-cells  | L08   |  90,124 | zstd    |    690,734 |     0.22 |       3.8 |        - |
| a5-cells  | L08   |  90,124 | parquet |    578,363 |     0.18 |      13.5 |        - |
| a5-cells  | L09   | 358,599 | none    | 12,552,508 |     1.00 |       4.4 |        - |
| a5-cells  | L09   | 358,599 | gzip    |  2,720,435 |     0.22 |       7.8 |    335.2 |
| a5-cells  | L09   | 358,599 | zstd    |  2,619,359 |     0.21 |      15.2 |        - |
| a5-cells  | L09   | 358,599 | parquet |  2,083,221 |     0.17 |      48.3 |        - |
| pentagons | L07   |  22,722 | none    |  4,367,973 |     1.00 |       0.4 |        - |
| pentagons | L07   |  22,722 | gzip    |  2,048,277 |     0.47 |       0.7 |     78.7 |
| pentagons | L07   |  22,722 | zstd    |  1,945,841 |     0.45 |      14.3 |        - |
| pentagons | L07   |  22,722 | parquet |  1,779,822 |     0.41 |      25.9 |        - |
| pentagons | L08   |  90,124 | none    | 17,146,718 |     1.00 |       2.1 |        - |
| pentagons | L08   |  90,124 | gzip    |  8,005,521 |     0.47 |       3.0 |    276.5 |
| pentagons | L08   |  90,124 | zstd    |  7,683,721 |     0.45 |      54.6 |        - |
| pentagons | L08   |  90,124 | parquet |  7,004,893 |     0.41 |     100.6 |        - |

Reading this table (against Phase 4h's worst-case ratios of 0.89-0.79):

- **Real data compresses 3-4x better than the synthetic benchmark
  suggested.** a5-cells payloads shrink to 0.17-0.25x (vs. 0.79-0.91x
  synthetic): sorted uint64 cell ids delta-compress well under any LZ,
  spatially-correlated float32 statistics repeat aggressively, and NaN
  no-coverage runs are free. Even real polygon *coordinates* - the
  synthetic benchmark's worst case - reach 0.41-0.47x, because neighboring
  pentagons share vertex byte patterns.
- **The mode ranking survives contact with real data** - parquet smallest,
  zstd a close second at a fraction of gzip's encode cost - but the gaps
  compress: on a5-cells, zstd already captures ~95% of gzip's ratio for
  ~1/20th the CPU, and parquet's extra ~4-5 size points cost 3-4x zstd's
  encode time.
- **BYTE_STREAM_SPLIT needed a policy fix for real geometry** (this data
  caught it; the synthetic benchmark could not have): applying BSS to
  *deeply-nested* coordinate leaves (Polygon rings, rep level > 1) made
  Parquet dramatically WORSE - 3.10MB vs. 1.79MB written plain at
  pentagons/L07 - because BSS's byte transposition destroys the repeated
  vertex byte sequences ZSTD exploits directly. `_table_to_parquet_bytes`
  now applies BSS only to flat/point-like numeric leaves
  (max_repetition_level <= 1) and writes deeper coordinate leaves plain;
  point coordinates (the synthetic case) keep BSS and its ~21% win.

### Part 2: browser-side decode cost

Same methodology as Phase 4h Part 2 (forced fresh-payload rerun, WASM
already warm; `decode` = mode-specific decompression + `tableFromIPC`,
which includes zstd's per-buffer decompression). Every (scenario, level,
mode) cell verified rendering via screenshot.

| scenario  | level | mode    | decode ms | mount ms |
| --------- | ----- | ------- | --------: | -------: |
| a5-cells  | L07   | none    |       1.5 |      6.1 |
| a5-cells  | L07   | gzip    |       5.0 |      9.5 |
| a5-cells  | L07   | zstd    |       8.7 |     12.0 |
| a5-cells  | L07   | parquet |       6.7 |      9.9 |
| a5-cells  | L08   | none    |       6.1 |     17.6 |
| a5-cells  | L08   | gzip    |      13.8 |     24.4 |
| a5-cells  | L08   | zstd    |      69.7 |     74.7 |
| a5-cells  | L08   | parquet |      18.5 |     22.8 |
| a5-cells  | L09   | none    |       6.3 |     40.6 |
| a5-cells  | L09   | gzip    |      47.7 |     81.7 |
| a5-cells  | L09   | zstd    |     155.1 |    164.4 |
| a5-cells  | L09   | parquet |      66.9 |     74.6 |
| pentagons | L07   | none    |       2.5 |     16.7 |
| pentagons | L07   | gzip    |      28.2 |     41.0 |
| pentagons | L07   | zstd    |      70.9 |     78.5 |
| pentagons | L07   | parquet |      32.8 |     39.8 |
| pentagons | L08   | none    |       6.9 |     49.9 |
| pentagons | L08   | gzip    |      84.5 |    127.3 |
| pentagons | L08   | zstd    |     209.9 |    231.3 |
| pentagons | L08   | parquet |     117.4 |    137.4 |

The Phase 4h decode ranking holds on real data at every scale: raw IPC
near-free, parquet the fastest compressed decode... except gzip, whose
native `DecompressionStream` pulls ahead of parquet here because real-data
payloads are so much smaller than synthetic ones that WASM-call overhead
matters relatively more. zstd's pure-JS `fzstd` decode stays the slowest -
again an artifact of the codec choice, not the format (see Phase 4h).

### End-to-end at a5-cells/L09 (358.6k cells): added CPU vs. bytes saved (vs. `None`)

| mode    | added encode+decode | bytes saved | break-even link speed |
| ------- | ------------------: | ----------: | --------------------: |
| gzip    |              ~380ms |      9.83MB |             ~210 Mbps |
| zstd    |              ~160ms |      9.93MB |             ~500 Mbps |
| parquet |              ~105ms |     10.47MB |             ~800 Mbps |

Compare Phase 4h's synthetic break-evens (22/58/150 Mbps): on real data
every compressed mode pays for itself on links an order of magnitude
faster, i.e. **on real data, compression wins on essentially any
non-localhost connection** - and parquet again dominates on both axes at
once (most bytes saved, least CPU added).

### Which mode to pick, revised for real data

- The economics tilt much further toward compressing than the synthetic
  numbers implied: on a5-cells@L09, `"zstd"` removes ~9.9MB (79%) of a
  12.6MB payload for ~15ms of Python and well under 200ms of browser
  decode - worth it on almost any non-localhost link. The synthetic
  benchmark's "compression barely pays" framing was an artifact of
  uniform-random coordinates.
- `"parquet"` stays the ratio winner (0.17-0.21x on a5-cells) and its
  decode stays competitive; the choice vs. `"zstd"` remains "best ratio +
  ~1.8MB one-time WASM" vs. "near-free encode, no extra downloads".
- DGGS-style payloads (`a5-cells`) are the best case for *every* mode:
  if you can ship cell ids instead of boundary polygons, the payload is
  already 4-5x smaller before compression enters the picture (797KB vs.
  4.4MB at L07), and compresses harder on top.

## Phase 4j: Parquet becomes the default (`compression="auto"`)

Given Phase 4h/4i, `"auto"` - the default - no longer means "plain IPC, gzip
the whole body above 1MB". It now resolves **per layer**:

| layer's `table.nbytes` | encoding shipped |
| ---------------------- | ---------------- |
| < `AUTO_COMPRESSION_THRESHOLD` (1MB) | `ipc` (unchanged, plain Arrow IPC) |
| >= threshold | `parquet` (ZSTD + BYTE_STREAM_SPLIT policy) |

Why parquet rather than zstd for the default, given zstd needs no WASM: on
every scale measured, parquet is both smaller *and* cheaper to decode than
zstd, so the only thing weighing against it is the one-time WASM download.
That turned out to be much smaller than assumed - **1.8MB, not 6.5MB**:
Streamlit's gzip middleware bypasses only `/static/`, and component assets
are served from `_stcore/bidi-components/`, so the binary ships compressed
(measured `encodedBodySize` 1,908,319 in a live browser) and is then
browser-cached. Against a5-cells@L09's 10.47MB saved per rerun that
amortizes on the *first* payload; even against zstd (the next best mode) it
breaks even after ~3 reruns.

Design notes:

- **The decision is per layer, not per payload**, because it depends on a
  layer's own table size. `pack_payload` therefore emits `bodyEncoding` in
  each *layer* header rather than once at the container level, and one map
  can legitimately mix a Parquet-encoded 2M-row layer with a plain-IPC
  500-row one (`test_pack_payload_compression_auto_mixes_encodings_across_layers`).
- **`SerializedLayer` records both the request and the resolution**
  (`encoding_request` = what `compression=` asked for, `encoding` = what
  this layer got). The cache keys on the *request*, so a cache hit still
  costs no `layer.table` access, and `pack_payload` validates the request
  matches its own `compression=` - catching stale-cache/mismatch bugs
  loudly instead of shipping mis-decodable bytes.
- **Sub-threshold payloads are byte-identical to before** (no
  `bodyEncoding` key at all), so small apps see no wire change and, more
  importantly, never trigger the parquet-wasm fetch.
- **Localhost caveat unchanged**: `"auto"` cannot know the link speed, so a
  large payload served over localhost now pays parquet encode/decode for
  transfer that was free. That is still strictly better than the gzip
  `"auto"` used to pick there (Phase 4d), but `compression=None` remains
  the right explicit choice for purely local use.

Verified end-to-end in a real browser (`benchmarks/wire_verify_app.py`, now
including an `"auto"` map at 100k points): all five modes render
identically, the WASM is fetched exactly once, and the console stays clean.
