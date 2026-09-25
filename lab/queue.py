"""SQLite-backed task queue with expiring leases.

Step 1 of the build order in the companion study's reference
architecture. The state machine and lease/recovery rules come from
section 9 of that document:

    queued -> leased -> running -> succeeded | failed
                                 | interrupted | cancelled

Leases expire. On supervisor restart, stale ``running`` tasks become
``interrupted`` and are requeued according to retry policy. Tasks that
are not marked idempotent are never auto-requeued, because destructive
actions must not be replayed blindly after recovery.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Self

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# Terminal states never transition again.
TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})

# Every legal transition. Anything not listed here is rejected, so an
# invalid transition is a loud error rather than silent corruption.
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"leased", "cancelled"}),
    "leased": frozenset({"running", "queued", "interrupted", "cancelled"}),
    "running": frozenset({"succeeded", "failed", "interrupted", "cancelled"}),
    "succeeded": frozenset(),
    "failed": frozenset({"queued"}),        # retry
    "interrupted": frozenset({"queued", "failed", "cancelled"}),
    "cancelled": frozenset(),
}


class TransitionError(RuntimeError):
    """Raised when a caller attempts an illegal state transition."""


class LeaseLost(RuntimeError):
    """Raised when a worker tries to commit a result it no longer owns.

    This is the fencing check. A worker whose lease expired while it was
    still working must not be able to write a result, because another
    supervisor may already have reclaimed and re-run the task. Losing the
    race is recoverable; two workers both committing is not.
    """


def _utcnow() -> datetime:
    return datetime.now(UTC)


# SQLite's datetime('now') is second-resolution, which is too coarse for
# lease arithmetic: with a short TTL the truncation can put an expiry in
# the past the instant it is written, so a renewal comparison never
# passes and a live worker looks abandoned. Both sides of every
# lease comparison therefore use millisecond precision.
NOW_MS = "strftime('%Y-%m-%d %H:%M:%f', 'now')"


def _ts(moment: datetime) -> str:
    """Format to match SQLite's strftime('%Y-%m-%d %H:%M:%f', 'now')."""
    utc = moment.astimezone(UTC)
    return f"{utc.strftime('%Y-%m-%d %H:%M:%S')}.{utc.microsecond // 1000:03d}"


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    state: str
    priority: int
    attempts: int
    max_attempts: int
    idempotent: bool
    payload: dict
    weight: str = "light"
    capability_tier: str = "autonomous"
    agent_kind: str | None = None
    last_error: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Task:
        return cls(
            id=row["id"],
            title=row["title"],
            state=row["state"],
            priority=row["priority"],
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            idempotent=bool(row["idempotent"]),
            payload=json.loads(row["payload"] or "{}"),
            weight=row["weight"],
            capability_tier=row["capability_tier"],
            agent_kind=row["agent_kind"],
            last_error=row["last_error"],
        )


