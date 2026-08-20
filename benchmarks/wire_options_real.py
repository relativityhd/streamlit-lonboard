"""Wire-format shootout on REAL data - see wire_options.py for the synthetic twin.

Usage:
    uv run python benchmarks/wire_options_real.py                # everything
    uv run python benchmarks/wire_options_real.py --skip-browser
    uv run python benchmarks/wire_options_real.py --data-root /path/to/a5

Runs the same Part 1 (encode time + wire size) and Part 2 (browser decode
time via bench_real_app.py + playwright) as wire_options.py, but over the
scenarios in real_scenarios.py (an Arctic EO cube on the A5 DGGS):

- a5-cells @ L07/L08/L09 (22.7k / 90.1k / 358.6k cells)
- pentagons @ L07/L08 (real polygon boundary geometry; L09 works too but
  is skipped by default - ~35MB raw payloads make the browser rows slow
  without changing the story)

Needs the `bench` extra (`uv sync --all-extras`) for zarr + playwright.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from real_scenarios import build_scenario  # noqa: E402
from wire_options import MODES, measure_browser, measure_python_layer  # noqa: E402

BENCH_REAL_APP = Path(__file__).parent / "bench_real_app.py"

DEFAULT_CASES = [
    ("a5-cells", "L07"),
    ("a5-cells", "L08"),
    ("a5-cells", "L09"),
    ("pentagons", "L07"),
    ("pentagons", "L08"),
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", default=None)
    parser.add_argument("--cases", default=",".join(f"{s}:{lv}" for s, lv in DEFAULT_CASES))
    parser.add_argument("--skip-browser", action="store_true")
    parser.add_argument("--out", default=None)
    parser.add_argument("--screenshot-dir", default=None)
    args = parser.parse_args()

    if args.data_root:
        os.environ["A5_DATA_ROOT"] = args.data_root
    cases = [tuple(c.split(":")) for c in args.cases.split(",")]
    screenshot_dir = Path(args.screenshot_dir) if args.screenshot_dir else None

    results: dict = {"python": [], "browser": []}
    for scenario, level in cases:
        print(f"--- Part 1 (encode/size) {scenario} @ {level} ---", file=sys.stderr)
        layer, tooltip, _colors = build_scenario(scenario, level)
        n = layer.table.num_rows
        results["python"].extend(
            measure_python_layer(layer, n=n, tooltip=tooltip, extra={"scenario": scenario, "level": level})
        )
        del layer

    if not args.skip_browser:
        for scenario, level in cases:
            for mode_name, _ in MODES:
                print(f"--- Part 2 (browser decode) {scenario} @ {level} {mode_name} ---", file=sys.stderr)
                try:
                    row = measure_browser(
                        0,
                        mode_name,
                        screenshot_dir,
                        script=BENCH_REAL_APP,
                        extra_env={
                            "BENCH_SCENARIO": scenario,
                            "BENCH_LEVEL": level,
                            **({"A5_DATA_ROOT": os.environ["A5_DATA_ROOT"]} if args.data_root else {}),
                        },
                        shot_label=f"real_{scenario}_{level}",
                    )
                except Exception as e:  # noqa: BLE001 - a DNF is a valid result
                    row = {"mode": mode_name, "status": "DNF", "error": str(e)[:500]}
                row.update({"scenario": scenario, "level": level})
                row.pop("n", None)
                print(json.dumps(row), file=sys.stderr)
                results["browser"].append(row)

    output = json.dumps(results, indent=2)
    if args.out:
        Path(args.out).write_text(output)
    else:
        print(output)


if __name__ == "__main__":
    main()
