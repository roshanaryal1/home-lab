"""The router's rubric: evidence weight, thin evidence refused upward (item 7.2, #33)."""

from __future__ import annotations

from pathlib import Path

import pytest

from lab.artifacts import ArtifactStore
from lab.cli import main
from lab.ledger import Ledger
from lab.queue import TaskQueue
from lab.rubric import (
    ResearchBudget,
    draft_from_decision,
    route_research_task,
    stop_decision,
)


@pytest.fixture()
def q(tmp_path: Path) -> TaskQueue:
    with TaskQueue(tmp_path / "lab.db") as queue:
        yield queue


@pytest.fixture()
def ledger(q: TaskQueue, tmp_path: Path) -> Ledger:
    ledger = Ledger(q._conn, ArtifactStore(tmp_path / "artifacts", q._conn))
    ledger.open_research_task("r1", "Why do leases get lost?", "protocol-v1")
    return ledger


def evidence(ledger: Ledger, claim: int, source: str, stype: str, text: str,
             relation: str = "supports") -> None:
    snap = ledger.add_snapshot("r1", source, stype, text.encode())
    ledger.link(claim, snap, relation, text.split(".")[0])


def reviewed(ledger: Ledger) -> None:
    ledger.run_review_pass("r1", "checker")


def one_incident(ledger: Ledger) -> int:
    claim = ledger.add_claim("r1", "The lease was lost after a restart")
    evidence(ledger, claim, "issue-47", "incident", "Lease fencing failed after restart.")
    return claim


# --------------------------------------------------------------- nothing, thin


def test_no_claims_means_no_artifact(ledger) -> None:
    reviewed(ledger)
    decision = route_research_task(ledger, "r1")
    assert decision.route == "no_artifact" and decision.needs_human_review
    assert "no claims" in decision.reasons[0]


def test_claims_without_evidence_are_insufficient_evidence_not_a_post(ledger) -> None:
    ledger.add_claim("r1", "Something happened")
    reviewed(ledger)
    decision = route_research_task(ledger, "r1")
    assert decision.route == "insufficient_evidence"
    assert "lack any supporting evidence" in decision.reasons[0]


@pytest.mark.safety
def test_an_unreviewed_ledger_cannot_be_routed(ledger) -> None:
    one_incident(ledger)
    decision = route_research_task(ledger, "r1")               # no review pass yet
    assert decision.route == "insufficient_evidence" and "review pass" in decision.reasons[0]
    reviewed(ledger)
    assert route_research_task(ledger, "r1").route == "post"
    evidence(ledger, 1, "issue-99", "incident", "A second lease loss occurred.")
    assert route_research_task(ledger, "r1").route == "insufficient_evidence", \
        "evidence changed after the review pass"


# ------------------------------------------------------------ post, blog, paper


def test_one_incident_routes_to_a_post(ledger) -> None:
    one_incident(ledger)
    reviewed(ledger)
    decision = route_research_task(ledger, "r1")
    assert decision.route == "post" and not decision.refused_upward
    assert "not for a blog" in decision.reasons[0]


def blog_evidence(ledger: Ledger) -> None:
    claim = one_incident(ledger)
    evidence(ledger, claim, "issue-52", "incident", "Second loss during a deploy.")
    evidence(ledger, claim, "issue-54", "incident", "Third loss under load.")
    mech = ledger.add_claim("r1", "Fencing keyed on the owner name allowed stale writers",
                            kind="mechanism")
    evidence(ledger, mech, "pr-98", "incident", "Fencing now keyed on a unique claim token.")


def test_a_pattern_across_incidents_with_a_mechanism_routes_to_a_blog(ledger) -> None:
    blog_evidence(ledger)
    reviewed(ledger)
    decision = route_research_task(ledger, "r1")
    assert decision.route == "blog" and "4 incident sources" in decision.reasons[0]


