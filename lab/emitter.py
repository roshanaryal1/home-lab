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

One rule is built: ``repeated_failure``, several tasks of one kind
failing for what is normalized to the same reason. The others named in
the issue (a merged fix with a reproduction, closed issues sharing a
root cause) need GitHub data and belong to ``lab.observe``; a
measurement with no artifact needs a measurement event that nothing
emits yet. Each new rule is a function returning ``Finding`` objects.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass

from lab.origin import Origin, SourceType, content_sha256
from lab.queue import TaskQueue

PROPOSAL_KIND = "proposal"
MAX_CHAIN_IDS = 50
SIGNATURE_CHARS = 120


@dataclass(frozen=True)
class Finding:
    rule: str
    key: str                  # stable identity, so a finding is proposed once
    title: str
    summary: dict[str, object]
    event_ids: tuple[int, ...]


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


def _known_keys(queue: TaskQueue) -> set[str]:
    rows = queue._conn.execute(
        "SELECT payload FROM tasks WHERE agent_kind = ?", (PROPOSAL_KIND,)
    ).fetchall()
    return {json.loads(r["payload"]).get("emitter_key") for r in rows}


def emit_proposals(queue: TaskQueue, *, min_failures: int = 3) -> list[str]:
    """Run every rule, queue a proposal per new finding, return the task ids."""
    rules: list[Callable[[], list[Finding]]] = [
        lambda: repeated_failure(queue, min_failures),
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
                         "source_event_ids": list(finding.event_ids)},
                agent_kind=PROPOSAL_KIND,
                capability_tier="notify",
                idempotent=True,
                origin=Origin(SourceType.EVENT,
                              f"events:{finding.event_ids[0]}-{finding.event_ids[-1]}",
                              content_sha256(",".join(map(str, finding.event_ids)))),
            )
            queue.record_event(task_id, "proposal_emitted", {
                "rule": finding.rule, "event_ids": list(finding.event_ids)})
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
