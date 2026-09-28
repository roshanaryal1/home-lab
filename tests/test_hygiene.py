"""The test configuration itself is a safety property (item 2.3, #60)."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


def _config() -> dict[str, Any]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]


def test_unregistered_markers_are_errors() -> None:
    assert "--strict-markers" in _config()["pytest"]["ini_options"]["addopts"]


def test_safety_marker_is_registered() -> None:
    markers = _config()["pytest"]["ini_options"]["markers"]
    assert any(m.startswith("safety:") for m in markers)


def test_mypy_runs_strict_on_the_package() -> None:
    mypy = _config()["mypy"]
    assert mypy["strict"] is True
    assert mypy["files"] == ["lab"]