def test_a_pattern_without_a_mechanism_or_with_too_few_incidents_stays_a_post(ledger) -> None:
    claim = one_incident(ledger)
    evidence(ledger, claim, "issue-52", "incident", "Second loss during a deploy.")
    evidence(ledger, claim, "issue-54", "incident", "Third loss under load.")
    reviewed(ledger)
    decision = route_research_task(ledger, "r1")
    assert decision.route == "post" and "no usable mechanism claim" in decision.reasons[0]

    ledger2_claim = ledger.add_claim("r1", "Mechanism", kind="mechanism")
    evidence(ledger, ledger2_claim, "issue-52", "incident", "Same source again as before.")
    reviewed(ledger)
    assert route_research_task(ledger, "r1").route == "blog", "3 sources plus a mechanism"


def measured(ledger: Ledger, *, types=("measurement", "baseline", "control")) -> int:
    claim = ledger.add_claim("r1", "Typed routing lowers false promotion by 30 percent",
                             kind="measurement")
    for stype in types:
        evidence(ledger, claim, f"run-{stype}", stype, f"The {stype} run recorded a result.")
    return claim


def test_a_verified_measurement_with_baseline_and_control_routes_to_a_paper(ledger) -> None:
    claim = measured(ledger)
    reviewed(ledger)
    # Supported alone is not enough for a paper.
    assert route_research_task(ledger, "r1").route == "post"
    ledger.verify(claim, "roshan")
    decision = route_research_task(ledger, "r1")
    assert decision.route == "paper" and "measurement, a baseline and a control" \
        in decision.reasons[0]


@pytest.mark.safety
@pytest.mark.parametrize("missing", ["measurement", "baseline", "control"])
def test_a_paper_without_all_three_kinds_of_evidence_is_not_a_paper(ledger, missing) -> None:
    types = tuple(t for t in ("measurement", "baseline", "control") if t != missing)
    claim = measured(ledger, types=types)
    evidence(ledger, claim, "extra-incident", "incident", "An incident also supports it.")
    reviewed(ledger)
    ledger.verify(claim, "roshan")
    assert route_research_task(ledger, "r1").route == "post"


# ---------------------------------------------------- the deliberately thin proposal


@pytest.mark.safety
def test_a_deliberately_thin_proposal_is_refused_an_upward_route(ledger) -> None:
    """The acceptance case for #33: ask for a paper on one incident."""
    one_incident(ledger)
    reviewed(ledger)
    decision = route_research_task(ledger, "r1", requested="paper")
    assert decision.route == "post" and decision.refused_upward
    assert "requested 'paper' refused" in decision.reasons[-1]
    blog = route_research_task(ledger, "r1", requested="blog")
    assert blog.route == "post" and blog.refused_upward
    fine = route_research_task(ledger, "r1", requested="post")
    assert fine.route == "post" and not fine.refused_upward


def test_asking_for_less_than_the_evidence_allows_is_not_a_refusal(ledger) -> None:
    blog_evidence(ledger)
    reviewed(ledger)
    decision = route_research_task(ledger, "r1", requested="post")
    assert decision.route == "blog" and not decision.refused_upward


# -------------------------------------------------- conflicts, chain, the draft


def test_conflicts_are_stated_and_excluded_never_averaged(ledger) -> None:
    good = one_incident(ledger)
    bad = ledger.add_claim("r1", "The lease was never lost")
    evidence(ledger, bad, "issue-60", "incident", "Reporter says nothing was lost.")
    evidence(ledger, bad, "issue-61", "incident", "Logs show the lease was lost.",
             relation="contradicts")
    reviewed(ledger)
    decision = route_research_task(ledger, "r1")
    assert decision.conflicts == [bad] and decision.route == "post"
    assert "stated, not averaged away" in decision.reasons[-1]
    text = draft_from_decision(ledger, "r1", decision)
    assert f"claims [{bad}]" in text and "never lost" not in text.split("Unresolved")[0]
    assert "The lease was lost after a restart" in text and good


