"""Real-data twin of bench_app.py, driven by wire_options_real.py.

Usage:
    BENCH_SCENARIO=a5-cells BENCH_LEVEL=L08 BENCH_COMPRESSION=parquet \
        ST_LONBOARD_PERF=1 uv run streamlit run benchmarks/bench_real_app.py

Same two buttons as bench_app.py: "Rerun (unchanged data)" (byte-identical
payload; CCv2 skips mount() entirely) and "Rerun (new data)" (perturbs the
color accessor so the payload genuinely differs, forcing a real mount() -
what the browser-decode measurement uses). See real_scenarios.py for the
scenario definitions and A5_DATA_ROOT.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import streamlit as st
from real_scenarios import ARCTIC_VIEW, build_scenario

from streamlit_lonboard import st_lonboard

SCENARIO = os.environ.get("BENCH_SCENARIO", "a5-cells")
LEVEL = os.environ.get("BENCH_LEVEL", "L08")
_compression_env = os.environ.get("BENCH_COMPRESSION", "auto")
COMPRESSION = None if _compression_env == "none" else _compression_env

st.title(f"Real data: {SCENARIO} @ {LEVEL} (compression={COMPRESSION!r})")
col1, col2 = st.columns(2)
col1.button("Rerun (unchanged data)")
if col2.button("Rerun (new data)"):
    st.session_state["seed"] = st.session_state.get("seed", 0) + 1


@st.cache_resource
def build(scenario: str, level: str):
    return build_scenario(scenario, level)


layer, tooltip_columns, base_colors = build(SCENARIO, LEVEL)

# "New data" rerun: nudge the green channel by seed so the Arrow bytes
# differ without rebuilding the (cached) layer from disk. Mutating a trait
# also evicts serialize_layer_cached's entry, like a real data update would.
seed = st.session_state.get("seed", 0)
if seed:
    colors = base_colors.copy()
    colors[:, 1] = (colors[:, 1].astype(np.uint16) + seed) % 256
    layer.get_fill_color = colors

st_lonboard(
    layers=[layer],
    view_state=ARCTIC_VIEW,
    height=600,
    compression=COMPRESSION,
    tooltip=list(tooltip_columns),
    key="bench-real",
)
