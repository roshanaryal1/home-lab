"""Emit proposals from the internal event log (issue #32).

The observation plane of ADR 0004. ``lab.observe`` reads GitHub; this
reads the append-only ``events`` table, which nothing else consumed.
A rule looks for a pattern across events and, when it holds, queues an
ordinary proposal task. There is no proposals table and no special
path: the task is ``notify`` tier, its origin is ``event`` (so it is
tainted and can carry no grant, destination or policy), and it passes
the same gate as anything else. Autonomy here is generating work, not
bypassing controls.

The evidence for a proposal is the event ids that produced it. They are
stored in the payload and in a ``proposal_emitted`` event on the hash
chain, and ``chain_for`` renders them, so a person can see why the work
exists.

Three rules: ``repeated_failure`` (several tasks of one kind failing for
what normalizes to the same reason), ``similar_closed_issues`` (three or
more closed issues whose titles share most of their words, a cheap
stand-in for a shared root cause that errs toward grouping), and
``unpublished_measurement`` (an eval run recorded as a ``measurement``
event that no artifact cites in its lineage after a grace period). A
merged fix that carried a reproduction is not built. Each rule is a
function returning ``Finding`` objects.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass

from lab.observe import Signal
from lab.origin import Origin, SourceType, content_sha256
from lab.queue import TaskQueue

PROPOSAL_KIND = "proposal"
MAX_CHAIN_IDS = 50
SIGNATURE_CHARS = 120
SIMILARITY_THRESHOLD = 0.3
MIN_SHARED_WORDS = 2
MIN_CLUSTER = 3
MAX_TITLES = 10
TITLE_CHARS = 120
MEASUREMENT_GRACE_SECONDS = 24 * 3600.0
_STOP = frozenset({
    "with", "that", "this", "from", "when", "then", "than", "have", "into", "over",
    "under", "also", "are", "was", "were", "the", "and", "for", "but", "its", "their",
    "there", "which", "what", "will", "would", "should", "could", "fix", "fixes", "fixed",
    "issue", "bug", "error",
})


@dataclass(frozen=True)
class Finding:
    rule: str
    key: str                  # stable identity, so a finding is proposed once
    title: str
    summary: dict[str, object]
    event_ids: tuple[int, ...] = ()
    source_urls: tuple[str, ...] = ()     # for findings built from GitHub signals


@dataclass(frozen=True)
class ChainLink:
    event_id: int
    task_id: str | None
    kind: str
    to_state: str | None
    detail: str | None
    hash: str | None


def signature(error: str) -> str:
    """The reason a task failed with the variable parts removed.

    Numbers, hex ids and addresses differ between runs of one fault; the
    words do not.
    """
    text = re.sub(r"0x[0-9a-f]+|[0-9a-f]{8,}|\d+", "#", error.lower())
    return re.sub(r"\s+", " ", text).strip()[:SIGNATURE_CHARS]


def repeated_failure(queue: TaskQueue, min_failures: int) -> list[Finding]:
    rows = queue._conn.execute(
        "SELECT e.id, e.task_id, e.detail, t.agent_kind FROM events e "
        "JOIN tasks t ON t.id = e.task_id "
        "WHERE e.kind = 'failed' AND e.to_state = 'failed' "
        "AND COALESCE(t.agent_kind, '') <> ? ORDER BY e.id",
        (PROPOSAL_KIND,),
    ).fetchall()
    groups: dict[tuple[str, str], list[int]] = {}
    for row in rows:
        detail = json.loads(row["detail"]) if row["detail"] else {}
        sig = signature(str(detail.get("error") or ""))
        groups.setdefault((row["agent_kind"] or "", sig), []).append(row["id"])
    findings: list[Finding] = []
    for (kind, sig), ids in groups.items():
        if len(ids) < min_failures:
            continue
        key = hashlib.sha256(f"repeated_failure|{kind}|{sig}".encode()).hexdigest()
        findings.append(Finding(
            "repeated_failure", key,
            f"proposal: {len(ids)} {kind or 'untyped'} tasks failed alike",
            {"agent_kind": kind, "signature": sig, "count": len(ids)},
            tuple(ids[-MAX_CHAIN_IDS:]),
        ))
    return findings


def _tokens(title: str) -> frozenset[str]:
    words = re.findall(r"[a-z0-9]+", title.lower())
    return frozenset(w for w in words if len(w) >= 4 and w not in _STOP)


def similar_closed_issues(signals: list[Signal]) -> list[Finding]:
    """Closed issues whose titles share most of their significant words.

    A stand-in for "a shared root cause" that needs no model: word overlap
    (Jaccard >= 0.3 and at least two shared words), single linkage, at least three issues.
    It will over-group, which is cheap: the output is a proposal a person
    reads, never an action. Pull requests are not counted.
    """
    issues = [s for s in signals if s.source_type == "issue"]
    tokens = [_tokens(s.title) for s in issues]
    parent = list(range(len(issues)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(issues)):
        for j in range(i + 1, len(issues)):
            union = tokens[i] | tokens[j]
            shared = len(tokens[i] & tokens[j])
            if union and shared >= MIN_SHARED_WORDS \
                    and shared / len(union) >= SIMILARITY_THRESHOLD:
                parent[find(i)] = find(j)
    clusters: dict[int, list[int]] = {}
    for i in range(len(issues)):
        clusters.setdefault(find(i), []).append(i)
    findings: list[Finding] = []
    for members in clusters.values():
        if len(members) < MIN_CLUSTER:
            continue
        urls = tuple(sorted(issues[i].source_url for i in members))[:MAX_CHAIN_IDS]
        key = hashlib.sha256(("similar_closed_issues|" + "|".join(urls)).encode()).hexdigest()
        titles = [issues[i].title[:TITLE_CHARS] for i in members][:MAX_TITLES]
        findings.append(Finding(
            "similar_closed_issues", key,
            f"proposal: {len(members)} closed issues may share a root cause",
            {"count": len(members), "titles": titles}, source_urls=urls))
    return findings


def unpublished_measurement(queue: TaskQueue, min_age_seconds: float) -> list[Finding]:
    """A ``measurement`` event whose record no artifact cites in its lineage."""
    rows = queue._conn.execute(
        "SELECT id, detail FROM events WHERE kind = 'measurement' "
        "AND created_at <= strftime('%Y-%m-%d %H:%M:%f', 'now', ?) ORDER BY id",
        (f"-{int(min_age_seconds)} seconds",)).fetchall()
    findings: list[Finding] = []
    for row in rows:
        detail = json.loads(row["detail"] or "{}")
        sha = detail.get("record_sha256")
        if not isinstance(sha, str) or len(sha) != 64:
            continue
        cited = queue._conn.execute(
            "SELECT 1 FROM artifacts a, json_each(a.lineage) j WHERE j.value = ? LIMIT 1",
            (sha,)).fetchone()
        if cited:
            continue
        key = hashlib.sha256(f"unpublished_measurement|{sha}".encode()).hexdigest()
        findings.append(Finding(
            "unpublished_measurement", key,
            f"proposal: measurement {sha[:12]} has no artifact",
            {"record_sha256": sha, "name": str(detail.get("name", ""))[:TITLE_CHARS]},
            (row["id"],)))
    return findings


def _known_keys(queue: TaskQueue) -> set[str]:
    rows = queue._conn.execute(
        "SELECT payload FROM tasks WHERE agent_kind = ?", (PROPOSAL_KIND,)
    ).fetchall()
    return {json.loads(r["payload"]).get("emitter_key") for r in rows}


def emit_proposals(queue: TaskQueue, *, min_failures: int = 3,
                   signals: list[Signal] | None = None,
                   measurement_min_age_seconds: float = MEASUREMENT_GRACE_SECONDS) -> list[str]:
    """Run every rule, queue a proposal per new finding, return the task ids."""
    rules: list[Callable[[], list[Finding]]] = [
        lambda: repeated_failure(queue, min_failures),
        lambda: similar_closed_issues(signals or []),
        lambda: unpublished_measurement(queue, measurement_min_age_seconds),
    ]
    known = _known_keys(queue)
    created: list[str] = []
    for rule in rules:
        for finding in rule():
            if finding.key in known:
                continue
            task_id = queue.add_task(
                finding.title,
                payload={**finding.summary, "rule": finding.rule,
                         "emitter_key": finding.key,
                         "source_event_ids": list(finding.event_ids),
                         **({"source_urls": list(finding.source_urls)}
                            if finding.source_urls else {})},
                agent_kind=PROPOSAL_KIND,
                capability_tier="notify",
                idempotent=True,
                origin=Origin(
                    SourceType.EVENT,
                    (f"events:{finding.event_ids[0]}-{finding.event_ids[-1]}"
                     if finding.event_ids else finding.source_urls[0]),
                    content_sha256(",".join(map(str, finding.event_ids))
                                   + "|" + ",".join(finding.source_urls))),
            )
            queue.record_event(task_id, "proposal_emitted", {
                "rule": finding.rule, "event_ids": list(finding.event_ids),
                "source_urls": list(finding.source_urls)})
            known.add(finding.key)
            created.append(task_id)
    return created


def chain_for(queue: TaskQueue, task_id: str) -> list[ChainLink]:
    """The events that produced a proposal, in log order, with their hashes."""
    emitted = queue._conn.execute(
        "SELECT detail FROM events WHERE task_id = ? AND kind = 'proposal_emitted' "
        "ORDER BY id LIMIT 1", (task_id,)).fetchone()
    if emitted is None:
        return []
    ids = json.loads(emitted["detail"])["event_ids"]
    marks = ",".join("?" * len(ids))
    rows = queue._conn.execute(
        f"SELECT id, task_id, kind, to_state, detail, hash FROM events "
        f"WHERE id IN ({marks}) ORDER BY id", ids).fetchall()
    return [ChainLink(r["id"], r["task_id"], r["kind"], r["to_state"],
                      r["detail"], r["hash"]) for r in rows]


def sources_for(queue: TaskQueue, task_id: str) -> list[str]:
    """The GitHub issue URLs behind a proposal built from signals."""
    emitted = queue._conn.execute(
        "SELECT detail FROM events WHERE task_id = ? AND kind = 'proposal_emitted' "
        "ORDER BY id LIMIT 1", (task_id,)).fetchone()
    if emitted is None:
        return []
    return [str(u) for u in json.loads(emitted["detail"]).get("source_urls", [])]
