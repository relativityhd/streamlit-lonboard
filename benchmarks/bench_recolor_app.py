"""Phase 4f benchmark app: does changing only a colour column avoid re-tessellation?

Usage: BENCH_N=160000 ST_LONBOARD_PERF=1 uv run streamlit run benchmarks/bench_recolor_app.py

The dashboard interaction this models is "user picks a different colormap": the cells
stay exactly where they are and only the per-row colour changes. Before Phase 4f that
cost ~56us per cell in the browser, because a new Arrow table meant a new deck.gl
`data` object, which meant re-deriving every cell boundary and re-tessellating.

Layer families (BENCH_LAYER):
- "h3" (default): H3HexagonLayer with `high_precision=True`, geometry carried by the
  `get_hexagon` accessor - the DGGS shape that motivated this work. Needs `h3`
  (`uv pip install h3`). The flag matters: deck.gl's `highPrecision: "auto"` default
  instances a single hexagon geometry through ColumnLayer, which is cheap enough that
  it never exhibits the problem (160k cells in 0.6s). A5/S2/geohash have no such fast
  path, which is why they were the ones costing ~9s.
- "polygon": PolygonLayer over a GeoArrow geometry column, which exercises the
  earcut/tessellation path instead of per-cell boundary derivation.
- "scatterplot": the cheap control - points need no tessellation at all, so recolour
  was already fast here.

A5 itself has no published Python bindings (only the JS `a5-js` that deck.gl uses), so
it cannot be generated here; its numbers were measured directly in the browser.

Heads-up on `uv pip install h3`: it makes
`tests/test_serialize.py::test_serialize_layer_h3_hexagon_ships_uint64_cells` fail.
That test feeds H3HexagonLayer fabricated uint64 cell IDs, which only pass while
`h3-py` is absent - with it installed, lonboard's default-viewport computation
actually decodes them and raises `H3CellInvalidError`. Either uninstall h3 after
benchmarking, or swap that test's IDs for real ones (e.g. 0x8928308212FFFFF,
0x89283082EBBFFFF, 0x89283082E23FFFF - resolution-9 cells around San Francisco).

Buttons:
- "Rerun (recolour only)": bumps only the colour seed. The layer object is cached and
  its `get_fill_color` trait reassigned, so the geometry columns are byte-identical
  and only the colour column differs. This is the path Phase 4f optimizes.
- "Rerun (new geometry)": bumps the geometry seed, forcing a genuine rebuild.
- "Rerun (unchanged)": byte-identical payload; Streamlit's ForwardMsgCache means the
  frontend's mount() is never even invoked (Phase 4.0 finding).

Read the browser console for the `st-lonboard:*` perf table, and use
`benchmarks/playwright_driver.py --scenario recolor` for the longtask numbers that
Phase 4f is actually judged on.
"""

import os

import numpy as np
import pyarrow as pa
import streamlit as st

from streamlit_lonboard import st_lonboard

N = int(os.environ.get("BENCH_N", "160000"))
LAYER = os.environ.get("BENCH_LAYER", "h3")
CENTER = (37.75, -122.4)