def test_the_decision_carries_an_inspectable_evidence_chain(ledger) -> None:
    blog_evidence(ledger)
    reviewed(ledger)
    decision = route_research_task(ledger, "r1")
    assert [link.claim_id for link in decision.chain] == [1, 2]
    first = decision.chain[0]
    assert first.status == "supported" and first.kind == "finding"
    assert ("issue-47", "incident") in first.sources and len(first.sources) == 3


def test_the_draft_lists_sources_and_demands_human_review(ledger) -> None:
    one_incident(ledger)
    reviewed(ledger)
    text = draft_from_decision(ledger, "r1", route_research_task(ledger, "r1"))
    assert "template, not model-written" in text and "source: issue-47 [incident]" in text
    assert "Requires human review" in text


def test_nothing_to_route_yields_no_draft(ledger) -> None:
    reviewed(ledger)
    text = draft_from_decision(ledger, "r1", route_research_task(ledger, "r1"))
    assert text.startswith("[NO DRAFT] no_artifact")


# ----------------------------------------------------------------- stop rules


def claims_of(ledger: Ledger):
    return ledger.claims("r1")


def test_research_completes_only_when_every_claim_is_supported_and_uncontradicted(ledger) -> None:
    budget = ResearchBudget(max_searches=5, max_seconds=60)
    assert stop_decision([], 0, 0, budget)[0] == "continue"
    claim = ledger.add_claim("r1", "A claim")
    state, why = stop_decision(claims_of(ledger), 1, 1, budget)
    assert state == "continue" and "incomplete" in why
    evidence(ledger, claim, "s1", "incident", "Supporting text here.")
    assert stop_decision(claims_of(ledger), 2, 2, budget)[0] == "complete"
    evidence(ledger, claim, "s2", "incident", "Contradicting text here.", relation="contradicts")
    assert stop_decision(claims_of(ledger), 3, 3, budget)[0] == "continue"


@pytest.mark.safety
def test_running_out_of_budget_is_insufficient_evidence_not_done(ledger) -> None:
    ledger.add_claim("r1", "Unsupported")
    budget = ResearchBudget(max_searches=3, max_seconds=60)
    state, why = stop_decision(claims_of(ledger), 3, 5, budget)
    assert state == "insufficient_evidence" and "claims without support" in why
    state, why = stop_decision(claims_of(ledger), 1, 61, budget)
    assert state == "insufficient_evidence"
    assert stop_decision([], 3, 1, budget)[0] == "insufficient_evidence"


def test_budget_exhaustion_does_not_hide_a_finished_result(ledger) -> None:
    claim = ledger.add_claim("r1", "A claim")
    evidence(ledger, claim, "s1", "incident", "Supporting text here.")
    assert stop_decision(claims_of(ledger), 99, 9999, ResearchBudget(1, 1))[0] == "complete"


# ------------------------------------------------------------------------ CLI


def test_cli_route_prints_route_reasons_chain_and_draft(tmp_path: Path, capsys) -> None:
    db = tmp_path / "lab.db"
    with TaskQueue(db) as queue:
        led = Ledger(queue._conn, ArtifactStore(tmp_path / "artifacts", queue._conn))
        led.open_research_task("r1", "Why?", "p1")
        claim = led.add_claim("r1", "The lease was lost")
        led.link(claim, led.add_snapshot("r1", "issue-47", "incident", b"Lease lost here."),
                 "supports", "Lease lost")
        led.run_review_pass("r1", "checker")
    assert main(["--db", str(db), "route", "r1", "--want", "paper"]) == 0
    out = capsys.readouterr().out
    assert "route: POST   (asked for paper: REFUSED)" in out
    assert "evidence chain:" in out and "issue-47 [incident]" in out and "DRAFT" in out
    assert main(["--db", str(db), "route", "ghost"]) == 1
