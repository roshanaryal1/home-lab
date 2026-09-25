"""Authorization: the gate between leasing a task and executing it.

Closes issue #9. Section 10 of the reference architecture defines four
capability tiers; until now they existed in the schema and nothing read
them, which made them documentation rather than control.

The rule this module enforces is the architecture's central one: the
model is not the system administrator. A model may propose an action.
Whether it runs is decided here, in code the model cannot talk its way
past, because this code never sees model output as instruction, only as
parameters to hash.

Design notes worth keeping:

* An approval authorises **one action**, not a capability. It is bound to
  the exact normalised parameters a human was shown. Approving "email
  alice about the invoice" cannot be replayed to email bob.
* Approvals are **single use**. Consuming one is an atomic UPDATE with the
  unconsumed state in the WHERE clause, so two concurrent workers cannot
  both spend the same token.
* Everything fails **closed**. An unknown tier, a missing approval, an
  expired one, a mismatched hash: all deny.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from lab.queue import NOW_MS, Task, _ts, _utcnow


class Tier(StrEnum):
    """What authority an action needs. Ordered least to most dangerous."""

    AUTONOMOUS = "autonomous"
    NOTIFY = "notify"
    APPROVE = "approve"
    NEVER = "never"


class Decision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    NEEDS_APPROVAL = "needs_approval"


@dataclass(frozen=True)
class PolicyResult:
    decision: Decision
    tier: Tier
    reason: str
    approval_id: str | None = None

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW


def action_hash(task: Task) -> str:
    """Stable fingerprint of exactly what is about to happen.

    Sorted keys so that two logically identical payloads hash the same,
    and a changed parameter always hashes differently. This is what binds
    an approval to one action rather than to a capability.
    """
    material = json.dumps(
        {"task": task.id, "kind": task.agent_kind, "payload": task.payload},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class PolicyEngine:
    """Decides whether a leased task may execute."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # --------------------------------------------------------- decisions

    def authorize(self, task: Task) -> PolicyResult:
        """The gate. Called between lease and execute, never skipped."""
        try:
            tier = Tier(task.capability_tier)
        except ValueError:
            # Unknown tier is a bug or tampering. Fail closed either way.
            return self._record(
                task, Tier.NEVER, Decision.DENY,
                f"unknown capability tier {task.capability_tier!r}",
            )

        if tier is Tier.NEVER:
            return self._record(
                task, tier, Decision.DENY,
                "never tier is refused unconditionally, approvals do not apply",
            )

        if tier in (Tier.AUTONOMOUS, Tier.NOTIFY):
            return self._record(
                task, tier, Decision.ALLOW, f"{tier.value} tier proceeds"
            )

        # approve tier: requires a valid, unexpired, unconsumed approval
        # bound to this exact action.
        wanted = action_hash(task)
        row = self._conn.execute(
            "SELECT id FROM approvals "
            "WHERE task_id = ? AND action_hash = ? AND state = 'granted' "
            f"AND consumed_at IS NULL AND expires_at > {NOW_MS} "
            "ORDER BY requested_at LIMIT 1",
            (task.id, wanted),
        ).fetchone()

        if row is None:
            return self._record(
                task, tier, Decision.NEEDS_APPROVAL,
                "no valid, unexpired, unconsumed approval for this exact action",
            )

        if not self._consume(row["id"]):
            # Lost a race with another worker for the same token.
            return self._record(
                task, tier, Decision.NEEDS_APPROVAL,
                "approval was consumed by another worker first",
            )

        return self._record(
            task, tier, Decision.ALLOW, "approval consumed", row["id"]
        )

    def _consume(self, approval_id: str) -> bool:
        """Spend an approval exactly once.

        The unconsumed condition lives in the WHERE clause rather than in
        a prior SELECT, so concurrent workers cannot both succeed.
        """
        cur = self._conn.execute(
            "UPDATE approvals SET consumed_at = "
            f"{NOW_MS} WHERE id = ? AND consumed_at IS NULL",
            (approval_id,),
        )
        return cur.rowcount == 1

    def _record(
        self,
        task: Task,
        tier: Tier,
        decision: Decision,
        reason: str,
        approval_id: str | None = None,
    ) -> PolicyResult:
        """Every decision is audited, allow and deny alike.

        Denials are the interesting ones for a security review, but only
        having denials would make it impossible to prove a given action
        was ever authorised.
        """
        self._conn.execute(
            "INSERT INTO events (task_id, kind, detail) VALUES (?, ?, ?)",
            (task.id, f"policy_{decision.value}", json.dumps({
                "tier": tier.value,
                "reason": reason,
                "approval_id": approval_id,
                "action_hash": action_hash(task),
            })),
        )
        return PolicyResult(decision, tier, reason, approval_id)

    # --------------------------------------------------------- approvals

    def request_approval(self, task: Task, reason: str) -> str:
        """Open a pending approval request for a human to decide."""
        approval_id = uuid.uuid4().hex
        self._conn.execute(
            "INSERT INTO approvals (id, task_id, reason, action_hash, "
            "expires_at) VALUES (?, ?, ?, ?, ?)",
            (approval_id, task.id, reason, action_hash(task),
             _ts(_utcnow())),  # pending requests carry no grant window yet
        )
        return approval_id

    def grant(
        self,
        approval_id: str,
        decided_by: str,
        valid_for: timedelta = timedelta(minutes=15),
    ) -> None:
        """Approve a pending request, for a bounded window.

        The window is deliberately short. An approval is permission to do
        one thing now, not a standing grant.
        """
        self._conn.execute(
            "UPDATE approvals SET state = 'granted', decided_by = ?, "
            f"decided_at = {NOW_MS}, expires_at = ? "
            "WHERE id = ? AND state = 'pending'",
            (decided_by, _ts(_utcnow() + valid_for), approval_id),
        )

    def deny(self, approval_id: str, decided_by: str) -> None:
        self._conn.execute(
            "UPDATE approvals SET state = 'denied', decided_by = ?, "
            f"decided_at = {NOW_MS} WHERE id = ? AND state = 'pending'",
            (decided_by, approval_id),
        )

    def pending(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM approvals WHERE state = 'pending' "
            "ORDER BY requested_at"
        ).fetchall()
