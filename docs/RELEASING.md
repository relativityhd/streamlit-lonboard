# Releasing streamlit-lonboard

Releases are cut by a manually-triggered GitHub Actions workflow. Nothing is ever
published by a plain push to `main`.

## The normal path

1. Make sure everything you want in the release is merged into `main`, and that
   the `## [Unreleased]` section of [`CHANGELOG.md`](../CHANGELOG.md) describes it.
   The release refuses to run if that section is empty.
2. GitHub → **Actions** → **Release** → **Run workflow**.
3. Pick a bump level and run it.

That is the whole process. Everything below is what the workflow does on your
behalf, and what to do when it goes sideways.

## Bump levels

The version lives in `pyproject.toml` and is bumped by `uv version --bump`.
That is the single source of truth: `scripts/sync_version.py` mirrors it into
`frontend/package.json`, `uv version` re-locks `uv.lock`, and
`streamlit_lonboard.__version__` reads it back out of the installed distribution
metadata at import time. Do not hardcode the version anywhere else - an
unsynced copy silently goes stale (`__version__` used to be one, and still read
`0.1.0.dev0` after `0.2.0` shipped).

| Input    | `0.1.0.dev0` becomes | Use when                                        |
| -------- | -------------------- | ----------------------------------------------- |
| `stable` | `0.1.0`              | Dropping a pre-release suffix. **First release.**|
| `patch`  | `0.1.1`              | Bug fixes only, no API change.                   |
| `minor`  | `0.2.0`              | New features, backwards compatible.              |
| `major`  | `1.0.0`              | Breaking changes to `st_lonboard()`'s signature or behaviour. |

Below `1.0.0`, SemVer treats the whole API as unstable — but this project is a
single public function, so it is worth being disciplined anyway: anything that
would break an existing `st_lonboard(...)` call is a `minor` bump pre-1.0 and a
`major` bump after.

## What the workflow does

`.github/workflows/release.yml`, in order:

1. **checks** — runs `.github/workflows/ci.yml` unchanged (ruff, the Python
   3.11–3.14 test matrix on Linux plus macOS/Windows smoke tests, a frontend
   typecheck and build, and a trial packaging run). A release can only be cut
   from a commit that passes everything a PR has to pass.
2. **release** — refuses to run off `main`; bumps the version with
   `uv version --no-sync --bump <level>` (which also re-locks, because `uv.lock`
   stores the project version too); refuses to reuse an existing tag; runs
   `scripts/sync_version.py sync`, which mirrors the version into
   `frontend/package.json` and rewrites `## [Unreleased]` into a dated section;
   builds the frontend with `npm ci` (lockfile-exact) and then the sdist and
   wheel; verifies the wheel really contains `frontend_dist/`; commits, tags
   `vX.Y.Z`, and pushes.
3. **pypi** — publishes via [Trusted Publishing](https://docs.pypi.org/trusted-publishers/).
   No PyPI token is stored anywhere; PyPI validates a short-lived OIDC token
   minted for this repo, workflow and environment.
4. **announce** — creates the GitHub Release from the new changelog section and
   attaches the sdist and wheel.

## Dry run

Tick **dry run** to exercise steps 1–2 without tagging, pushing or publishing.
The built artifacts and the release diff are attached to the run, so you can
inspect exactly what would ship. Use it the first time, and any time the
packaging setup changes.

## Doing it by hand

The workflow is a wrapper around commands you can run locally:

```bash
uv version --no-sync --bump patch
python scripts/sync_version.py sync "$(uv version --short)"
(cd frontend && npm ci && npm run build)
STREAMLIT_LONBOARD_SKIP_FRONTEND_BUILD=1 uv build
uvx twine check --strict dist/*
```

Note the `SKIP_FRONTEND_BUILD` env var: the bundle was already built from the
lockfile, so the Hatchling hook is told to stand down rather than re-running a
loose `npm install`.

## When something goes wrong

**Failed after the tag was pushed (e.g. PyPI rejected the upload).** The tag and
release commit are already on `main`. PyPI never allows re-uploading a version,
even a deleted one, so fix the problem and cut the next patch release. Do not
try to reuse the version.

**The changelog gate fired.** The `## [Unreleased]` section is empty. Write it,
merge, and re-run.

**Publishing is stuck "waiting".** That is the `pypi` environment's required
reviewer gate, if you configured one. Approve it from the run page.

**A bad release reached PyPI.** Yank it (`Manage project → Releases → Yank` on
PyPI) rather than deleting it — yanking leaves existing pinned installs working
while keeping new resolutions off it — then release a fix.
