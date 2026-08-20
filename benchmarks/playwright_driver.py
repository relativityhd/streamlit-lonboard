"""Phase 4c driver: measures st_lonboard vs st.pydeck_chart head-to-head.

Usage: uv run python benchmarks/playwright_driver.py [--scales 10000,100000] [--out results.json]

Map.to_html() (benchmarks/contenders/tohtml_app.py) is deliberately excluded
here - it doesn't render at all when embedded via components.v1.html (see
benchmarks/RESULTS.md "Phase 4c" for the root cause: requirejs/anywidget's
module loader breaks inside a srcdoc iframe, since `document.location.href`
there is the opaque `about:srcdoc`, not the page's real URL). That's a
hard, scale-independent DNF, not something a timing run would add anything
to - rerunning it wouldn't do anything except wait for a canvas that will
never appear.

For each (contender, N) this measures, using Streamlit's own
`data-test-script-state` attribute on `[data-testid="stApp"]` (the same
signal Streamlit's own test suite uses) to know exactly when a rerun starts
and ends, plus a canvas-appearance + short-quiescence check for "visually
rendered" (script completion alone doesn't mean deck.gl has painted yet):

- time_to_first_render: navigation -> script done -> canvas appears and
  stops growing.
- rerun_unchanged_ms: click "Rerun (unchanged data)" -> script re-runs ->
  canvas re-settles.
- interaction_rerun_ms: st_lonboard only (return_view_state=True is the only
  one of the three contenders where an interaction round-trips through
  Python at all - pydeck's view state never leaves the browser, and to_html
  doesn't render in the first place). Simulates a small canvas drag, waits
  for the resulting rerun to complete the same way.

Each (contender, N) gets its own fresh `streamlit run` subprocess (own port,
own process) so measurements never share warm caches or leftover memory
across scales.

Each measurement ALSO runs inside its own `--single` driver subprocess
(spawned by `main()`) with a hard wall-clock timeout. This isn't optional
paranoia: deck.gl's WebGL picking readback (`gl.readPixels`) occasionally
stalls the renderer's main thread for many seconds under this environment's
(headless, often software-rendered) GPU path - confirmed by reproducing it
standalone, console warnings included ("GPU stall due to ReadPixels"). When
that happens mid-measurement, `page.evaluate()` blocks with no timeout of
its own and no way to interrupt it from Python (it's not waiting on
anything Playwright can cancel - the target page's JS thread is genuinely
still busy). A subprocess boundary with `subprocess.run(..., timeout=...)`
is what actually recovers from that: kill the whole subprocess and record a
DNF, rather than hang the entire benchmark run over one stalled frame.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

CONTENDERS_DIR = Path(__file__).parent / "contenders"
DEFAULT_SCALES = [10_000, 100_000, 1_000_000, 10_000_000]
STARTUP_TIMEOUT_S = 90
RERUN_TIMEOUT_S = 120
SETTLE_POLL_S = 0.2
SETTLE_STABLE_ROUNDS = 3  # consecutive stable polls before declaring "rendered"
SOFTWARE_GL_ARGS = [
    "--use-gl=angle",
    "--use-angle=swiftshader",
    "--enable-unsafe-swiftshader",
    "--ignore-gpu-blocklist",
]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def streamlit_app(script: str, n: int, port: int, *, script_dir: Path = CONTENDERS_DIR, env: dict | None = None):
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(script_dir / script),
            "--server.headless",
            "true",
            "--server.port",
            str(port),
        ],
        env={**os.environ, "BENCH_N": str(n), **(env or {})},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        url = f"http://127.0.0.1:{port}/_stcore/health"
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                stderr = proc.stderr.read() if proc.stderr else ""
                raise RuntimeError(f"streamlit process exited early:\n{stderr}")
            try:
                with urllib.request.urlopen(url, timeout=1) as resp:
                    if resp.status == 200:
                        break
            except OSError:
                pass
            time.sleep(0.3)
        else:
            raise TimeoutError(f"streamlit server on port {port} never became healthy")
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def _wait_script_state(page: Page, state: str, timeout_s: float) -> None:
    page.wait_for_function(
        "(state) => document.querySelector('[data-testid=\"stApp\"]')"
        "?.getAttribute('data-test-script-state') === state",
        arg=state,
        timeout=timeout_s * 1000,
    )


def _wait_canvas_settled(page: Page, timeout_s: float) -> None:
    """Wait for at least one <canvas> with a nonzero box, then for its
    bounding box to stop changing across SETTLE_STABLE_ROUNDS consecutive
    polls - deck.gl/MapLibre resize their canvas a few times while spinning
    up (devicePixelRatio fixups, container resize, etc.).

    Uses Playwright's own locator engine (`page.locator`), not
    `page.evaluate(() => document.querySelector(...))` - our own component's
    canvas lives inside an open shadow root (Streamlit mounts BidiComponents
    under `[data-testid="stBidiComponentIsolated"]`), which plain
    `document.querySelector` does not pierce but Playwright's locators do
    automatically.
    """
    deadline = time.monotonic() + timeout_s
    last_box = None
    stable_rounds = 0
    canvas = page.locator("canvas").first
    while time.monotonic() < deadline:
        box = None
        if canvas.count() > 0:
            box = canvas.bounding_box()
        if box and box["width"] > 0 and box["height"] > 0:
            key = (box["width"], box["height"])
            if key == last_box:
                stable_rounds += 1
                if stable_rounds >= SETTLE_STABLE_ROUNDS:
                    return
            else:
                stable_rounds = 0
            last_box = key
        time.sleep(SETTLE_POLL_S)
    raise TimeoutError("canvas never appeared/settled")


def _wait_rerun_complete(page: Page, timeout_s: float) -> None:
    """Wait for a rerun triggered just before this call to finish.

    Local reruns on small data can flip running -> notRunning faster than we
    can observe the "running" transition, so that wait is best-effort and
    short; `wait_for_function` resolves immediately if its condition is
    already true, so waiting for "notRunning" afterward is correct whether or
    not we caught "running" - that's the authoritative completion signal.
    """
    with contextlib.suppress(PlaywrightTimeoutError):
        _wait_script_state(page, "running", min(5.0, timeout_s))
    _wait_script_state(page, "notRunning", timeout_s)
    _wait_canvas_settled(page, timeout_s)


def _click_rerun_and_time(page: Page) -> float:
    start = time.monotonic()
    page.get_by_role("button", name="Rerun (unchanged data)").click()
    _wait_rerun_complete(page, RERUN_TIMEOUT_S)
    return (time.monotonic() - start) * 1000


def _drag_canvas_and_time(page: Page) -> float:
    canvas = page.locator("canvas").first
    box = canvas.bounding_box()
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    start = time.monotonic()
    page.mouse.move(cx, cy)
    page.mouse.down()
    page.mouse.move(cx + 80, cy + 40, steps=10)
    page.mouse.up()
    _wait_rerun_complete(page, RERUN_TIMEOUT_S)
    return (time.monotonic() - start) * 1000


# Installed before any page script runs. Phase 4f's acceptance criterion is
# main-thread blocking time (what a user feels as "the tab froze"), which wall clock
# around a rerun does not isolate - it also contains Python time, transport, and GPU
# work. `longtask` entries are exactly the >50ms main-thread tasks.
LONGTASK_OBSERVER = """
window.__lt = [];
new PerformanceObserver((list) => {
  for (const e of list.getEntries()) window.__lt.push(e.duration);
}).observe({type: 'longtask', buffered: true});
window.__ltReset = () => { window.__lt = []; };
window.__ltSum = () => window.__lt.reduce((a, b) => a + b, 0);
window.__ltMax = () => window.__lt.reduce((a, b) => Math.max(a, b), 0);
"""


def _wait_main_thread_quiet(page: Page, quiet_ms: int = 1500, timeout_s: float = 180) -> None:
    """Wait until no new longtask has been recorded for `quiet_ms`.

    `_wait_canvas_settled` only watches the canvas's bounding box, which stops
    changing as soon as the element is sized - long before deck.gl has finished
    deriving geometry and tessellating. Measuring a rerun without this first lets
    the *previous* render's tail land inside the next measurement window, which is
    enough to make a genuinely-zero-cost recolour look like tens of seconds.
    """
    deadline = time.monotonic() + timeout_s
    page.evaluate("window.__ltMark = window.__lt.length")
    quiet_since = time.monotonic()
    while time.monotonic() < deadline:
        grew = page.evaluate("window.__lt.length > window.__ltMark")
        if grew:
            page.evaluate("window.__ltMark = window.__lt.length")
            quiet_since = time.monotonic()
        elif (time.monotonic() - quiet_since) * 1000 >= quiet_ms:
            return
        time.sleep(0.25)


def _click_and_measure_longtasks(page: Page, button: str) -> dict:
    page.evaluate("window.__ltReset()")
    start = time.monotonic()
    page.get_by_role("button", name=button).click()
    _wait_rerun_complete(page, RERUN_TIMEOUT_S)
    _wait_main_thread_quiet(page)
    wall_ms = (time.monotonic() - start) * 1000
    # PerformanceObserver delivers in batches; without a settle window the last
    # tasks of the rerun land after we read the total.
    page.wait_for_timeout(500)
    return {
        "wall_ms": round(wall_ms, 1),
        "longtask_total_ms": round(page.evaluate("window.__ltSum()"), 1),
        "longtask_max_ms": round(page.evaluate("window.__ltMax()"), 1),
    }


def measure_recolor(n: int, layer: str) -> dict:
    """Phase 4f: cost of a rerun that changes only per-row colours.

    Drives `bench_recolor_app.py`, whose recolour button reassigns `get_fill_color` on
    a cached layer so the geometry columns stay byte-identical. Reports the same three
    numbers for a genuine geometry change, as the control: that one *must* stay
    expensive, or the reuse path is over-matching and the map has gone stale.
    """
    port = _free_port()
    with streamlit_app(
        "bench_recolor_app.py",
        n,
        port,
        script_dir=Path(__file__).parent,
        env={"BENCH_LAYER": layer},
    ) as base_url:
        with sync_playwright() as pw:
            # Pin the software GL path explicitly. Headless Chromium's default
            # varies with what the host exposes, and the fallback it picks when
            # left alone is dramatically slower - enough to swamp the difference
            # this benchmark exists to measure.
            browser = pw.chromium.launch(headless=True, args=SOFTWARE_GL_ARGS)
            page = browser.new_page()
            page.add_init_script(LONGTASK_OBSERVER)
            try:
                page.goto(base_url, wait_until="domcontentloaded")
                _wait_script_state(page, "notRunning", STARTUP_TIMEOUT_S)
                _wait_canvas_settled(page, STARTUP_TIMEOUT_S)
                # The first render is the big one; it must be fully finished before
                # any rerun is timed, or its tail is charged to that rerun.
                _wait_main_thread_quiet(page)
                page.wait_for_timeout(500)
                first_render = {
                    "longtask_total_ms": round(page.evaluate("window.__ltSum()"), 1),
                    "longtask_max_ms": round(page.evaluate("window.__ltMax()"), 1),
                }
                return {
                    "scenario": "recolor",
                    "layer": layer,
                    "n": n,
                    "status": "ok",
                    "first_render": first_render,
                    "recolor": _click_and_measure_longtasks(page, "Rerun (recolour only)"),
                    "new_geometry": _click_and_measure_longtasks(page, "Rerun (new geometry)"),
                }
            finally:
                browser.close()


def measure(script: str, n: int, measure_interaction: bool) -> dict:
    port = _free_port()
    with streamlit_app(script, n, port) as base_url:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                t0 = time.monotonic()
                page.goto(base_url, wait_until="domcontentloaded")
                _wait_script_state(page, "notRunning", STARTUP_TIMEOUT_S)
                _wait_canvas_settled(page, STARTUP_TIMEOUT_S)
                time_to_first_render_ms = (time.monotonic() - t0) * 1000

                rerun_unchanged_ms = _click_rerun_and_time(page)

                interaction_rerun_ms = None
                if measure_interaction:
                    interaction_rerun_ms = _drag_canvas_and_time(page)

                return {
                    "contender": script,
                    "n": n,
                    "status": "ok",
                    "time_to_first_render_ms": round(time_to_first_render_ms, 1),
                    "rerun_unchanged_ms": round(rerun_unchanged_ms, 1),
                    "interaction_rerun_ms": (
                        round(interaction_rerun_ms, 1) if interaction_rerun_ms is not None else None
                    ),
                }
            finally:
                browser.close()


SUBPROCESS_TIMEOUT_S = STARTUP_TIMEOUT_S + 2 * RERUN_TIMEOUT_S + 30  # + margin for browser/process teardown


def _measure_in_subprocess(script: str, n: int, measure_interaction: bool) -> dict:
    """Runs `measure()` in a fresh `python playwright_driver.py --single ...`
    subprocess with a hard wall-clock timeout - see module docstring for why
    this boundary (not just a try/except around `measure()`) is required."""
    cmd = [sys.executable, str(Path(__file__)), "--single", script, str(n)]
    if measure_interaction:
        cmd.append("--interaction")
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT_S
        )
    except subprocess.TimeoutExpired:
        return {
            "contender": script,
            "n": n,
            "status": "DNF",
            "error": f"measurement subprocess did not finish within {SUBPROCESS_TIMEOUT_S}s (hung)",
        }
    if proc.returncode != 0:
        return {
            "contender": script,
            "n": n,
            "status": "DNF",
            "error": proc.stderr.strip()[-2000:] or f"subprocess exited {proc.returncode}",
        }
    return json.loads(proc.stdout)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scales", default=",".join(str(s) for s in DEFAULT_SCALES))
    parser.add_argument("--out", default=None)
    parser.add_argument("--single", nargs=2, metavar=("SCRIPT", "N"), default=None)
    parser.add_argument("--interaction", action="store_true")
    parser.add_argument(
        "--recolor",
        nargs="?",
        const="h3,polygon,scatterplot",
        default=None,
        metavar="LAYERS",
        help=(
            "Phase 4f: measure colour-only reruns (comma-separated layer kinds for "
            "bench_recolor_app.py). Uses --scales for sizes."
        ),
    )
    args = parser.parse_args()

    if args.recolor is not None:
        results = []
        for layer in args.recolor.split(","):
            for n in [int(s) for s in args.scales.split(",")]:
                print(f"--- recolor: {layer} @ {n:,} ---", file=sys.stderr)
                try:
                    result = measure_recolor(n, layer)
                except Exception as e:  # noqa: BLE001 - a DNF is a valid result
                    result = {"scenario": "recolor", "layer": layer, "n": n, "status": "DNF", "error": str(e)}
                print(json.dumps(result), file=sys.stderr)
                results.append(result)
        output = json.dumps(results, indent=2)
        if args.out:
            Path(args.out).write_text(output)
        else:
            print(output)
        return

    if args.single is not None:
        # Internal mode: run exactly one measurement and print its JSON result
        # to stdout. Invoked by _measure_in_subprocess, not meant for direct use.
        script, n = args.single[0], int(args.single[1])
        try:
            result = measure(script, n, args.interaction)
        except Exception as e:  # noqa: BLE001 - deliberately broad: a DNF is a valid result
            result = {"contender": script, "n": n, "status": "DNF", "error": str(e)}
        print(json.dumps(result))
        return

    scales = [int(s) for s in args.scales.split(",")]

    contenders = [
        ("lonboard_app.py", True),
        ("pydeck_app.py", False),
    ]

    results = []
    for script, measure_interaction in contenders:
        for n in scales:
            print(f"--- {script} @ {n:,} points ---", file=sys.stderr)
            result = _measure_in_subprocess(script, n, measure_interaction)
            print(json.dumps(result), file=sys.stderr)
            results.append(result)

    # to_html is a hard, scale-independent DNF - see module docstring - recorded
    # once per scale for a complete table without wasting time launching it.
    for n in scales:
        results.append(
            {
                "contender": "tohtml_app.py",
                "n": n,
                "status": "DNF",
                "error": (
                    "Map.to_html() never renders inside components.v1.html's srcdoc "
                    "iframe: document.location.href there is the opaque 'about:srcdoc', "
                    "which breaks requirejs/anywidget's module-loading base-URL "
                    "inference. The widget bundle, anywidget runtime, and WASM parquet "
                    "reader never load; the map area stays blank forever, with no error "
                    "surfaced to the user. Confirmed the same to_html() output works "
                    "fine served standalone (outside any iframe)."
                ),
            }
        )

    output = json.dumps(results, indent=2)
    if args.out:
        Path(args.out).write_text(output)
    else:
        print(output)


if __name__ == "__main__":
    main()
