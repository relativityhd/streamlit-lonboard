"""Streamlit custom component (v2) wrapper around the frontend bundle."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import streamlit as st
import streamlit.components.v2 as components
from lonboard import Map
from lonboard.basemap import CartoStyle

from ._perf import perf_enabled, span
from .serialize import (
    ACCESSOR_GEOMETRY_LAYER_TYPES,
    Compression,
    TooltipSpec,
    check_parameters_json_safe,
    check_payload_size,
    pack_payload,
    serialize_controls,
    serialize_layer_cached,
)

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
    picking_radius: int | None = None,
    parameters: dict[str, Any] | None = None,
    use_device_pixels: bool | float | None = None,
    custom_attribution: str | list[str] | None = None,
    tooltip: bool | list[str] = False,
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

    H3/S2/A5/Geohash/Arc layers carry their geometry in accessor columns
    (cell IDs, point pairs) rather than a bounding geometry column, so
    lonboard cannot compute a default view state for them (H3 can, but only
    if `h3-py` is installed). Pass an explicit `view_state=` when using these
    layer types, or a degenerate `{0, 0, 0}` view will be used and a warning
    shown.

    `compression` controls gzip compression of the Arrow payload:
    - `"auto"` (default): compress only above
      `serialize.AUTO_COMPRESSION_THRESHOLD` (1MB) of raw data. Worth it for
      remote deployments; on localhost the extra CPU usually costs more than
      it saves (see IMPLEMENTATION_PLAN.md §2 and Phase 4d), which is why the
      threshold exists instead of always compressing.
    - `"gzip"`: always compress, regardless of size.
    - `None`: never compress.

    `picking_radius`, `parameters`, `use_device_pixels`, and `custom_attribution`
    each default to `None`, which means "use the passed `map`'s own value"
    (`lonboard.Map.picking_radius` etc.) - pass an explicit value to override
    it, or omit both a `map` and these to get lonboard's own defaults (5,
    `None`, `None`, `None` respectively).

    `map.controls` (default: a fullscreen button, zoom/compass buttons, and a
    scale bar - lonboard's own default) is always forwarded; there is no
    separate `controls=` parameter, since controls only make sense attached to
    a `Map`. `GeocoderControl` isn't supported (it needs a Python-side async
    handler wired over lonboard's ipywidgets comm channel, which Streamlit has
    no equivalent for) and is skipped with a warning if present.

    `selected_index` / `selected_bounds` (lonboard's own click/box-select
    output traits) aren't forwarded - use `StLonboardResult.clicked` instead.

    `tooltip` controls the hover tooltip: `False` (default) shows none;
    `True` shows every non-geometry column of each layer's own data (falls
    back to the passed `map`'s `show_tooltip` when left at `False`); a list of
    column names shows only those (per layer - a name absent from a
    particular layer's data is silently skipped, since this one setting
    applies to every layer passed to this call). Requires `pickable=True` on
    the layer (lonboard's own default). Shipping `True` on a layer with many
    attribute columns adds that data to the payload on every rerun - prefer
    an explicit column list for anything beyond a quick look.
    """
    with span("st_lonboard.total"):
        if map is not None and layers is not None:
            raise ValueError("st_lonboard: pass either `map` or `layers`, not both")

        if map is None:
            map = Map(layers=list(layers or []))

        if view_state is None:
            view_state = _view_state_to_dict(map.view_state)
            if (
                view_state["longitude"] == 0
                and view_state["latitude"] == 0
                and view_state["zoom"] == 0
                and any(layer._layer_type in ACCESSOR_GEOMETRY_LAYER_TYPES for layer in map.layers)
            ):
                st.warning(
                    "st_lonboard: couldn't compute a default view state (H3/S2/A5/"
                    "Geohash/Arc layers store geometry in accessor columns, not a "
                    "bounding geometry column - H3 needs `h3-py` installed for "
                    "auto-centering). Pass an explicit `view_state=` to `st_lonboard()`."
                )
        if basemap_style == CartoStyle.PositronNoLabels and map.basemap is not None:
            basemap_style = str(map.basemap.style)

        # `None` means "inherit from `map`" for all four - `map` always has a
        # concrete value by this point (lonboard's own defaults, if the caller
        # built it via `Map(layers=...)` above rather than passing one directly).
        if picking_radius is None:
            picking_radius = map.picking_radius
        if parameters is None:
            parameters = map.parameters
        if use_device_pixels is None:
            use_device_pixels = map.use_device_pixels
        if custom_attribution is None:
            custom_attribution = map.custom_attribution
        check_parameters_json_safe(parameters)

        if tooltip is False and map.show_tooltip:
            tooltip = True
        tooltip_spec: TooltipSpec = tooltip if isinstance(tooltip, bool) else tuple(tooltip)

        with span("serialize_layers"):
            serialized = [
                serialize_layer_cached(layer, f"layer-{i}", tooltip_spec) for i, layer in enumerate(map.layers)
            ]

        map_options: dict[str, Any] = {
            "basemapStyle": str(basemap_style),
            "height": height,
            "onClick": on_click,
            "onHover": on_hover,
            "returnViewState": return_view_state,
            "perf": perf_enabled(),
            # `picking_radius` always has a concrete value by now (lonboard's
            # own `Map.picking_radius` defaults to 5, never `None`), unlike
            # the fields below - keep it unconditional for that reason.
            "pickingRadius": picking_radius,
        }
        # Omitted when left at lonboard's own "unset" default, matching how
        # `extensions` is omitted per-layer when empty (serialize.py) - keeps
        # the common case's payload minimal.
        if use_device_pixels is not None:
            map_options["useDevicePixels"] = use_device_pixels
        if parameters is not None:
            map_options["parameters"] = parameters
        if custom_attribution is not None:
            map_options["customAttribution"] = custom_attribution
        controls = serialize_controls(map.controls)
        if controls:
            map_options["controls"] = controls

        payload = pack_payload(
            serialized,
            view_state=view_state,
            map_options=map_options,
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
