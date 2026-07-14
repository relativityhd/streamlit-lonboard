"""Shared data generator for the Phase 4c contender benchmarks.

Same N, same seed, same visual encoding (color, radius) feeds every
contender (`st_lonboard`, `st.pydeck_chart`, `Map.to_html()`), so any
difference in wire size or render cost comes from the library, not the data.
"""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point

# Matches benchmarks/bench_app.py's bbox (SF bay area) for consistency across
# all benchmark apps in this repo.
BBOX = (-122.6, 37.6, -122.3, 37.9)


def generate_points(n: int, seed: int = 0) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(seed)
    lon = rng.uniform(BBOX[0], BBOX[2], n)
    lat = rng.uniform(BBOX[1], BBOX[3], n)
    colors = rng.integers(0, 255, size=(n, 3)).astype("uint8")
    radii = rng.uniform(20, 200, n).astype("float32")
    return {"lon": lon, "lat": lat, "colors": colors, "radii": radii}


def points_geodataframe(n: int, seed: int = 0) -> gpd.GeoDataFrame:
    """For st_lonboard (ScatterplotLayer.from_geopandas)."""
    data = generate_points(n, seed)
    return gpd.GeoDataFrame(
        {
            "r": data["colors"][:, 0],
            "g": data["colors"][:, 1],
            "b": data["colors"][:, 2],
            "radius": data["radii"],
        },
        geometry=[Point(x, y) for x, y in zip(data["lon"], data["lat"])],
        crs="EPSG:4326",
    )


def points_dataframe(n: int, seed: int = 0) -> pd.DataFrame:
    """For pydeck (no GeoArrow support - plain lon/lat columns)."""
    data = generate_points(n, seed)
    return pd.DataFrame(
        {
            "lon": data["lon"],
            "lat": data["lat"],
            "r": data["colors"][:, 0],
            "g": data["colors"][:, 1],
            "b": data["colors"][:, 2],
            "radius": data["radii"],
        }
    )
