"""streamlit-lonboard: render lonboard maps in Streamlit via Arrow, not GeoJSON."""

from importlib.metadata import PackageNotFoundError, version

from ._widget_patch import apply_widget_comm_patch

# Before anything else, and before .component pulls in lonboard: layers are built in
# user code, usually right after this import, and every lonboard widget constructed
# before the patch lands pays for a Jupyter comm message that Streamlit discards.
# See _widget_patch.py, and STREAMLIT_LONBOARD_KEEP_WIDGET_COMM=1 to opt out.
apply_widget_comm_patch()

from .component import StLonboardResult, st_lonboard  # noqa: E402

__all__ = ["st_lonboard", "StLonboardResult"]

# Read out of the installed distribution metadata rather than hardcoded here.
# pyproject.toml is the single source of truth for the version, and
# scripts/sync_version.py mirrors it into frontend/package.json; a literal in this
# file would be a third copy that nothing syncs, which is exactly how it came to
# still read "0.1.0.dev0" after 0.2.0 shipped.
try:
    __version__ = version("streamlit-lonboard")
except PackageNotFoundError:  # source tree with no install - report it, don't guess
    __version__ = "0.0.0+unknown"
