"""The artifact router's rubric (item 7.2, closes #33).

Routes a research task to a post, a blog, a paper, or to nothing, by the
weight of its evidence and by nothing else. Rules, not judgement, so the
rule can be read, corrected and tested:

===============  =============================================================
route            what the ledger must show
===============  =============================================================
post             one incident, commit or lesson: at least one supported claim
blog             a pattern with a mechanism: usable claims backed by at least
                 3 distinct incident sources, and at least one usable claim of
                 kind "mechanism"
paper            a measurement that survives a control: a *verified* claim of
                 kind "measurement" whose supporting evidence includes a
                 measurement, a baseline and a control (three different
                 sources, of those three types)
insufficient     there are claims but not enough behind them for even a post,
evidence         or the ledger has not been through a current review pass
no artifact      there is nothing to say: no claims at all
===============  =============================================================

A "usable" claim is supported or verified and has no contradicting evidence.
Contradicted claims are never averaged away: they are listed in the decision
as conflicts, and the route is computed without them.

Thin evidence is never routed upward. A caller may *ask* for a route; if the
evidence allows less, the answer is the lower route with the reason, and
the request is recorded as refused. Nothing here calls a model, and a
model's opinion cannot raise a route.

Every decision carries its evidence chain (claim, status, sources) so a
person can inspect exactly what justified it, and every decision asks for
human review: the rule is not yet trusted to act alone.

Research also needs a stop rule that is not the model saying it is done:
``stop_decision`` completes only when every material claim has support and
none is contradicted, and gives up, with "insufficient evidence", when the
search or time budget runs out first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from lab.ledger import ClaimView, Ledger

Route = Literal["paper", "blog", "post", "insufficient_evidence", "no_artifact"]
ORDER = {"no_artifact": 0, "insufficient_evidence": 0, "post": 1, "blog": 2, "paper": 3}

BLOG_MIN_INCIDENT_SOURCES = 3
PAPER_REQUIRED_TYPES = ("measurement", "baseline", "control")


@dataclass(frozen=True)
class ChainLink:
    claim_id: int
    kind: str
    status: str
    sources: tuple[tuple[str, str], ...]     # (source_id, source_type)


@dataclass(frozen=True)
class Decision:
    route: Route
    requested: str | None
    refused_upward: bool
    reasons: list[str]
    chain: list[ChainLink]
    conflicts: list[int] = field(default_factory=list)
    needs_human_review: bool = True


def _usable(claim: ClaimView) -> bool:
    return claim.status in ("supported", "verified") and claim.contradicts == 0


def route_research_task(ledger: Ledger, task_id: str, requested: str | None = None) -> Decision:
    ledger.research_task(task_id)          # an unknown task is an error, not 'nothing to say'
    claims = ledger.claims(task_id)
    chain = [ChainLink(c.id, c.kind, c.status, tuple(sorted(ledger.support_sources(c.id))))
             for c in claims]
    conflicts = [c.id for c in claims if c.contradicts > 0]
    reasons: list[str] = []

    def done(route: Route) -> Decision:
        refused = (requested is not None and requested in ORDER
                   and ORDER[requested] > ORDER[route])
        if refused:
            reasons.append(f"requested {requested!r} refused: the evidence supports at most "
                           f"{route!r}")
        if conflicts:
            reasons.append(f"conflicting evidence on claims {conflicts} is stated, not "
                           "averaged away; those claims are excluded from the route")
        return Decision(route, requested, refused, reasons, chain, conflicts)

    if not claims:
        reasons.append("the research task has no claims: nothing to say")
        return done("no_artifact")
    review = ledger.review_state(task_id)
    if not review.reviewable:
        reasons.append("no current review pass (" + "; ".join(review.reasons)
                       + "): a route needs the contradiction check and the missing-evidence list")
        return done("insufficient_evidence")

    usable = [c for c in claims if _usable(c)]
    if not usable:
        reasons.append("no claim is supported and free of contradiction; "
                       f"{len(review.missing_evidence)} lack any supporting evidence")
        return done("insufficient_evidence")

    incident_sources = {sid for c in usable for sid, stype in ledger.support_sources(c.id)
                        if stype == "incident"}
    has_mechanism = any(c.kind == "mechanism" for c in usable)
    paper_claim = next((c.id for c in usable if c.kind == "measurement"
                        and c.status == "verified"
                        and {t for _, t in ledger.support_sources(c.id)}
                        >= set(PAPER_REQUIRED_TYPES)), None)

    if paper_claim is not None:
        reasons.append(f"verified measurement claim {paper_claim} has a measurement, a "
                       "baseline and a control behind it")
        return done("paper")
    if len(incident_sources) >= BLOG_MIN_INCIDENT_SOURCES and has_mechanism:
        reasons.append(f"a pattern across {len(incident_sources)} incident sources with a "
                       "mechanism claim")
        return done("blog")
    why_not_blog = []
    if len(incident_sources) < BLOG_MIN_INCIDENT_SOURCES:
        why_not_blog.append(f"only {len(incident_sources)} incident source(s), blog needs "
                            f"{BLOG_MIN_INCIDENT_SOURCES}")
    if not has_mechanism:
        why_not_blog.append("no usable mechanism claim")
    reasons.append("one supported claim is enough for a post; not for a blog: "
                   + "; ".join(why_not_blog))
    return done("post")


def draft_from_decision(ledger: Ledger, task_id: str, decision: Decision) -> str:
    """A template draft over the usable claims and their sources. Not writing."""
    if decision.route in ("no_artifact", "insufficient_evidence"):
        return f"[NO DRAFT] {decision.route}: " + " ".join(decision.reasons)
    task = ledger.research_task(task_id)
    lines = [f"[DRAFT, template, not model-written] {decision.route.upper()}",
             f"Question: {task['question']}", ""]
    for claim in ledger.claims(task_id):
        if claim.id in decision.conflicts or claim.status not in ("supported", "verified"):
            continue
        lines.append(f"- ({claim.status}) {claim.text}")
        lines += [f"    source: {sid} [{stype}]"
                  for sid, stype in sorted(ledger.support_sources(claim.id))]
    if decision.conflicts:
        lines += ["", f"Unresolved conflicts, stated: claims {decision.conflicts}"]
    lines += ["", "Requires human review before anything is published."]
    return "\n".join(lines)


@dataclass(frozen=True)
class ResearchBudget:
    max_searches: int = 20
    max_seconds: float = 900.0


StopState = Literal["continue", "complete", "insufficient_evidence"]


DEFAULT_BUDGET = ResearchBudget()


def stop_decision(claims: list[ClaimView], searches_used: int, seconds_used: float,
                  budget: ResearchBudget = DEFAULT_BUDGET) -> tuple[StopState, str]:
    """Bounded, observable completion: not the model's say-so."""
    conflicts = [c.id for c in claims if c.contradicts > 0]
    unsupported = [c.id for c in claims if c.supports == 0]
    if claims and not unsupported and not conflicts:
        return "complete", "every material claim has support and none is contradicted"
    if searches_used >= budget.max_searches or seconds_used >= budget.max_seconds:
        why = []
        if unsupported:
            why.append(f"claims without support: {unsupported}")
        if conflicts:
            why.append(f"unresolved conflicts: {conflicts}")
        if not claims:
            why.append("no claims yet")
        return "insufficient_evidence", "budget exhausted with " + "; ".join(why)
    return "continue", "within budget, evidence still incomplete"
