"""Streamlit custom component (v2) wrapper around the frontend bundle."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# TODO(phase-0): confirm import path & minimum Streamlit version for CCv2
# import streamlit.components.v2 as components

from .serialize import serialize_layer


@dataclass
class StLonboardResult:
    """State returned from the frontend."""

    clicked: dict | None = None      # {layer_id, index, coordinate}
    hovered: dict | None = None
    view_state: dict | None = None


def st_lonboard(
    map: Any | None = None,
    layers: list[Any] | None = None,
    *,
    view_state: dict | None = None,
    height: int = 500,
    on_click: bool = True,
    on_hover: bool = False,
    return_view_state: bool = False,
    key: str | None = None,
) -> StLonboardResult:
    """Render lonboard layers in Streamlit.

    Accepts either a lonboard.Map or a list of lonboard layers.

    TODO(phase-1):
    - serialize layers via serialize_layer()
    - mount via st.components.v2.component(data={...}, key=key)
    - wrap BidiComponentResult state into StLonboardResult
    """
    raise NotImplementedError
