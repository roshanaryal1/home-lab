"""The evidence ledger: claim status is not task status (item #90)."""

from __future__ import annotations

from pathlib import Path

import pytest

from lab.artifacts import ArtifactStore
from lab.cli import main
from lab.ledger import Ledger, LedgerError
from lab.queue import TaskQueue

PAGE_A = b"The measured throughput was 41 tokens per second on the test machine."
PAGE_B = b"Independent run: throughput 40 to 42 tokens per second."
PAGE_C = b"A different report says throughput was only 9 tokens per second."
CLAIM = "Throughput is about 41 tokens per second"


@pytest.fixture()
def q(tmp_path: Path) -> TaskQueue:
    with TaskQueue(tmp_path / "lab.db") as queue:
        yield queue


@pytest.fixture()
def ledger(q: TaskQueue, tmp_path: Path) -> Ledger:
    ledger = Ledger(q._conn, ArtifactStore(tmp_path / "artifacts", q._conn))
    ledger.open_research_task("r1", "What is the throughput?", "protocol-v1",
                              data_ids=["dataset-1"], outputs=["report.md"],
                              validation_checks=["two independent sources"])
    return ledger


def snap(ledger: Ledger, source: str, data: bytes) -> int:
    return ledger.add_snapshot("r1", source, "web", data)


def status(ledger: Ledger, claim: int) -> str:
    return next(c.status for c in ledger.claims("r1") if c.id == claim)


# ------------------------------------------------------------ the task record


def test_a_research_task_records_question_protocol_data_code_outputs_and_checks(ledger) -> None:
    row = ledger.research_task("r1")
    assert row["question"] == "What is the throughput?" and row["protocol_version"] == "protocol-v1"
    assert '"dataset-1"' in row["data_ids"] and "report.md" in row["outputs"]
    assert "two independent sources" in row["validation_checks"]
    assert len(__import__("json").loads(row["code_ids"])["lab_commit"]) == 40


def test_a_task_id_opens_once_and_unknown_tasks_are_refused(ledger) -> None:
    with pytest.raises(LedgerError, match="already exists"):
        ledger.open_research_task("r1", "again", "p")
    with pytest.raises(LedgerError, match="no research task"):
        ledger.add_claim("ghost", "x")
    with pytest.raises(LedgerError):
        ledger.open_research_task("r2", "", "p")


# ------------------------------------------------------------------ statuses


@pytest.mark.safety
def test_a_completed_task_with_an_unsupported_claim_shows_the_claim_unverified(
        ledger, q) -> None:
    """Task completed is not claim verified."""
    claim = ledger.add_claim("r1", CLAIM)
    q._conn.execute("INSERT INTO tasks (id, title, state) VALUES ('r1', 't', 'succeeded')")
    assert q.get("r1").state == "succeeded"
    assert status(ledger, claim) == "unverified"
    ledger.run_review_pass("r1", "checker")
    assert status(ledger, claim) == "unverified"


def test_supporting_evidence_makes_a_claim_supported_but_never_verified(ledger) -> None:
    claim = ledger.add_claim("r1", CLAIM)
    s1 = snap(ledger, "https://a.example/report", PAGE_A)
    ledger.link(claim, s1, "supports", "41 tokens per second")
    assert status(ledger, claim) == "supported"
    s2 = snap(ledger, "https://b.example/run", PAGE_B)
    ledger.link(claim, s2, "supports", "40 to 42 tokens")
    assert status(ledger, claim) == "supported", "more evidence does not verify by itself"


@pytest.mark.safety
def test_contradicting_evidence_wins_and_blocks_verification(ledger) -> None:
    claim = ledger.add_claim("r1", CLAIM)
    ledger.link(claim, snap(ledger, "https://a.example", PAGE_A), "supports", "41 tokens")
    ledger.link(claim, snap(ledger, "https://c.example", PAGE_C), "contradicts", "only 9 tokens")
    assert status(ledger, claim) == "contradicted"
    ledger.run_review_pass("r1", "checker")
    with pytest.raises(LedgerError, match="contradicted"):
        ledger.verify(claim, "roshan")


def test_a_quote_the_source_does_not_contain_is_refused(ledger) -> None:
    claim = ledger.add_claim("r1", CLAIM)
    s1 = snap(ledger, "https://a.example", PAGE_A)
    with pytest.raises(LedgerError, match="does not appear"):
        ledger.link(claim, s1, "supports", "throughput was 99 tokens per second")
    assert status(ledger, claim) == "unverified"
    assert ledger.evidence(claim) == []


