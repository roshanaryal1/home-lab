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
from collections.abc import Iterator
from contextlib import contextmanager
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


def tool_action(task_id: str, tool: str, params: dict) -> str:
    """Canonical text of one tool call, as a human reviewer is shown it.

    Sorted keys and fixed separators, so the same call always renders
    (and hashes) the same, and any changed argument renders differently.
    """
    return json.dumps(
        {"task": task_id, "tool": tool, "params": params},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )


def tool_action_hash(task_id: str, tool: str, params: dict) -> str:
    """Fingerprint binding an approval to one exact tool call (item 1.1)."""
    return hashlib.sha256(
        tool_action(task_id, tool, params).encode("utf-8")
    ).hexdigest()


class PolicyEngine:
    """Decides whether a leased task may execute, and each tool call in it."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @contextmanager
    def _tx(self) -> Iterator[None]:
        """Wrap a compound operation in one atomic transaction.

        Mirrors ``TaskQueue._tx`` in lab/queue.py: same connection (this
        class is always constructed from a TaskQueue's ``_conn``), same
        problem. ``grant``/``deny`` update the approvals table and then
        separately release the parked task; a crash between the two left
        an approval marked granted with the task never returned to the
        queue. Issue #44.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

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

    def authorize_tool(self, task_id: str, tool: str, params: dict,
                       tier: Tier) -> PolicyResult:
        """The per-call gate. The broker calls this before every tool runs.

        ``tier`` comes from the broker's trusted registry, never from the
        task or the request, so a task cannot under-declare a tool's
        authority. An approve-tier call needs a granted, unexpired,
        unconsumed approval for this exact call; without one a pending
        request carrying the canonical call text is opened (once) and the
        call is refused. Every decision is audited, and if the audit
        write fails the caller sees an exception, never an allow.
        """
        action = tool_action(task_id, tool, params)
        wanted = tool_action_hash(task_id, tool, params)

        def record(decision: Decision, reason: str,
                   approval_id: str | None = None) -> PolicyResult:
            self._conn.execute(
                "INSERT INTO events (task_id, kind, detail) VALUES (?, ?, ?)",
                (task_id, f"tool_{decision.value}", json.dumps({
                    "tool": tool, "tier": tier.value, "reason": reason,
                    "approval_id": approval_id, "action_hash": wanted,
                })),
            )
            return PolicyResult(decision, tier, reason, approval_id)

        if tier is Tier.NEVER:
            return record(Decision.DENY, f"{tool} is never permitted")
        if tier in (Tier.AUTONOMOUS, Tier.NOTIFY):
            return record(Decision.ALLOW, f"{tier.value} tier proceeds")

        with self._tx():
            row = self._conn.execute(
                "SELECT id FROM approvals WHERE task_id = ? AND action_hash = ? "
                f"AND state = 'granted' AND consumed_at IS NULL AND expires_at > {NOW_MS} "
                "ORDER BY requested_at LIMIT 1",
                (task_id, wanted),
            ).fetchone()
            if row is not None and self._consume(row["id"]):
                return record(Decision.ALLOW, "approval consumed", row["id"])

            pending = self._conn.execute(
                "SELECT id FROM approvals WHERE task_id = ? AND action_hash = ? "
                "AND state = 'pending'",
                (task_id, wanted),
            ).fetchone()
            if pending is not None:
                approval_id = pending["id"]
            else:
                approval_id = uuid.uuid4().hex
                self._conn.execute(
                    "INSERT INTO approvals (id, task_id, reason, action_hash, "
                    "expires_at) VALUES (?, ?, ?, ?, ?)",
                    (approval_id, task_id, f"tool call {action}", wanted,
                     _ts(_utcnow())),
                )
            return record(Decision.NEEDS_APPROVAL,
                          f"no approval for this exact call: {action}", approval_id)

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
    ) -> str | None:
        """Approve a pending request, for a bounded window.

        The window is deliberately short. An approval is permission to do
        one thing now, not a standing grant.

        Returns the task id if a task was released back to the queue, so
        the caller can report that something will actually happen.
        """
        with self._tx():
            cur = self._conn.execute(
                "UPDATE approvals SET state = 'granted', decided_by = ?, "
                f"decided_at = {NOW_MS}, expires_at = ? "
                "WHERE id = ? AND state = 'pending'",
                (decided_by, _ts(_utcnow() + valid_for), approval_id),
            )
            if cur.rowcount == 0:
                return None
            return self._release_task(approval_id, "queued")

    def deny(self, approval_id: str, decided_by: str,
             reason: str = "denied") -> str | None:
        with self._tx():
            cur = self._conn.execute(
                "UPDATE approvals SET state = 'denied', decided_by = ?, "
                f"decided_at = {NOW_MS} WHERE id = ? AND state = 'pending'",
                (decided_by, approval_id),
            )
            if cur.rowcount == 0:
                return None
            return self._release_task(approval_id, "cancelled", reason)

    def _release_task(self, approval_id: str, to_state: str,
                      reason: str | None = None) -> str | None:
        """Move the parked task on, now that a human has decided.

        Without this, granting an approval updated a row and nothing
        else: the task stayed parked forever and the approval looked
        successful. That was issue #19.
        """
        row = self._conn.execute(
            "SELECT task_id FROM approvals WHERE id = ?", (approval_id,)
        ).fetchone()
        if row is None:
            return None
        task_id = row["task_id"]

        state = self._conn.execute(
            "SELECT state FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if state is None or state["state"] != "awaiting_approval":
            # Nothing parked, so nothing to release. A pre-emptive
            # approval granted before the task ran is still valid; it
            # will simply be consumed when the task reaches the gate.
            return None

        self._conn.execute(
            "UPDATE tasks SET state = ?, last_error = COALESCE(?, last_error), "
            f"updated_at = {NOW_MS}, available_at = {NOW_MS} WHERE id = ?",
            (to_state, reason, task_id),
        )
        self._conn.execute(
            "INSERT INTO events (task_id, kind, from_state, to_state, detail) "
            "VALUES (?, ?, 'awaiting_approval', ?, ?)",
            (task_id, to_state, to_state,
             json.dumps({"approval_id": approval_id, "reason": reason})),
        )
        return task_id

    def pending(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM approvals WHERE state = 'pending' "
            "ORDER BY requested_at"
        ).fetchall()
