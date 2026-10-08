"""The H1 interval reproduces the registered numbers, and the H1b interval file is what the script
prints (#321)."""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent
REVIEW = ROOT / "evals" / "h1_review"
H1_SHA256 = "9d06bed917dbea2e1d83e29856b820d68c9c9c5cd1f3967363f3f112148b8b3d"
H1B_SHA256 = "e3b4dda4a674407025b7eb06380317578e89ec9a79aa4c868fa4dcfde62e0e9b"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("h1_result", ROOT / "scripts" / "h1_result.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def repo(monkeypatch: pytest.MonkeyPatch) -> None:
    # The script reads its inputs by repository-relative path, as it does when run from the root.
    monkeypatch.chdir(ROOT)


def test_the_h1_interval_reproduces_the_registered_numbers(
        repo: None, capsys: pytest.CaptureFixture[str]) -> None:
    assert hashlib.sha256((REVIEW / "h1-run.json").read_bytes()).hexdigest() == H1_SHA256
    _script().main([])
    out = capsys.readouterr().out
    assert ("accuracy gain -0.733, 95% CI [-0.867, -0.567] (paired bootstrap, seed 20260930)"
            in out)
    _script().main(["--record", "evals/h1_review/h1-run.json"])
    lines = capsys.readouterr().out.splitlines()
    assert "accuracy, candidate: 3 of 30 (0.100)" in lines
    assert "accuracy, rubric: 25 of 30 (0.833)" in lines
    assert lines[-1] == "accuracy gain -0.733, 95% CI [-0.867, -0.567]"


def test_the_h1b_interval_file_is_what_the_script_prints(
        repo: None, capsys: pytest.CaptureFixture[str]) -> None:
    assert hashlib.sha256((REVIEW / "h1b-run.json").read_bytes()).hexdigest() == H1B_SHA256
    _script().main(["--record", "evals/h1_review/h1b-run.json"])
    printed = capsys.readouterr().out
    committed = (REVIEW / "h1b-interval.txt").read_text(encoding="utf-8")
    assert printed == committed
    assert committed.startswith("EXPLORATORY.")
    assert f"record SHA-256: {H1B_SHA256}" in committed
    assert "resamples: 10,000, seed: 20260930" in committed
    assert "accuracy, candidate: 13 of 30 (0.433)" in committed
    assert "accuracy, rubric: 25 of 30 (0.833)" in committed
    assert "accuracy gain -0.400, 95% CI [-0.600, -0.200]" in committed
    assert "—" not in committed