def test_a_snapshot_from_another_task_or_a_bad_relation_is_refused(ledger) -> None:
    ledger.open_research_task("r2", "other", "p")
    other = ledger.add_snapshot("r2", "https://x", "web", PAGE_A)
    claim = ledger.add_claim("r1", CLAIM)
    with pytest.raises(LedgerError, match="different research task"):
        ledger.link(claim, other, "supports", "41 tokens")
    s1 = snap(ledger, "https://a.example", PAGE_A)
    with pytest.raises(LedgerError, match="relation"):
        ledger.link(claim, s1, "implies", "41 tokens")
    with pytest.raises(LedgerError, match="quote"):
        ledger.link(claim, s1, "supports", "  ")
    ledger.link(claim, s1, "supports", "41 tokens")
    with pytest.raises(LedgerError, match="already exists"):
        ledger.link(claim, s1, "supports", "41 tokens")


# -------------------------------------------------------------- verification


def supported_by_two(ledger: Ledger) -> int:
    claim = ledger.add_claim("r1", CLAIM)
    ledger.link(claim, snap(ledger, "https://a.example", PAGE_A), "supports", "41 tokens")
    ledger.link(claim, snap(ledger, "https://b.example", PAGE_B), "supports", "40 to 42")
    return claim


@pytest.mark.safety
def test_verification_needs_independent_sources_a_review_pass_and_a_named_signer(
        ledger) -> None:
    claim = ledger.add_claim("r1", CLAIM)
    with pytest.raises(LedgerError, match="no supporting evidence"):
        ledger.verify(claim, "roshan")

    ledger.link(claim, snap(ledger, "https://a.example", PAGE_A), "supports", "41 tokens")
    # A second snapshot of the SAME source is not independent.
    ledger.link(claim, snap(ledger, "https://a.example", PAGE_A + b" (mirror)"), "supports",
                "41 tokens")
    ledger.run_review_pass("r1", "checker")
    with pytest.raises(LedgerError, match="independent sources, has 1"):
        ledger.verify(claim, "roshan")

    ledger.link(claim, snap(ledger, "https://b.example", PAGE_B), "supports", "40 to 42")
    with pytest.raises(LedgerError, match="review pass"):
        ledger.verify(claim, "roshan")               # evidence changed since the pass
    ledger.run_review_pass("r1", "checker")
    with pytest.raises(LedgerError, match="say who"):
        ledger.verify(claim, "  ")
    ledger.verify(claim, "roshan")
    assert status(ledger, claim) == "verified"


def test_new_evidence_sends_a_verified_claim_back_to_be_checked(ledger) -> None:
    claim = supported_by_two(ledger)
    ledger.run_review_pass("r1", "checker")
    ledger.verify(claim, "roshan")
    ledger.link(claim, snap(ledger, "https://d.example", PAGE_B + b" more"), "supports",
                "40 to 42")
    assert status(ledger, claim) == "supported"
    ledger.link(claim, snap(ledger, "https://c.example", PAGE_C), "contradicts", "only 9 tokens")
    assert status(ledger, claim) == "contradicted"


def test_the_review_pass_does_not_itself_grant_or_lose_verification(ledger) -> None:
    claim = supported_by_two(ledger)
    ledger.run_review_pass("r1", "checker")
    ledger.verify(claim, "roshan")
    ledger.run_review_pass("r1", "checker again")
    assert status(ledger, claim) == "verified"


# ------------------------------------------------------- the reviewable draft


@pytest.mark.safety
def test_a_draft_is_reviewable_only_after_a_current_review_pass(ledger) -> None:
    claim = ledger.add_claim("r1", CLAIM)
    state = ledger.review_state("r1")
    assert not state.reviewable and "no review pass" in state.reasons[0]

    state = ledger.run_review_pass("r1", "checker")
    assert state.reviewable and state.missing_evidence == [claim]

    ledger.link(claim, snap(ledger, "https://a.example", PAGE_A), "supports", "41 tokens")
    stale = ledger.review_state("r1")
    assert not stale.reviewable and "after the last review pass" in stale.reasons[0]

    ledger.add_claim("r1", "a second claim")
    assert not ledger.review_state("r1").reviewable

    state = ledger.run_review_pass("r1", "checker")
    assert state.reviewable and len(state.missing_evidence) == 1


