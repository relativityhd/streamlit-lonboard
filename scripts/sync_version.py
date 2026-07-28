#!/usr/bin/env python3
"""Release plumbing: propagate the version out of pyproject.toml, and roll the changelog.

`uv version --bump <level>` owns the version bump itself - it rewrites
`[project].version` in pyproject.toml *and* re-locks, keeping the copy of the
version inside uv.lock in sync (a stale lock would break `uv sync --locked`).
This script picks up from there and handles everything uv does not know about:

    sync <X.Y.Z>   mirror the version into frontend/package.json, then rewrite the
                   changelog's "## [Unreleased]" section into "## [X.Y.Z] - <date>"
                   and open a fresh empty Unreleased section above it
    notes <X.Y.Z>  print that version's changelog section (used as GitHub release notes)
    current        print the version currently in pyproject.toml

Stdlib only, and deliberately no `tomllib` (which is 3.11+) so this also runs on
the project's minimum Python. It is called from .github/workflows/release.yml but
is equally runnable by hand - see docs/RELEASING.md.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
PACKAGE_JSON = ROOT / "frontend" / "package.json"
CHANGELOG = ROOT / "CHANGELOG.md"

UNRELEASED_HEADING = "## [Unreleased]"
# Matches "## [1.2.3] - 2026-07-27" as well as the Unreleased heading.
VERSION_HEADING = re.compile(r"^## \[(?P<version>[^\]]+)\]")


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def read_current_version() -> str:
    """Return `[project].version` from pyproject.toml.

    Section-aware on purpose: a plain "first `version =` in the file" match would
    happily pick up a `version` key from some other table if one is ever added.
    """
    section = None
    for line in PYPROJECT.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped
        elif section == "[project]" and stripped.startswith("version"):
            match = re.match(r'version\s*=\s*"([^"]+)"', stripped)
            if match:
                return match.group(1)
    fail("no [project].version found in pyproject.toml")
    raise AssertionError("unreachable")


def sync_package_json(version: str) -> None:
    """Mirror the Python version into the (private, unpublished) frontend package.

    Nothing consumes this version - the frontend is bundled into the wheel, never
    published to npm - but letting it drift makes the tree confusing to read and
    makes a bundle hard to trace back to a release.
    """
    raw = PACKAGE_JSON.read_text(encoding="utf-8")
    data = json.loads(raw)
    if data.get("version") == version:
        return
    # Rewritten with a targeted regex rather than json.dumps so the file keeps its
    # existing key order and formatting, leaving a one-line diff.
    updated, count = re.subn(r'("version"\s*:\s*)"[^"]*"', rf'\g<1>"{version}"', raw, count=1)
    if count != 1:
        fail(f"could not locate a version field to rewrite in {PACKAGE_JSON}")
    PACKAGE_JSON.write_text(updated, encoding="utf-8")


def split_changelog(text: str) -> tuple[str, str, str]:
    """Split the changelog into (preamble, unreleased body, remainder)."""
    lines = text.splitlines(keepends=True)
    try:
        start = next(i for i, line in enumerate(lines) if line.startswith(UNRELEASED_HEADING))
    except StopIteration:
        fail(f"{CHANGELOG.name} has no '{UNRELEASED_HEADING}' heading")
        raise AssertionError("unreachable")

    end = len(lines)
    for i in range(start + 1, len(lines)):
        if VERSION_HEADING.match(lines[i]):
            end = i
            break

    return "".join(lines[:start]), "".join(lines[start + 1 : end]), "".join(lines[end:])


def roll_changelog(version: str, today: str) -> None:
    text = CHANGELOG.read_text(encoding="utf-8")
    preamble, body, remainder = split_changelog(text)

    if not body.strip():
        fail(
            f"the '{UNRELEASED_HEADING}' section of {CHANGELOG.name} is empty - "
            "describe what changed before cutting a release"
        )

    new = (
        f"{preamble}"
        f"{UNRELEASED_HEADING}\n\n"
        f"## [{version}] - {today}\n\n"
        f"{body.lstrip(chr(10))}"
        f"{remainder}"
    )
    CHANGELOG.write_text(new, encoding="utf-8")


def extract_notes(version: str) -> str:
    """Return the changelog body for `version`, for use as GitHub release notes."""
    lines = CHANGELOG.read_text(encoding="utf-8").splitlines(keepends=True)
    start = None
    for i, line in enumerate(lines):
        match = VERSION_HEADING.match(line)
        if match and match.group("version") == version:
            start = i + 1
            break
    if start is None:
        fail(f"no '## [{version}]' section found in {CHANGELOG.name}")
        raise AssertionError("unreachable")

    end = len(lines)
    for i in range(start, len(lines)):
        if VERSION_HEADING.match(lines[i]):
            end = i
            break
    return "".join(lines[start:end]).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sync = sub.add_parser("sync", help="mirror the version and roll the changelog")
    sync.add_argument("version", help="the version just written to pyproject.toml")
    sync.add_argument("--date", default=None, help="release date (YYYY-MM-DD); defaults to today, UTC")

    notes = sub.add_parser("notes", help="print a version's changelog section")
    notes.add_argument("version")

    sub.add_parser("current", help="print the version in pyproject.toml")

    args = parser.parse_args()

    if args.command == "current":
        print(read_current_version())
        return

    if args.command == "notes":
        print(extract_notes(args.version))
        return

    expected = read_current_version()
    if args.version != expected:
        fail(f"pyproject.toml says {expected!r} but sync was asked for {args.version!r}")

    date = args.date or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    sync_package_json(args.version)
    roll_changelog(args.version, date)
    print(f"synced version {args.version} (released {date})")


if __name__ == "__main__":
    main()
