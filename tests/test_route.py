"""Tests for the v0 router and draft template.

No model call anywhere in lab.route, so these are pure function tests:
given a payload, is the routing decision and the draft's content
correct. Nothing here asserts draft *quality*, only that the pipeline
produces something and stops before anything that looks like
publishing.
"""

from __future__ import annotations

from lab.route import draft, route_by_evidence_weight


def test_single_issue_routes_to_post() -> None:
    decision = route_by_evidence_weight({"source_type": "issue"})
    assert decision.route == "post"


def test_short_pr_body_routes_to_post() -> None:
    decision = route_by_evidence_weight({
        "source_type": "pull_request",
        "source_body": "fix typo",
    })
    assert decision.route == "post"


def test_long_pr_body_routes_to_blog() -> None:
    decision = route_by_evidence_weight({
        "source_type": "pull_request",
        "source_body": "x" * 500,
    })
    assert decision.route == "blog"


def test_single_signal_never_routes_to_paper() -> None:
    for source_type in ("issue", "pull_request"):
        for body_len in (0, 100, 500, 5000):
            decision = route_by_evidence_weight({
                "source_type": source_type,
                "source_body": "x" * body_len,
            })
            assert decision.route != "paper", (
                "v0 router must never reach paper from one signal; "
                "that gate requires a measurement, which #33's general "
                "router is what can actually check"
            )


def test_decision_always_carries_evidence() -> None:
    decision = route_by_evidence_weight({"source_type": "issue"})
    assert decision.evidence
    assert isinstance(decision.evidence, str)


def test_draft_marks_itself_as_a_template() -> None:
    decision = route_by_evidence_weight({"source_type": "issue"})
    text = draft(
        {"source_title": "fixed the thing", "source_url": "https://x",
         "source_number": 7},
        decision,
    )
    assert "template" in text.lower()
    assert "fixed the thing" in text


def test_draft_never_contains_a_publish_action() -> None:
    """The draft is text, not an instruction. It must never itself carry
    the vocabulary of having been sent, posted, or published."""
    decision = route_by_evidence_weight({"source_type": "issue"})
    text = draft(
        {"source_title": "t", "source_url": "u", "source_number": 1},
        decision,
    )
    for forbidden in ("posted successfully", "sent to", "published to"):
        assert forbidden not in text.lower()
