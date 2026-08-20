"""Wire-format shootout: the four `compression=` options head-to-head.

Usage:
    uv run python benchmarks/wire_options.py                     # everything
    uv run python benchmarks/wire_options.py --skip-browser      # Part 1 only
    uv run python benchmarks/wire_options.py --scales 100000 --out results.json

Compares `compression=None | "gzip" | "zstd" | "parquet"` on the same
N-point scatterplot (datagen.points_geodataframe: point geometry + uint8
r/g/b color accessor + float32 radius accessor):

Part 1 (in-process, no browser):
- encode_ms: median time to turn the (already-built, canonicalized) Arrow
  table into that mode's per-layer body bytes (`_encode_table`) - the
  marginal Python-side cost of the mode itself, excluding table building.
- outer_compress_ms: gzip only - the whole-body gzip pass in `pack_payload`.
- wire_bytes: the full framed payload actually shipped.

Part 2 (playwright, real Chromium): drives benchmarks/bench_app.py with
BENCH_COMPRESSION=<mode>, clicks "Rerun (new data)" (forces a genuinely
different payload so a real mount() happens), and reads the frontend's
ST_LONBOARD_PERF console table for that mount:
- decompress / parquetDecode / tableFromIPC / parseContainer / mount (ms)
Also saves a screenshot per (mode, N) as render proof.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import socket
import statistics
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from datagen import points_geodataframe  # noqa: E402
from payload_sizes import _load_serialize_module  # noqa: E402

from lonboard import ScatterplotLayer  # noqa: E402

serialize = _load_serialize_module()

BENCH_APP = Path(__file__).parent / "bench_app.py"
MODES: list[tuple[str, str | None]] = [
    ("none", None),
    ("gzip", "gzip"),
    ("zstd", "zstd"),
    ("parquet", "parquet"),
]
DEFAULT_SCALES = [100_000, 1_000_000]
ENCODE_REPEATS = 3
STARTUP_TIMEOUT_S = 120
RERUN_TIMEOUT_S = 180


def _build_layer(n: int) -> ScatterplotLayer:
    gdf = points_geodataframe(n)
    return ScatterplotLayer.from_geopandas(
        gdf,
        get_fill_color=gdf[["r", "g", "b"]].to_numpy(),
        get_radius=gdf["radius"].to_numpy(),
        radius_units="meters",
        pickable=True,
    )


def measure_python(n: int) -> list[dict]:
    return measure_python_layer(_build_layer(n), n=n)


def measure_python_layer(layer, *, n: int, tooltip=False, extra: dict | None = None) -> list[dict]:
    """Part-1 measurement for an arbitrary prebuilt lonboard layer.

    `extra` is merged into every result row (e.g. a scenario label for the
    real-data benchmark, see wire_options_real.py)."""
    rows = []
    for mode_name, compression in MODES:
        encoding = serialize.body_encoding_for(compression)
        serialized = serialize.serialize_layer(layer, "layer-0", tooltip=tooltip, encoding=encoding)

        times = []
        for _ in range(ENCODE_REPEATS):
            t0 = time.perf_counter()
            serialize._encode_table(serialized.table, encoding)
            times.append((time.perf_counter() - t0) * 1000)

        outer_ms = None
        if compression == "gzip":
            raw = serialized.body_bytes
            outer_times = []
            for _ in range(ENCODE_REPEATS):
                t0 = time.perf_counter()
                serialize._compress_body(raw, "gzip")
                outer_times.append((time.perf_counter() - t0) * 1000)
            outer_ms = round(statistics.median(outer_times), 1)

        payload = serialize.pack_payload([serialized], view_state=None, map_options={}, compression=compression)
        rows.append(
            {
                **(extra or {}),
                "n": n,
                "mode": mode_name,
                "encode_ms": round(statistics.median(times), 1),
                "outer_compress_ms": outer_ms,
                "wire_bytes": len(payload),
            }
        )
        print(json.dumps(rows[-1]), file=sys.stderr)
    return rows


# ---------------------------------------------------------------------------
# Part 2: browser decode timing
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def _bench_app(n: int, mode_name: str, port: int, *, script: Path = BENCH_APP, extra_env: dict | None = None):
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(script),
            "--server.headless",
            "true",
            "--server.port",
            str(port),
        ],
        env={
            **os.environ,
            "BENCH_N": str(n),
            "BENCH_COMPRESSION": mode_name,
            "ST_LONBOARD_PERF": "1",
            **(extra_env or {}),
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        url = f"http://127.0.0.1:{port}/_stcore/health"
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"streamlit exited early:\n{proc.stderr.read() if proc.stderr else ''}")
            try:
                with urllib.request.urlopen(url, timeout=1) as resp:
                    if resp.status == 200:
                        break
            except OSError:
                pass
            time.sleep(0.3)
        else:
            raise TimeoutError("streamlit never became healthy")
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def measure_browser(
    n: int,
    mode_name: str,
    screenshot_dir: Path | None,
    *,
    script: Path = BENCH_APP,
    extra_env: dict | None = None,
    shot_label: str = "wire",
) -> dict:
    from playwright.sync_api import sync_playwright

    port = _free_port()

    # console.table's data is captured by a hook installed *inside the page*
    # (see below) and read back with a plain evaluate. The obvious
    # alternative - page.on("console") + msg.args[0].json_value() - resolves
    # the argument over a CDP remote-object handle, which silently fails
    # under heavy page load (confirmed with a diagnostic run: the table
    # event fires every time, but json_value() on a busy 17MB-payload page
    # intermittently returns nothing).
    # NOTE: add_init_script takes raw JS *source* (executed as-is at
    # document start), not a function - hence the IIFE.
    HOOK = """
    (() => {
      if (window.__stPerfTables) return;
      window.__stPerfTables = [];
      const orig = console.table.bind(console);
      console.table = (data) => {
        try {
          if (Array.isArray(data) && data.length && data[0].span !== undefined) {
            window.__stPerfTables.push(JSON.parse(JSON.stringify(data)));
          }
        } catch (e) {}
        orig(data);
      };
    })();
    """

    with _bench_app(n, mode_name, port, script=script, extra_env=extra_env) as base_url, sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.add_init_script(HOOK)
        try:
            page.goto(base_url, wait_until="domcontentloaded")
            page.wait_for_function(
                "() => document.querySelector('[data-testid=\"stApp\"]')"
                "?.getAttribute('data-test-script-state') === 'notRunning'",
                timeout=STARTUP_TIMEOUT_S * 1000,
            )
            page.locator("canvas").first.wait_for(state="visible", timeout=STARTUP_TIMEOUT_S * 1000)

            def table_count() -> int:
                return page.evaluate("() => (window.__stPerfTables || []).length")

            # Force a *fresh* payload so the frontend does a full parse (the
            # unchanged-rerun path skips mount() entirely - see RESULTS.md).
            tables_before = table_count()
            page.get_by_role("button", name="Rerun (new data)").click()
            page.wait_for_function(
                "() => document.querySelector('[data-testid=\"stApp\"]')"
                "?.getAttribute('data-test-script-state') === 'notRunning'",
                timeout=RERUN_TIMEOUT_S * 1000,
            )
            # Generous deadline: the perf table only logs once mount()
            # *completes*, and deck.gl layer construction can take tens of
            # seconds for heavy layers under headless/software GL (e.g.
            # earcut-triangulating 90k solid polygons).
            deadline = time.monotonic() + 120
            while table_count() <= tables_before and time.monotonic() < deadline:
                time.sleep(0.2)
            perf_tables: list[list[dict]] = page.evaluate("() => window.__stPerfTables || []")

            if screenshot_dir is not None:
                screenshot_dir.mkdir(parents=True, exist_ok=True)
                # Best-effort: a compositing stall must not discard the
                # already-captured decode numbers.
                with contextlib.suppress(Exception):
                    page.screenshot(path=str(screenshot_dir / f"{shot_label}_{mode_name}_{n}.png"))

            if len(perf_tables) <= tables_before:
                return {"n": n, "mode": mode_name, "status": "no-perf-table"}

            spans = {row["span"]: float(row["ms"]) for row in perf_tables[-1]}
            decode_ms = spans.get("decompress", 0.0) + spans.get("parquetDecode[layer-0]", 0.0)
            return {
                "n": n,
                "mode": mode_name,
                "status": "ok",
                "decompress_ms": spans.get("decompress"),
                "parquet_decode_ms": spans.get("parquetDecode[layer-0]"),
                "table_from_ipc_ms": spans.get("tableFromIPC[layer-0]"),
                "parse_container_ms": spans.get("parseContainer"),
                "mount_ms": spans.get("mount"),
                "total_decode_ms": round(decode_ms + (spans.get("tableFromIPC[layer-0]") or 0.0), 1),
            }
        finally:
            browser.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scales", default=",".join(str(s) for s in DEFAULT_SCALES))
    parser.add_argument("--skip-browser", action="store_true")
    parser.add_argument("--out", default=None)
    parser.add_argument("--screenshot-dir", default=None)
    args = parser.parse_args()
    scales = [int(s) for s in args.scales.split(",")]
    screenshot_dir = Path(args.screenshot_dir) if args.screenshot_dir else None

    results: dict = {"python": [], "browser": []}
    for n in scales:
        print(f"--- Part 1 (encode/size) @ {n:,} ---", file=sys.stderr)
        results["python"].extend(measure_python(n))

    if not args.skip_browser:
        for n in scales:
            for mode_name, _ in MODES:
                print(f"--- Part 2 (browser decode) {mode_name} @ {n:,} ---", file=sys.stderr)
                try:
                    row = measure_browser(n, mode_name, screenshot_dir)
                except Exception as e:  # noqa: BLE001 - a DNF is a valid result
                    row = {"n": n, "mode": mode_name, "status": "DNF", "error": str(e)[:500]}
                print(json.dumps(row), file=sys.stderr)
                results["browser"].append(row)

    output = json.dumps(results, indent=2)
    if args.out:
        Path(args.out).write_text(output)
    else:
        print(output)


if __name__ == "__main__":
    main()