@st.cache_resource
def build_geometry(n: int, layer_kind: str, geometry_seed: int):
    """Build the layer once per (size, kind, geometry seed).

    Cached so that a recolour rerun reassigns a trait on the *same* layer object
    rather than building a new one - which is both what a real app does and what
    keeps the geometry columns byte-identical across reruns.
    """
    rng = np.random.default_rng(geometry_seed)

    if layer_kind == "h3":
        import h3
        from lonboard import H3HexagonLayer

        # grid_disk(k) yields 3k^2+3k+1 cells; solve for the k that covers n.
        k = int(np.ceil((-3 + np.sqrt(9 + 12 * (n - 1))) / 6))
        # Offsetting the centre by the seed genuinely moves every cell, so the
        # "new geometry" button actually changes the geometry columns.
        center = (CENTER[0] + geometry_seed * 0.05, CENTER[1] + geometry_seed * 0.05)
        cells = h3.grid_disk(h3.latlng_to_cell(*center, 8), k)[:n]
        hex_ids = np.array([h3.str_to_int(c) for c in cells], dtype=np.uint64)
        table = pa.table({"value": rng.random(len(hex_ids))})
        # high_precision=True forces deck.gl down the PolygonLayer path, where each
        # cell is polygonized and tessellated individually. Its `highPrecision:
        # "auto"` default instead instances one hexagon geometry via ColumnLayer,
        # which is so much cheaper that it never showed the problem this phase is
        # about (measured: 160k cells cost 0.6s in auto mode vs ~9s for the same
        # count of A5 pentagons, which have no instanced fast path).
        return (
            H3HexagonLayer(table=table, get_hexagon=hex_ids, high_precision=True, pickable=True),
            len(hex_ids),
        )

    if layer_kind == "polygon":
        import geopandas as gpd
        from lonboard import PolygonLayer
        from shapely.geometry import Polygon

        # A grid of small squares - cheap to build, and every one needs tessellating.
        side = int(np.ceil(np.sqrt(n)))
        step = 0.4 / side
        shift = geometry_seed * 0.05
        xs, ys = np.meshgrid(np.arange(side), np.arange(side))
        corners = np.column_stack([xs.ravel(), ys.ravel()])[:n]
        polys = [
            Polygon(
                [
                    (-122.6 + shift + i * step, 37.6 + j * step),
                    (-122.6 + shift + (i + 0.9) * step, 37.6 + j * step),
                    (-122.6 + shift + (i + 0.9) * step, 37.6 + (j + 0.9) * step),
                    (-122.6 + shift + i * step, 37.6 + (j + 0.9) * step),
                ]
            )
            for i, j in corners
        ]
        gdf = gpd.GeoDataFrame({"value": rng.random(len(polys))}, geometry=polys, crs="EPSG:4326")
        return PolygonLayer.from_geopandas(gdf, pickable=True), len(polys)

    import geopandas as gpd
    from lonboard import ScatterplotLayer
    from shapely.geometry import Point

    pts = [
        Point(x, y)
        for x, y in zip(
            rng.uniform(-122.6, -122.3, n) + geometry_seed * 0.05, rng.uniform(37.6, 37.9, n)
        )
    ]
    gdf = gpd.GeoDataFrame({"value": rng.random(n)}, geometry=pts, crs="EPSG:4326")
    return ScatterplotLayer.from_geopandas(gdf, get_radius=30, radius_units="meters", pickable=True), n


st.title(f"Phase 4f: {LAYER} recolour, {N:,} requested")

col1, col2, col3 = st.columns(3)
col1.button("Rerun (unchanged)")
if col2.button("Rerun (recolour only)"):
    st.session_state["color_seed"] = st.session_state.get("color_seed", 0) + 1
if col3.button("Rerun (new geometry)"):
    st.session_state["geometry_seed"] = st.session_state.get("geometry_seed", 0) + 1

color_seed = st.session_state.get("color_seed", 0)
geometry_seed = st.session_state.get("geometry_seed", 0)

layer, actual_n = build_geometry(N, LAYER, geometry_seed)

# Reassigning the trait invalidates this layer's entry in serialize.py's cache, so the
# payload is rebuilt - with identical geometry bytes and a different colour column.
colors = np.random.default_rng(color_seed + 10_000).integers(0, 255, size=(actual_n, 3))
layer.get_fill_color = colors.astype("uint8")

result = st_lonboard(
    layers=[layer],
    height=600,
    view_state={"longitude": CENTER[1], "latitude": CENTER[0], "zoom": 9},
    key="map",
)
st.write(
    {
        "layer": LAYER,
        "cells": actual_n,
        "color_seed": color_seed,
        "geometry_seed": geometry_seed,
        "clicked": result.clicked,
    }
)
