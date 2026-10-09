"""CHANGELOG.md has a heading for the version in pyproject.toml (#332)."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _package_version() -> str:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    return str(project["version"])


def _version_headings() -> list[str]:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    return re.findall(r"^## \[([^\]]+)\]", text, flags=re.MULTILINE)


def test_changelog_has_a_heading_for_the_package_version() -> None:
    version = _package_version()
    headings = _version_headings()
    assert version in headings, f"CHANGELOG.md needs a '## [{version}]' heading: {headings}"


def test_changelog_starts_with_an_unreleased_section() -> None:
    assert _version_headings()[:1] == ["Unreleased"]
