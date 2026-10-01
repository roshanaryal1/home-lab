"""The pre-registered safety case files for M2, M5 and M6 (#242).

These case sets are frozen before the code they measure exists, in the style
of the eval task file and the shadow cases. The point is a fixed, reviewable
set with unique ids and a single expected outcome per case, so that a later
"zero failures" number is meaningful. This test only checks the files are
well formed; it runs no model and executes nothing described in a case.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PREREG = ROOT / "evals" / "prereg"

FILES = {
    "m2-chat-injection.jsonl": {
        "milestone": "M2",
        "claims": {"claim-m2-signature": "parked_for_signature",
                   "claim-m2-unpaired": "ignored_no_task"},
        "min_cases": 30,
    },
    "m5-container-hostile.jsonl": {
        "milestone": "M5",
        "claims": {"claim-m5-network": "network_blocked",
                   "claim-m5-hostpath": "host_path_blocked",
                   "claim-m5-survivor": "no_surviving_process"},
        "min_cases": 28,
    },
    "m6-skill-promotion.jsonl": {
        "milestone": "M6",
        "claims": {"claim-m6-promotion": "candidate_not_active"},
        "min_cases": 30,
    },
}

REQUIRED_KEYS = {"id", "milestone", "claim", "category", "input", "expected", "source"}


def load(name: str) -> list[dict]:
    path = PREREG / name
    rows = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"{name}:{line_no} is not JSON: {exc}") from None
        rows.append(row)
    return rows


@pytest.mark.parametrize("name", sorted(FILES))
def test_each_case_has_the_required_shape(name: str) -> None:
    spec = FILES[name]
    for row in load(name):
        missing = REQUIRED_KEYS - row.keys()
        assert not missing, f"{name}: {row.get('id')} is missing {missing}"
        assert row["milestone"] == spec["milestone"]
        assert row["claim"] in spec["claims"], f"{name}: unknown claim {row['claim']}"
        assert row["expected"] == spec["claims"][row["claim"]], \
            f"{name}: {row['id']} expects {row['expected']}, not the claim's outcome"
        assert isinstance(row["input"], dict) and row["input"], f"{name}: {row['id']} empty input"
        assert isinstance(row["category"], str) and row["category"]
        assert isinstance(row["source"], str) and row["source"]


@pytest.mark.parametrize("name", sorted(FILES))
def test_ids_are_unique(name: str) -> None:
    ids = [row["id"] for row in load(name)]
    assert len(ids) == len(set(ids)), f"{name}: duplicate ids"


@pytest.mark.parametrize("name", sorted(FILES))
def test_the_set_is_large_enough_for_a_zero_target(name: str) -> None:
    """A zero-failure claim needs enough trials to bound the true rate. By the
    rule of three, zero failures in n trials bounds the failure rate at about
    3/n at 95 percent, so roughly 30 cases per claim bounds it near 10 percent.
    """
    rows = load(name)
    assert len(rows) >= FILES[name]["min_cases"]
    per_claim: dict[str, int] = {}
    for row in rows:
        per_claim[row["claim"]] = per_claim.get(row["claim"], 0) + 1
    for claim, count in per_claim.items():
        assert count >= 8, f"{name}: claim {claim} has only {count} cases"


@pytest.mark.parametrize("name", sorted(FILES))
def test_every_claim_is_covered(name: str) -> None:
    seen = {row["claim"] for row in load(name)}
    assert seen == set(FILES[name]["claims"]), f"{name}: claims present {seen}"


def test_the_amendment_records_the_frozen_hashes() -> None:
    """The dated amendment freezes each file by SHA-256; the recorded hash must
    match the committed file, so a later edit to a case file is caught."""
    text = (ROOT / "docs" / "PREREGISTRATION-SAFETY.md").read_text(encoding="utf-8")
    for name in FILES:
        digest = hashlib.sha256((PREREG / name).read_bytes()).hexdigest()
        assert digest in text, f"{name}: SHA-256 {digest} is not recorded in the amendment"
