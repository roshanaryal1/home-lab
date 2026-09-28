"""Artifact router, v0: route a proposal by evidence weight, then draft it.

Issue #39. Deliberately simple and explicitly not #33's general service:
the rule below is the evidence-weight table from
`docs/PIPELINE.md` (one incident -> post, a pattern with a mechanism ->
blog, a measurement that survives a control -> paper), applied to
exactly one signal at a time. It cannot yet detect a pattern across
several related signals, because #39's job is to prove the path is
real, not to build the real router. That generalization is #33.

No model call anywhere in this file. The draft step is a template fill,
not writing: proving the pipeline moves a real signal to a real draft
without a human doing it by hand, not proving draft quality. Once a
heavy model is running on the mini, replacing `_draft_post` with a real
call is a drop-in change, not a redesign.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Route = Literal["post", "blog", "paper"]


@dataclass(frozen=True)
class RouteDecision:
    route: Route
    evidence: str  # human-readable justification, always present


def route_by_evidence_weight(payload: dict) -> RouteDecision:
    """Decide post vs blog vs paper for one signal.

    v0 rule, single-signal only:
    - a merged PR with a body over 400 characters (long enough to
      contain a "why", not just a one-line change) routes to blog;
    - anything else routes to post.
    Paper is never reachable from a single signal: PIPELINE.md's gate
    requires a measurement that survives a control, which by definition
    is not present in one GitHub event. A future multi-signal router
    (#33) is what can reach paper.
    """
    source_type = payload.get("source_type", "issue")
    body = payload.get("source_body", "") or ""

    if source_type == "pull_request" and len(body) > 400:
        return RouteDecision(
            route="blog",
            evidence=(
                f"merged PR with a {len(body)}-character body, long "
                "enough to carry a mechanism, not just a change log"
            ),
        )

    return RouteDecision(
        route="post",
        evidence=(
            f"single {source_type}, no pattern across related signals "
            "detected (v0 router checks one signal at a time)"
        ),
    )


def draft(payload: dict, decision: RouteDecision) -> str:
    """Template-fill a draft. Not real writing; proves the path only.

    Real drafting is a model call once one is available. This exists so
    the slice has something concrete to stop at before the publish gate,
    not to produce anything publishable as-is.
    """
    title = payload.get("source_title", "untitled")
    url = payload.get("source_url", "")
    number = payload.get("source_number", "?")

    if decision.route == "post":
        return (
            f"[DRAFT, template, not model-written]\n\n"
            f"{title}\n\n"
            f"Source: {url} (#{number})\n\n"
            f"One-line note: [fill in what happened and why it matters]\n\n"
            f"Route: post. {decision.evidence}"
        )

    return (
        f"[DRAFT, template, not model-written]\n\n"
        f"## {title}\n\n"
        f"Source: {url} (#{number})\n\n"
        f"[fill in: the pattern, the mechanism, what changed]\n\n"
        f"Route: {decision.route}. {decision.evidence}"
    )