def test_the_pass_lists_claims_with_contradictions_and_missing_evidence(ledger) -> None:
    bare = ledger.add_claim("r1", "nothing backs this")
    bad = ledger.add_claim("r1", CLAIM)
    ledger.link(bad, snap(ledger, "https://c.example", PAGE_C), "contradicts", "only 9 tokens")
    state = ledger.run_review_pass("r1", "checker")
    assert state.missing_evidence == [bare, bad] and state.contradicted == [bad]


# --------------------------------------------- opening the exact source, integrity


def test_a_reviewer_can_open_the_exact_source_behind_any_claim(ledger) -> None:
    claim = ledger.add_claim("r1", CLAIM)
    ledger.link(claim, snap(ledger, "https://a.example/report", PAGE_A), "supports", "41 tokens")
    (ev,) = ledger.evidence(claim)
    assert ev["source_id"] == "https://a.example/report" and ev["quote"] == "41 tokens"
    assert ledger.open_snapshot(ev["snapshot_id"]) == PAGE_A


@pytest.mark.safety
def test_a_corrupted_source_cannot_be_opened_and_costs_the_claim_its_support(
        ledger, tmp_path: Path) -> None:
    import os
    claim = ledger.add_claim("r1", CLAIM)
    s1 = snap(ledger, "https://a.example", PAGE_A)
    ledger.link(claim, s1, "supports", "41 tokens")
    assert status(ledger, claim) == "supported"
    blob = ledger._store.blob_path(ledger._conn.execute(
        "SELECT sha256 FROM evidence_snapshots WHERE id = ?", (s1,)).fetchone()[0])
    os.chmod(blob, 0o600)
    blob.write_bytes(b"tampered")
    with pytest.raises(LedgerError, match="cannot be opened"):
        ledger.open_snapshot(s1)
    state = ledger.run_review_pass("r1", "checker")
    assert status(ledger, claim) == "unverified" and state.missing_evidence == [claim]
    kinds = [r[0] for r in ledger._conn.execute("SELECT kind FROM events")]
    assert "evidence_invalidated" in kinds


def test_every_state_change_is_in_the_audit_log(ledger) -> None:
    claim = supported_by_two(ledger)
    ledger.run_review_pass("r1", "checker")
    ledger.verify(claim, "roshan")
    kinds = [r[0] for r in ledger._conn.execute("SELECT kind FROM events ORDER BY id")]
    for expected in ("research_task_opened", "evidence_snapshot", "claim_added",
                     "claim_status", "review_pass"):
        assert expected in kinds
    from lab.audit import verify_chain
    assert verify_chain(ledger._conn).ok


def test_snapshots_are_capped_and_need_a_source(ledger) -> None:
    with pytest.raises(LedgerError, match="cap"):
        snap(ledger, "https://big", b"x" * (8 * 1024 * 1024 + 1))
    with pytest.raises(LedgerError, match="source id"):
        ledger.add_snapshot("r1", "", "web", b"x")


# --------------------------------------------------------------------- the CLI


def test_cli_show_review_and_verify(tmp_path: Path, capsys) -> None:
    db = tmp_path / "lab.db"
    with TaskQueue(db) as queue:
        led = Ledger(queue._conn, ArtifactStore(tmp_path / "artifacts", queue._conn))
        led.open_research_task("r1", "What is the throughput?", "protocol-v1")
        claim = led.add_claim("r1", CLAIM)
        led.link(claim, led.add_snapshot("r1", "https://a.example", "web", PAGE_A),
                 "supports", "41 tokens")
        led.link(claim, led.add_snapshot("r1", "https://b.example", "web", PAGE_B),
                 "supports", "40 to 42")
    base = ["--db", str(db), "ledger"]
    assert main([*base, "show", "r1"]) == 0
    out = capsys.readouterr().out
    assert "[SUPPORTED   ]" in out and "reviewable: NO" in out and "https://a.example" in out
    assert main([*base, "verify", "1", "--by", "roshan"]) == 1
    assert "review pass" in capsys.readouterr().err
    assert main([*base, "review", "r1", "--by", "checker"]) == 0
    assert "reviewable: yes" in capsys.readouterr().out
    assert main([*base, "verify", "1", "--by", "roshan"]) == 0
    assert main([*base, "show", "r1"]) == 0
    assert "[VERIFIED    ]" in capsys.readouterr().out
    assert main([*base, "show", "nope"]) == 1
