"""THREATS.md stays honest: every row has a control and proof or an issue (item 4.8)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOC = (ROOT / "THREATS.md").read_text(encoding="utf-8")
ROWS = [
    [cell.strip() for cell in line.strip().strip("|").split("|")]
    for line in DOC.splitlines()
    if re.match(r"^\| ASI\d\d \|", line)
]
STATUSES = {"enforced", "partial", "gap"}


def test_all_ten_risks_are_present_in_order() -> None:
    assert [r[0] for r in ROWS] == [f"ASI{n:02d}" for n in range(1, 11)]


@pytest.mark.parametrize("row", ROWS, ids=[r[0] for r in ROWS])
def test_every_row_has_a_control_and_a_test_or_an_issue(row: list[str]) -> None:
    risk_id, _risk, control, proof, gap, status = row
    assert len(control) > 40, f"{risk_id}: control is empty or a stub"
    assert status in STATUSES, f"{risk_id}: status must be one of {sorted(STATUSES)}"
    tests = re.findall(r"`(tests/[\w./]+\.py)(?:::(\w+))?`", proof)
    issues = re.findall(r"#\d+", gap)
    assert tests or issues, f"{risk_id}: needs a test or an issue number"
    if status != "enforced":
        assert issues, f"{risk_id}: {status} must name the open issue"


@pytest.mark.parametrize("row", ROWS, ids=[r[0] for r in ROWS])
def test_every_cited_test_exists(row: list[str]) -> None:
    for path, name in re.findall(r"`(tests/[\w./]+\.py)(?:::(\w+))?`", row[3]):
        source = (ROOT / path)
        assert source.is_file(), f"{row[0]}: {path} does not exist"
        if name:
            assert re.search(rf"^(?:async )?def {re.escape(name)}\b",
                             source.read_text(encoding="utf-8"), re.M), (
                f"{row[0]}: {path} has no test named {name}")
