"""Phase 4c: wire payload size comparison across contenders, no browser needed.

Usage: uv run python benchmarks/payload_sizes.py

Measures the size of what each contender actually ships to the frontend for
the same N points at the same scales as playwright_driver.py:
- st_lonboard: the framed Arrow IPC payload from serialize.pack_payload
  (compression=None, to isolate the format itself from Phase 4d's optional
  compression - see benchmarks/RESULTS.md for that separate comparison).
- pydeck: pydeck.Deck.to_json() - what st.pydeck_chart actually sends
  (JSON-encoded layers, one object per row).
- Map.to_html(): the full standalone HTML string (JS runtime + data inlined
  together - not an apples-to-apples "data payload", but it's the entire
  cost a user pays to get anything on screen with this approach at all).
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from datagen import points_dataframe, points_geodataframe  # noqa: E402

import pydeck as pdk  # noqa: E402
from lonboard import Map, ScatterplotLayer  # noqa: E402


def _load_serialize_module():
    """Load serialize.py directly, without triggering
    streamlit_lonboard/__init__.py -> component.py, which registers a CCv2
    component and requires a full Streamlit runtime + asset_dir manifest
    (neither available when running this as a plain script). Same trick as
    tests/test_serialize.py's _load_serialize_module."""
    pkg_dir = Path(__file__).parent.parent / "src" / "streamlit_lonboard"

    def _load(name: str):
        spec = importlib.util.spec_from_file_location(f"streamlit_lonboard.{name}", pkg_dir / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"streamlit_lonboard.{name}"] = module
        spec.loader.exec_module(module)
        return module

    if "streamlit_lonboard" not in sys.modules:
        pkg = types.ModuleType("streamlit_lonboard")
        pkg.__path__ = [str(pkg_dir)]
        sys.modules["streamlit_lonboard"] = pkg
    _load("_perf")
    return _load("serialize")


serialize = _load_serialize_module()
pack_payload = serialize.pack_payload
serialize_layer = serialize.serialize_layer

SCALES = [10_000, 100_000, 1_000_000, 10_000_000]


def lonboard_bytes(n: int) -> int:
    gdf = points_geodataframe(n)
    layer = ScatterplotLayer.from_geopandas(
        gdf,
        get_fill_color=gdf[["r", "g", "b"]].to_numpy(),
        get_radius=gdf["radius"].to_numpy(),
        radius_units="meters",
        pickable=True,
    )
    # Explicit plain-IPC: this benchmark isolates the *format's* size from
    # any compression (see the module docstring), so it must not follow the
    # "auto" default, which would ship Parquet at these scales.
    serialized = serialize_layer(layer, "layer-0", encoding="ipc")
    payload = pack_payload(
        [serialized], view_state=None, map_options={}, compression=None
    )
    return len(payload)


def pydeck_bytes(n: int) -> int:
    df = points_dataframe(n)
    layer = pdk.Layer(
        "ScatterplotLayer",
        data=df,
        get_position=["lon", "lat"],
        get_fill_color=["r", "g", "b"],
        get_radius="radius",
        radius_units="meters",
        pickable=True,
    )
    deck = pdk.Deck(layers=[layer], map_provider="carto")
    return len(deck.to_json().encode("utf-8"))


def tohtml_bytes(n: int) -> int:
    gdf = points_geodataframe(n)
    layer = ScatterplotLayer.from_geopandas(
        gdf,
        get_fill_color=gdf[["r", "g", "b"]].to_numpy(),
        get_radius=gdf["radius"].to_numpy(),
        radius_units="meters",
        pickable=True,
    )
    html = Map(layers=[layer]).to_html()
    return len(html.encode("utf-8"))


def main() -> None:
    results = []
    for n in SCALES:
        row = {"n": n}
        for name, fn in [
            ("st_lonboard", lonboard_bytes),
            ("pydeck", pydeck_bytes),
            ("to_html", tohtml_bytes),
        ]:
            try:
                row[name] = fn(n)
            except Exception as e:  # noqa: BLE001 - a DNF is a valid result here too
                row[name] = f"ERROR: {e}"
            print(f"n={n:,} {name}: {row[name]}", file=sys.stderr, flush=True)
        results.append(row)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