class TaskQueue:
    """Durable task queue. One instance wraps one SQLite database."""

    def __init__(self, db_path: str | Path, owner: str | None = None) -> None:
        self.db_path = str(db_path)
        self.owner = owner or f"supervisor-{uuid.uuid4().hex[:8]}"
        self._conn = sqlite3.connect(self.db_path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._apply_schema()

    def _apply_schema(self) -> None:
        self._conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------ events

    def _record(
        self,
        task_id: str | None,
        kind: str,
        from_state: str | None = None,
        to_state: str | None = None,
        detail: dict | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO events (task_id, kind, from_state, to_state, detail) "
            "VALUES (?, ?, ?, ?, ?)",
            (task_id, kind, from_state, to_state,
             json.dumps(detail) if detail else None),
        )

    # ------------------------------------------------------------- write

    def add_task(
        self,
        title: str,
        payload: dict | None = None,
        *,
        priority: int = 100,
        agent_kind: str | None = None,
        idempotent: bool = False,
        max_attempts: int = 3,
        parent_id: str | None = None,
        weight: str = "light",
        capability_tier: str = "autonomous",
    ) -> str:
        task_id = uuid.uuid4().hex
        self._conn.execute(
            "INSERT INTO tasks (id, parent_id, title, payload, priority, "
            "agent_kind, idempotent, max_attempts, weight, capability_tier) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, parent_id, title, json.dumps(payload or {}), priority,
             agent_kind, int(idempotent), max_attempts, weight,
             capability_tier),
        )
        self._record(task_id, "created", None, "queued",
                     {"title": title, "priority": priority})
        return task_id

    def _transition(
        self,
        task_id: str,
        to_state: str,
        *,
        error: str | None = None,
        result: dict | None = None,
        available_in: timedelta | None = None,
    ) -> None:
        row = self._conn.execute(
            "SELECT state FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise TransitionError(f"no such task: {task_id}")
        from_state = row["state"]
        if to_state not in LEGAL_TRANSITIONS[from_state]:
            raise TransitionError(
                f"illegal transition {from_state} -> {to_state} for {task_id}"
            )

        delay = available_in if available_in is not None else timedelta()
        available_at = _ts(_utcnow() + delay)
        self._conn.execute(
            "UPDATE tasks SET state = ?, last_error = COALESCE(?, last_error), "
            "result = COALESCE(?, result), updated_at = datetime('now'), "
            "available_at = ? WHERE id = ?",
            (to_state, error, json.dumps(result) if result else None,
             available_at, task_id),
        )
        self._record(task_id, to_state, from_state, to_state,
                     {"error": error} if error else None)

    def lease(self, ttl_seconds: int = 300,
              weight: str | None = None) -> Task | None:
        """Claim the next runnable task, or return None if there is none.

        ``weight`` restricts the claim to one worker class. A worker only
        leases work it already has capacity to run, which is what keeps the
        number of leased-but-not-started tasks bounded.

        Runs in a single IMMEDIATE transaction so two supervisors racing
        for the same task cannot both win.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            if weight is None:
                row = self._conn.execute(
                    "SELECT * FROM tasks WHERE state = 'queued' "
                    f"AND available_at <= {NOW_MS} "
                    "ORDER BY priority ASC, created_at ASC LIMIT 1"
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT * FROM tasks WHERE state = 'queued' "
                    f"AND weight = ? AND available_at <= {NOW_MS} "
                    "ORDER BY priority ASC, created_at ASC LIMIT 1",
                    (weight,),
                ).fetchone()
            if row is None:
                self._conn.execute("COMMIT")
                return None

            task_id = row["id"]
            expires_at = _ts(_utcnow() + timedelta(seconds=ttl_seconds))
            self._conn.execute(
                "INSERT INTO leases (id, task_id, owner, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (uuid.uuid4().hex, task_id, self.owner, expires_at),
            )
            self._conn.execute(
                "UPDATE tasks SET state = 'leased', attempts = attempts + 1, "
                "updated_at = datetime('now') WHERE id = ?",
                (task_id,),
            )
            self._record(task_id, "leased", "queued", "leased",
                         {"owner": self.owner, "expires_at": expires_at})
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

        return self.get(task_id)

    # ------------------------------------------------- lease ownership

    def owns_lease(self, task_id: str) -> bool:
        """True if this instance holds a live, unexpired lease on the task."""
        row = self._conn.execute(
            "SELECT 1 FROM leases WHERE task_id = ? AND owner = ? "
            f"AND released_at IS NULL AND expires_at > {NOW_MS}",
            (task_id, self.owner),
        ).fetchone()
        return row is not None

    def renew_lease(self, task_id: str, ttl_seconds: int = 300) -> bool:
        """Push the lease expiry out. Returns False if the lease is gone.

        A worker calls this periodically while a long task runs. Without it
        a task that outlives its TTL looks abandoned to any other
        supervisor, which then reclaims and re-runs work that was never
        actually abandoned.
        """
        expires_at = _ts(_utcnow() + timedelta(seconds=ttl_seconds))
        cur = self._conn.execute(
            "UPDATE leases SET expires_at = ? WHERE task_id = ? AND owner = ? "
            f"AND released_at IS NULL AND expires_at > {NOW_MS}",
            (expires_at, task_id, self.owner),
        )
        return cur.rowcount > 0

    def _require_lease(self, task_id: str) -> None:
        """Fencing check. Refuse to commit a result we no longer own.

        Only applies to tasks that are actually leased or running. For a
        task in any other state the problem is an illegal transition, and
        that is a more useful error than a missing lease, so defer to
        ``_transition``.
        """
        ever = self._conn.execute(
            "SELECT 1 FROM leases WHERE task_id = ? AND owner = ? LIMIT 1",
            (task_id, self.owner),
        ).fetchone()
        if ever is None:
            # This caller never held a lease on this task, so the real
            # problem is an illegal transition. Let _transition say so,
            # which is the more useful message.
            return
        if not self.owns_lease(task_id):
            self._record(task_id, "lease_lost", detail={"owner": self.owner})
            raise LeaseLost(
                f"lease on {task_id} is no longer held by {self.owner}; "
                "refusing to commit a result that another worker may own"
            )

    def start(self, task_id: str) -> None:
        self._require_lease(task_id)
        self._transition(task_id, "running")

    def succeed(self, task_id: str, result: dict | None = None) -> None:
        self._require_lease(task_id)
        self._transition(task_id, "succeeded", result=result)
        self._release_lease(task_id)

    def fail(self, task_id: str, error: str,
             retry_in: timedelta | None = None) -> None:
        """Fail a task, retrying it if attempts remain."""
        self._require_lease(task_id)
        row = self._conn.execute(
            "SELECT attempts, max_attempts FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise TransitionError(f"no such task: {task_id}")

        self._transition(task_id, "failed", error=error)
        self._release_lease(task_id)
        if row["attempts"] < row["max_attempts"]:
            # `is not None`, not a truthiness check: timedelta(0) is falsy,
            # and an explicit "retry immediately" must not be silently
            # replaced by the default backoff.
            backoff = (retry_in if retry_in is not None
                       else timedelta(seconds=2 ** row["attempts"]))
            self._transition(task_id, "queued", available_in=backoff)

    def cancel(self, task_id: str, reason: str = "cancelled") -> None:
        self._transition(task_id, "cancelled", error=reason)
        self._release_lease(task_id)

    def _release_lease(self, task_id: str) -> None:
        # Not owner-scoped on purpose: recovery releases an expired lease
        # that belonged to a supervisor which is no longer running.
        self._conn.execute(
            "UPDATE leases SET released_at = datetime('now') "
            "WHERE task_id = ? AND released_at IS NULL",
            (task_id,),
        )

    # ---------------------------------------------------------- recovery

    def recover(self) -> dict[str, int]:
        """Reconcile state after a crash or restart.

        This is the method that previously caused duplicate execution. The
        old rule reclaimed anything in ``leased`` or ``running``, which
        meant a second supervisor would take work that the first was still
        actively doing. A task is only genuinely abandoned when:

        * its lease has **expired**, meaning nobody renewed it, or
        * the live lease belongs to **this owner**, which on startup can
          only mean a previous life of this same supervisor.

        Anything else belongs to a live worker and is left alone. The cost
        is that recovering another supervisor's crashed work waits for the
        lease TTL. That delay is the price of never double-running a task,
        and it is the right trade.
        """
        stranded = self._conn.execute(
            "SELECT t.id, t.idempotent, t.attempts, t.max_attempts "
            "FROM tasks t "
            "JOIN leases l ON l.task_id = t.id AND l.released_at IS NULL "
            "WHERE t.state IN ('leased', 'running') "
            f"  AND (l.expires_at <= {NOW_MS} OR l.owner = ?)",
            (self.owner,),
        ).fetchall()

        interrupted = requeued = held = 0
        for row in stranded:
            self._transition(row["id"], "interrupted",
                             error="lease expired or owner restarted")
            self._release_lease(row["id"])
            interrupted += 1

            if row["idempotent"] and row["attempts"] < row["max_attempts"]:
                self._transition(row["id"], "queued")
                requeued += 1
            else:
                held += 1

        if stranded:
            self._record(None, "recovery", detail={
                "interrupted": interrupted,
                "requeued": requeued,
                "held_for_review": held,
                "owner": self.owner,
            })
        return {"interrupted": interrupted, "requeued": requeued,
                "held_for_review": held}

    # ------------------------------------------------------------- reads

    def get(self, task_id: str) -> Task | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return Task.from_row(row) if row else None

    def counts(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT state, COUNT(*) AS n FROM tasks GROUP BY state"
        ).fetchall()
        return {row["state"]: row["n"] for row in rows}

    def events(self, task_id: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM events WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()
