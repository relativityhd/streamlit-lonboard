"""streamlit-lonboard: render lonboard maps in Streamlit via Arrow, not GeoJSON."""

from ._widget_patch import apply_widget_comm_patch

# Before anything else, and before .component pulls in lonboard: layers are built in
# user code, usually right after this import, and every lonboard widget constructed
# before the patch lands pays for a Jupyter comm message that Streamlit discards.
# See _widget_patch.py, and STREAMLIT_LONBOARD_KEEP_WIDGET_COMM=1 to opt out.
apply_widget_comm_patch()

from .component import StLonboardResult, st_lonboard  # noqa: E402

__all__ = ["st_lonboard", "StLonboardResult"]
__version__ = "0.1.0.dev0"
