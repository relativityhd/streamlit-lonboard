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
