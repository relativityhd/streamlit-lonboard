"""Real-data scenarios for the wire-format benchmarks (see wire_options_real.py).

Data: an Arctic EO analysis-ready cube aggregated onto the A5 DGGS
(zarr v3 stores per theme, one group per A5 level L06-L09, one 1-D array
per per-cell statistic; plus a `_index/L0X/cells.parquet` GeoParquet with
each cell's id and pentagon boundary polygon). Location is machine-specific
- override with A5_DATA_ROOT or --data-root.

Two scenarios, chosen to bracket how real payloads differ from the
synthetic uniform-random benchmark:

- "a5-cells": what an actual dashboard on this data ships - uint64 A5 cell
  ids (`get_pentagon` accessor; the browser derives pentagon geometry from
  the id, no coordinates on the wire), a uint8 RGBA color accessor computed
  from ArcticDEM elevation, and six float32 per-cell statistics as tooltip
  columns. Sorted ids + spatially-correlated stats = highly compressible.
- "pentagons": the same cells shipped the "classic" way, as real GeoArrow
  polygon geometry (each pentagon's boundary ring from the index parquet,
  float64 vertices) + the same color accessor and one tooltip column. This
  is the coordinates-on-the-wire case the synthetic benchmark modeled, but
  with real (spatially ordered, partially redundant) coordinates.
"""

from __future__ import annotations

import os
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyarrow as pa

from lonboard import A5Layer, SolidPolygonLayer

DEFAULT_DATA_ROOT = "/home/tobias/Data/eo-analysis-ready/a5"
DEM_STORE = "terrain/ArcticDEM-mosaic-v4.1.zarr"
DEM_STATS = ["mean", "std", "min", "max", "p10", "p90"]

# ArcticDEM is pan-Arctic; roughly center the view on it.
ARCTIC_VIEW = {"longitude": -40.0, "latitude": 72.0, "zoom": 2.5}


def data_root() -> Path:
    return Path(os.environ.get("A5_DATA_ROOT", DEFAULT_DATA_ROOT))


def _load_dem_level(level: str) -> dict[str, np.ndarray]:
    import zarr  # bench-only dependency (pyproject `bench` extra)

    grp = zarr.open_group(str(data_root() / DEM_STORE), mode="r")[level]
    out = {"cell_id": grp["cell_id"][:]}
    for stat in DEM_STATS:
        out[f"dem_{stat}"] = grp["dem"][stat][:]
    return out


def _elevation_colors(dem_mean: np.ndarray) -> np.ndarray:
    """uint8 RGB, blue (low) -> red (high) over the 2..98 percentile range.

    NaN cells (no DEM coverage) get gray - matplotlib isn't a benchmark
    dependency, so this is a deliberately minimal two-stop ramp; the point
    is a *realistic accessor column* (spatially correlated uint8), not
    cartographic beauty."""
    lo, hi = np.nanpercentile(dem_mean, 2), np.nanpercentile(dem_mean, 98)
    t = np.clip((np.nan_to_num(dem_mean, nan=lo) - lo) / max(1e-9, hi - lo), 0.0, 1.0)
    colors = np.stack([t * 255, np.full_like(t, 80.0), (1.0 - t) * 255], axis=1).astype("uint8")
    colors[np.isnan(dem_mean)] = (128, 128, 128)
    return colors


def build_a5_cells_layer(level: str) -> tuple[A5Layer, tuple[str, ...], np.ndarray]:
    """Returns (layer, tooltip column names, base color array).

    The base colors come back as a plain numpy array (not read back off the
    layer trait) so bench_real_app.py's "new data" perturbation can derive
    a modified copy - the trait itself holds an arro3 array whose
    FixedSizeList type has no `to_numpy`."""
    data = _load_dem_level(level)
    colors = _elevation_colors(data["dem_mean"])
    table = pa.table({name: pa.array(data[name]) for name in data if name != "cell_id"})
    layer = A5Layer(
        table=table,
        get_pentagon=pa.array(data["cell_id"], pa.uint64()),
        get_fill_color=colors,
        pickable=True,
    )
    return layer, tuple(f"dem_{s}" for s in DEM_STATS), colors


def build_pentagons_layer(level: str) -> tuple[SolidPolygonLayer, tuple[str, ...], np.ndarray]:
    """Returns (layer, tooltip column names, base color array)."""
    data = _load_dem_level(level)
    gdf = gpd.read_parquet(data_root() / "_index" / level / "cells.parquet", columns=["cell_id", "geometry"])
    # The DEM covers a subset of the index's cells; align by cell_id and
    # keep only covered cells so colors/tooltips describe real data.
    dem = pd.DataFrame({"cell_id": data["cell_id"], "dem_mean": data["dem_mean"]})
    gdf = gdf.merge(dem, on="cell_id", how="inner")
    colors = _elevation_colors(gdf["dem_mean"].to_numpy())
    layer = SolidPolygonLayer.from_geopandas(
        gdf[["geometry", "dem_mean"]],
        get_fill_color=colors,
        pickable=True,
    )
    return layer, ("dem_mean",), colors


SCENARIOS = {
    "a5-cells": build_a5_cells_layer,
    "pentagons": build_pentagons_layer,
}


def build_scenario(scenario: str, level: str):
    return SCENARIOS[scenario](level)
