"""The H1 chart must be reproducible, descriptive and sealed (#321)."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _figure() -> ModuleType:
    spec = importlib.util.spec_from_file_location("h1_figure", ROOT / "scripts" / "h1_figure.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_svg_exactly_reproducible_from_sealed_records() -> None:
    fig = _figure()
    rendered = fig.render()
    assert rendered.encode("utf-8") == (ROOT / "evals" / "h1_review" / "h1-results.svg").read_bytes()
    assert fig.main(["--check"]) == 0
    for text in (
        "25/30 (83.3%)",
        "3/30 (10.0%)",
        "13/30 (43.3%)",
        "5 / 30",
        "8 / 30",
        "REGISTERED",
        "EXPLORATORY",
        "H1 not supported",
        "not independent human ground truth",
    ):
        assert text in rendered
    assert "<title" in rendered and "<desc" in rendered
    assert "{{" not in rendered


def test_record_bytes_are_pinned_and_wrong_hash_is_refused(
        tmp_path: Path) -> None:
    fig = _figure()
    frozen = fig.H1_RECORD.read_bytes()
    assert fig.load_record(fig.H1_RECORD, fig.H1_SHA256)["valid"]
    mutated = tmp_path / "h1-run.json"
    mutated.write_bytes(frozen + b" ")
    with pytest.raises(ValueError, match="SHA-256"):
        fig.load_record(mutated, fig.H1_SHA256)


def test_summary_refuses_duplicate_or_inconsistent_cases() -> None:
    fig = _figure()
    record = fig.load_record(fig.H1_RECORD, fig.H1_SHA256)
    original = fig.summary(record)
    assert (original["base"], original["correct"], original["false_promotions"]) == (25, 3, 5)

    duplicate = copy.deepcopy(record)
    duplicate["report"]["rows"][0] = duplicate["report"]["rows"][1]
    with pytest.raises(ValueError, match="unique"):
        fig.summary(duplicate)

    wrong_result = copy.deepcopy(record)
    wrong_result["report"]["false_promotions"] = 0
    with pytest.raises(ValueError, match="disagree"):
        fig.summary(wrong_result)


def test_check_mode_does_not_touch_or_replace_figure(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fig = _figure()
    svg = tmp_path / "h1.svg"
    svg.write_text("sentinel", encoding="utf-8")
    monkeypatch.setattr(fig, "SVG", svg)
    with pytest.raises(SystemExit, match="differs from frozen records"):
        fig.main(["--check"])
    assert svg.read_text(encoding="utf-8") == "sentinel"
    assert fig.main(["--out", str(svg)]) == 0
    assert json.loads(fig.H1_RECORD.read_text(encoding="utf-8"))["valid"] is True
    assert svg.read_text(encoding="utf-8") == fig.render()


def test_template_is_pinned_even_if_placeholders_survive(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fig = _figure()
    modified = tmp_path / "modified-template.svg"
    raw = fig.TEMPLATE.read_bytes()
    assert b"#f9fafb" in raw
    modified.write_bytes(raw.replace(b"#f9fafb", b"#ffffff"))
    monkeypatch.setattr(fig, "TEMPLATE", modified)
    with pytest.raises(ValueError, match="template differs"):
        fig.render()


@pytest.mark.parametrize("protected", [
    "H1_RECORD", "H1B_RECORD", "TEMPLATE",
])
def test_out_does_not_overwrite_research_sources(protected: str) -> None:
    fig = _figure()
    source = getattr(fig, protected)
    original = source.read_bytes()
    with pytest.raises(SystemExit, match="refusing to overwrite"):
        fig.main(["--out", str(source)])
    assert source.read_bytes() == original


def test_exact_bytes_check_rejects_line_ending_changes(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fig = _figure()
    altered_svg = tmp_path / "different-newlines.svg"
    altered_svg.write_bytes(fig.render().replace("\n", "\r\n").encode("utf-8"))
    monkeypatch.setattr(fig, "SVG", altered_svg)
    with pytest.raises(SystemExit, match="differs"):
        fig.main(["--check"])
    assert altered_svg.read_bytes() != fig.render().encode("utf-8")


def test_failed_atomic_replace_keeps_previous_complete_svg(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fig = _figure()
    output = tmp_path / "figure.svg"
    output.write_bytes(b"previous-good-version")

    def fail_replace(src: object, dst: object) -> None:
        raise OSError("simulated interrupted replacement")

    monkeypatch.setattr(fig.os, "replace", fail_replace)
    with pytest.raises(OSError, match="interrupted"):
        fig.main(["--out", str(output)])
    assert output.read_bytes() == b"previous-good-version"
    assert not list(tmp_path.glob(".h1-figure-*.tmp"))
