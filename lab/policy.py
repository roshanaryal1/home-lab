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
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from lab import operator as operator_keys
from lab.audit import append_event
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


# Part of every intent. Bumping it invalidates every outstanding grant,
# which is the point: an approval given under one policy is not an
# approval under the next.
POLICY_VERSION = "2026-09-29.1"


def canonical(obj: object) -> str:
    """The defined serialization an intent is shown and hashed in.

    Sorted keys, no insignificant whitespace, UTF-8 text rather than
    escapes. Close to RFC 8785 for the JSON this lab produces (strings,
    integers, booleans, nulls, lists, objects); not a full implementation.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def intent_hash(intent: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(intent).encode("utf-8")).hexdigest()


def task_intent(task: Task) -> dict[str, Any]:
    """What a task-level approval authorizes: this task, this payload."""
    return {"kind": "task", "task": task.id, "agent_kind": task.agent_kind,
            "payload": task.payload, "policy_version": POLICY_VERSION}


def tool_intent(task_id: str, tool: str, params: dict[str, Any],
                preconditions: dict[str, Any] | None = None) -> dict[str, Any]:
    """What a tool-call approval authorizes (item 1.4).

    ``preconditions`` is the state the call will act on, as the broker
    measured it (for example a hash of the workspace). If that state has
    changed by the time the call is made again, the intent differs and
    the old grant does not apply. The lease generation is deliberately
    absent: approving parks the task, and the rerun that uses the grant
    always holds a newer lease.
    """
    return {"kind": "tool", "task": task_id, "tool": tool, "params": params,
            "preconditions": preconditions or {}, "policy_version": POLICY_VERSION}


def action_hash(task: Task) -> str:
    """Fingerprint of a task-level intent. Changes with any parameter."""
    return intent_hash(task_intent(task))


class PolicyEngine:
    """Decides whether a leased task may execute, and each tool call in it."""

    def __init__(self, conn: sqlite3.Connection,
                 operator_public_key: Ed25519PublicKey | None = None) -> None:
        self._conn = conn
        # When set, only approvals signed by the operator are honoured
        # (item 4.5). None means signatures are not checked: development
        # on dummy data, and the state every test starts in.
        self._operator_key = operator_public_key

    @property
    def enforces_operator_signatures(self) -> bool:
        return self._operator_key is not None

    def audit(self, task_id: str | None, kind: str, detail: dict[str, Any]) -> None:
        """Append a non-transition event to the hash-chained audit log."""
        append_event(self._conn, task_id, kind, detail=detail)

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
        # bound to this exact action. Lookup, consumption and the audit
        # record commit together, and the consuming UPDATE re-checks
        # every condition, so an approval that expires between lookup
        # and use is not spent (R08).
        wanted = action_hash(task)
        with self._tx():
            taken = self._take_granted(task.id, wanted)
            if taken is None:
                return self._record(
                    task, tier, Decision.NEEDS_APPROVAL,
                    "no valid, unexpired, unconsumed approval for this exact action",
                )
            return self._record(
                task, tier, Decision.ALLOW, "approval consumed", taken
            )

    def authorize_tool(self, task_id: str, tool: str, params: dict[str, Any],
                       tier: Tier, preconditions: dict[str, Any] | None = None) -> PolicyResult:
        """The per-call gate. The broker calls this before every tool runs.

        ``tier`` comes from the broker's trusted registry, never from the
        task or the request, so a task cannot under-declare a tool's
        authority. An approve-tier call needs a granted, unexpired,
        unconsumed approval for this exact call; without one a pending
        request carrying the canonical call text is opened (once) and the
        call is refused. Every decision is audited, and if the audit
        write fails the caller sees an exception, never an allow.
        """
        intent = tool_intent(task_id, tool, params, preconditions)
        action = canonical(intent)
        wanted = intent_hash(intent)

        def record(decision: Decision, reason: str,
                   approval_id: str | None = None) -> PolicyResult:
            append_event(self._conn, task_id, f"tool_{decision.value}", detail={
                "tool": tool, "tier": tier.value, "reason": reason,
                "approval_id": approval_id, "action_hash": wanted,
            })
            return PolicyResult(decision, tier, reason, approval_id)

        if tier is Tier.NEVER:
            return record(Decision.DENY, f"{tool} is never permitted")
        if tier in (Tier.AUTONOMOUS, Tier.NOTIFY):
            return record(Decision.ALLOW, f"{tier.value} tier proceeds")

        with self._tx():
            taken = self._take_granted(task_id, wanted)
            if taken is not None:
                return record(Decision.ALLOW, "approval consumed", taken)

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
                    "INSERT INTO approvals (id, task_id, reason, action_hash, intent, "
                    "expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (approval_id, task_id, f"tool call {tool}", wanted, action,
                     _ts(_utcnow())),
                )
            return record(Decision.NEEDS_APPROVAL,
                          f"no approval for this exact call: {action}", approval_id)

    def _take_granted(self, task_id: str, wanted_hash: str) -> str | None:
        """Find and spend one usable approval for exactly this action.

        Candidates are tried in order and each must pass the operator
        signature check when one is configured, so a forged or edited row
        cannot shadow a genuine approval queued behind it (item 4.5).
        """
        rows = self._conn.execute(
            "SELECT id, expires_at, decided_by, signature FROM approvals "
            "WHERE task_id = ? AND action_hash = ? AND state = 'granted' "
            f"AND consumed_at IS NULL AND expires_at > {NOW_MS} ORDER BY requested_at",
            (task_id, wanted_hash),
        ).fetchall()
        for row in rows:
            if self._operator_key is not None and not operator_keys.verify(
                    self._operator_key, row["signature"], row["id"], wanted_hash,
                    row["expires_at"], row["decided_by"] or ""):
                append_event(self._conn, task_id, "approval_rejected", detail={
                    "approval_id": row["id"], "reason": "no valid operator signature"})
                continue
            if self._consume(row["id"], wanted_hash):
                return str(row["id"])
        return None

    def _consume(self, approval_id: str, wanted_hash: str) -> bool:
        """Reserve an approval for exactly this intent, exactly once.

        Every condition is in the one UPDATE rather than a prior SELECT:
        granted, unspent, unexpired at this moment, and bound to the
        intent being executed. Two consumers cannot both succeed, and an
        approval that expired after it was looked up is not spent (R08).
        """
        cur = self._conn.execute(
            f"UPDATE approvals SET consumed_at = {NOW_MS} "
            "WHERE id = ? AND state = 'granted' AND consumed_at IS NULL "
            f"AND expires_at > {NOW_MS} AND action_hash = ?",
            (approval_id, wanted_hash),
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
        append_event(self._conn, task.id, f"policy_{decision.value}", detail={
            "tier": tier.value,
            "reason": reason,
            "approval_id": approval_id,
            "action_hash": action_hash(task),
        })
        return PolicyResult(decision, tier, reason, approval_id)

    # --------------------------------------------------------- approvals

    def request_approval(self, task: Task, reason: str) -> str:
        """Open a pending approval request for a human to decide."""
        approval_id = uuid.uuid4().hex
        self._conn.execute(
            "INSERT INTO approvals (id, task_id, reason, action_hash, intent, "
            "expires_at) VALUES (?, ?, ?, ?, ?, ?)",
            (approval_id, task.id, reason, action_hash(task),
             canonical(task_intent(task)),
             _ts(_utcnow())),  # pending requests carry no grant window yet
        )
        return approval_id

    def grant(
        self,
        approval_id: str,
        decided_by: str,
        valid_for: timedelta = timedelta(minutes=15),
        signer: Ed25519PrivateKey | None = None,
    ) -> str | None:
        """Approve a pending request, for a bounded window.

        The window is deliberately short. An approval is permission to do
        one thing now, not a standing grant.

        Returns the task id if a task was released back to the queue, so
        the caller can report that something will actually happen.
        """
        with self._tx():
            row = self._conn.execute(
                "SELECT action_hash FROM approvals WHERE id = ? AND state = 'pending'",
                (approval_id,)).fetchone()
            if row is None:
                return None
            expires = _ts(_utcnow() + valid_for)
            signature = (operator_keys.sign(signer, approval_id, row["action_hash"],
                                            expires, decided_by)
                         if signer is not None else None)
            cur = self._conn.execute(
                "UPDATE approvals SET state = 'granted', decided_by = ?, "
                f"decided_at = {NOW_MS}, expires_at = ?, signature = ? "
                "WHERE id = ? AND state = 'pending'",
                (decided_by, expires, signature, approval_id),
            )
            if cur.rowcount == 0:
                return None
            append_event(self._conn, None, "approval_granted", detail={
                "approval_id": approval_id, "decided_by": decided_by,
                "signed": signature is not None})
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
        task_id: str = row["task_id"]

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
        append_event(self._conn, task_id, to_state, "awaiting_approval", to_state,
                     {"approval_id": approval_id, "reason": reason})
        return task_id

    def pending(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM approvals WHERE state = 'pending' "
            "ORDER BY requested_at"
        ).fetchall()
