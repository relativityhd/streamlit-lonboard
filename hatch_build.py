"""Hatchling build hook: build the frontend bundle before packaging the wheel.

Without this, `src/streamlit_lonboard/frontend_dist/` has to be built by hand
(`cd frontend && npm install && npm run build`) before the component renders
anything - easy to forget, and easy to leave stale after a frontend edit.
This hook runs that build automatically for `uv build`, `uv sync`,
`pip install .`, and `pip install -e .`.

Consumers installing a pre-built wheel from PyPI never hit this hook at all
(the wheel already contains `frontend_dist/`), so they never need Node.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

SKIP_ENV_VAR = "STREAMLIT_LONBOARD_SKIP_FRONTEND_BUILD"


class FrontendBuildHook(BuildHookInterface):
    PLUGIN_NAME = "frontend"

    def initialize(self, version: str, build_data: dict) -> None:
        if self.target_name != "wheel":
            return
        if os.environ.get(SKIP_ENV_VAR):
            return

        root = Path(self.root)
        frontend_dir = root / "frontend"
        dist_dir = root / "src" / "streamlit_lonboard" / "frontend_dist"

        npm = shutil.which("npm")
        if npm is None:
            if dist_dir.is_dir() and any(dist_dir.iterdir()):
                self.app.display_info(
                    "streamlit-lonboard: npm not found; reusing existing "
                    f"{dist_dir.relative_to(root)}/"
                )
                return
            raise RuntimeError(
                "streamlit-lonboard: npm not found and no prebuilt "
                f"{dist_dir.relative_to(root)}/ is present. Install Node.js/npm, "
                f"or set {SKIP_ENV_VAR}=1 to build without the frontend "
                "(the component won't render anything until it's built)."
            )

        self.app.display_info("streamlit-lonboard: building frontend (npm install && npm run build)")
        subprocess.run([npm, "install"], cwd=frontend_dir, check=True)
        subprocess.run([npm, "run", "build"], cwd=frontend_dir, check=True)
