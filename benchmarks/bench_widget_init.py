"""Phase 4g: cost of lonboard layer construction with and without the widget-comm patch.

Usage: uv run python benchmarks/bench_widget_init.py [N]

No Streamlit and no browser involved - this measures pure Python construction cost.
lonboard layers are ipywidgets `Widget`s, and `Widget.__init__` unconditionally opens a
Jupyter comm, which serializes the whole table plus every accessor to parquet for a
message that Streamlit's (kernel-less) environment throws away. See
src/streamlit_lonboard/_widget_patch.py.

Reports A5Layer (accessor geometry, pre-built table - the shape where the comm work
dominates) and ScatterplotLayer.from_geopandas (where GeoDataFrame -> Arrow conversion
is real work the patch cannot remove, so the ratio is smaller and more representative
of a typical app).
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
import pyarrow as pa
from lonboard import A5Layer, ScatterplotLayer
from shapely.geometry import Point

REPEATS = 5


def _load_widget_patch():
    """Load _widget_patch.py directly, skipping streamlit_lonboard/__init__.py (which
    imports component.py and needs a full Streamlit runtime).
    """
    path = Path(__file__).resolve().parents[1] / "src" / "streamlit_lonboard" / "_widget_patch.py"
    spec = importlib.util.spec_from_file_location("streamlit_lonboard._widget_patch", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _time(build) -> float:
    """Best-of-REPEATS wall clock, in seconds."""
    best = float("inf")
    for _ in range(REPEATS):
        start = time.perf_counter()
        build()
        best = min(best, time.perf_counter() - start)
    return best


def main(n: int) -> None:
    widget_patch = _load_widget_patch()
    rng = np.random.default_rng(0)

    cells = rng.integers(1, 2**60, n, dtype=np.uint64)
    colors = rng.integers(0, 255, size=(n, 3)).astype("uint8")
    a5_table = pa.table({"value": rng.random(n)})
    fill = pa.FixedSizeListArray.from_arrays(pa.array(colors.ravel()), 3)

    lon = rng.uniform(-122.6, -122.3, n)
    lat = rng.uniform(37.6, 37.9, n)
    gdf = gpd.GeoDataFrame(geometry=[Point(x, y) for x, y in zip(lon, lat)], crs="EPSG:4326")

    scenarios = {
        "A5Layer (pre-built table)": lambda: A5Layer(
            table=a5_table, get_pentagon=cells, get_fill_color=fill
        ),
        "ScatterplotLayer.from_geopandas": lambda: ScatterplotLayer.from_geopandas(
            gdf, get_fill_color=colors
        ),
    }

    print(f"n = {n:,} rows, best of {REPEATS}\n")
    print(f"{'scenario':<34} {'stock':>10} {'patched':>10} {'speedup':>9}  comm")
    for label, build in scenarios.items():
        widget_patch.remove_widget_comm_patch()
        stock = _time(build)
        widget_patch.apply_widget_comm_patch()
        patched = _time(build)
        comm = build().comm
        print(
            f"{label:<34} {stock * 1000:>9.1f}ms {patched * 1000:>9.1f}ms "
            f"{stock / patched:>8.0f}x  {comm!r}"
        )
    widget_patch.remove_widget_comm_patch()


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 200_000)
