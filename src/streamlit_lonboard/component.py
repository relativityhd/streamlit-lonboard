"""Streamlit custom component (v2) wrapper around the frontend bundle."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import streamlit as st
import streamlit.components.v2 as components
from lonboard import Map
from lonboard.basemap import CartoStyle

from ._perf import perf_enabled, span
from .serialize import Compression, check_payload_size, pack_payload, serialize_layer_cached

# Fully-qualified name = "<project name from pyproject.toml>.<[tool.streamlit.component] entry>".
_COMPONENT_NAME = "streamlit-lonboard.lonboard_map"

# Registered once at import time (not per-call, so we don't re-register on every rerun).
_mount = components.component(_COMPONENT_NAME, js="index.js", css="index.css")


@dataclass
class StLonboardResult:
    """State returned from the frontend."""

    clicked: dict | None = None  # {layer_id, index, coordinate}
    hovered: dict | None = None
    view_state: dict | None = None


def _view_state_to_dict(vs: Any) -> dict[str, float]:
    return {
        "longitude": vs.longitude,
        "latitude": vs.latitude,
        "zoom": vs.zoom,
        "pitch": vs.pitch,
        "bearing": vs.bearing,
    }


def st_lonboard(
    map: Map | None = None,
    layers: list[Any] | None = None,
    *,
    view_state: dict[str, float] | None = None,
    basemap_style: str = CartoStyle.PositronNoLabels,
    height: int = 500,
    on_click: bool = True,
    on_hover: bool = False,
    return_view_state: bool = False,
    compression: Compression = "auto",
    key: str | None = None,
) -> StLonboardResult:
    """Render lonboard layers in Streamlit using Arrow end to end (no GeoJSON).

    Accepts either a `lonboard.Map` (its layers, view state, and basemap are
    used) or a plain list of lonboard layers (in which case a `Map` is built
    internally, purely to reuse lonboard's own default-view-state /
    bounds-fitting logic).

    Pass a stable `key` if you want the user's pan/zoom to survive Streamlit
    reruns triggered by unrelated widgets elsewhere in the app — without one,
    the component's identity is derived from the serialized data and may
    remount when that data changes. A `key` is also *required* (Streamlit will
    raise `StreamlitDuplicateElementId`) if your app calls `st_lonboard()`
    more than once with byte-identical arguments in the same script run —
    same as any other Streamlit element.

    `compression` controls gzip compression of the Arrow payload:
    - `"auto"` (default): compress only above
      `serialize.AUTO_COMPRESSION_THRESHOLD` (1MB) of raw data. Worth it for
      remote deployments; on localhost the extra CPU usually costs more than
      it saves (see IMPLEMENTATION_PLAN.md §2 and Phase 4d), which is why the
      threshold exists instead of always compressing.
    - `"gzip"`: always compress, regardless of size.
    - `None`: never compress.
    """
    with span("st_lonboard.total"):
        if map is not None and layers is not None:
            raise ValueError("st_lonboard: pass either `map` or `layers`, not both")

        if map is None:
            map = Map(layers=list(layers or []))

        if view_state is None:
            view_state = _view_state_to_dict(map.view_state)
        if basemap_style == CartoStyle.PositronNoLabels and map.basemap is not None:
            basemap_style = str(map.basemap.style)

        with span("serialize_layers"):
            serialized = [
                serialize_layer_cached(layer, f"layer-{i}") for i, layer in enumerate(map.layers)
            ]

        payload = pack_payload(
            serialized,
            view_state=view_state,
            map_options={
                "basemapStyle": str(basemap_style),
                "height": height,
                "onClick": on_click,
                "onHover": on_hover,
                "returnViewState": return_view_state,
                "perf": perf_enabled(),
            },
            compression=compression,
        )
        check_payload_size(payload, max_mb=st.get_option("server.maxMessageSize"))

        callbacks: dict[str, Any] = {}
        if on_click:
            callbacks["on_clicked_change"] = lambda: None
        if on_hover:
            callbacks["on_hovered_change"] = lambda: None
        if return_view_state:
            callbacks["on_view_state_change"] = lambda: None

        # Includes Streamlit's own per-message identity hashing + ForwardMsgCache
        # lookup - separate from "st_lonboard.total" so we know what's ours to
        # optimize vs. Streamlit-internal cost (see IMPLEMENTATION_PLAN.md 4.0).
        with span("mount (includes Streamlit identity hash + enqueue)"):
            result = _mount(data=payload, key=key, height=height, **callbacks)

    return StLonboardResult(
        clicked=result.get("clicked"),
        hovered=result.get("hovered"),
        view_state=result.get("view_state"),
    )
