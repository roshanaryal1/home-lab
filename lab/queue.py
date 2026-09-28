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

import contextlib
import json
import sqlite3
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Self

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# SQLite 3.7.0 through 3.51.2 carry the WAL-reset corruption bug, a data
# race between two connections checkpointing and writing at the same
# instant (https://sqlite.org/wal.html, section 11). It is fixed in
# 3.51.3, with backports in 3.50.7 and 3.44.6. This lab runs two or more
# connections on one WAL file, which is exactly the affected pattern.
WAL_RESET_FIXED: tuple[tuple[int, int, int], ...] = ((3, 51, 3), (3, 50, 7), (3, 44, 6))

# How long a connection waits on a competing writer before SQLITE_BUSY.
BUSY_TIMEOUT_MS = 5000


def sqlite_is_safe(version: str) -> bool:
    """True when ``version`` carries the fix for the WAL-reset bug."""
    parts = tuple(int(x) for x in version.split(".")[:3])
    v = (*parts, 0, 0, 0)[:3]
    if v >= WAL_RESET_FIXED[0]:
        return True
    return any(v[:2] == f[:2] and v >= f for f in WAL_RESET_FIXED[1:])

# Terminal states never transition again.
TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})

# Every legal transition. Anything not listed here is rejected, so an
# invalid transition is a loud error rather than silent corruption.
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"leased", "cancelled"}),
    "leased": frozenset({"running", "queued", "awaiting_approval",
                         "interrupted", "cancelled"}),
    # Parking is not cancelling. A task waiting on a human decision is
    # paused, and must be able to return to the queue once that decision
    # arrives. Routing it through 'cancelled' made approving it a no-op.
    "awaiting_approval": frozenset({"queued", "cancelled", "interrupted"}),
    "running": frozenset({"succeeded", "failed", "interrupted", "cancelled"}),
    "succeeded": frozenset(),
    "failed": frozenset({"queued"}),        # retry
    "interrupted": frozenset({"queued", "failed", "cancelled"}),
    "cancelled": frozenset(),
}


class TransitionError(RuntimeError):
    """Raised when a caller attempts an illegal state transition."""


class UnsafeSQLite(RuntimeError):
    """Raised at start-up when the linked SQLite has the WAL-reset bug."""


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
        # Fresh every construction, unlike `owner`. See the `holder`
        # column comment in schema.sql: this is what ordinary fencing
        # checks, `owner` is what recover()'s restart-reclaim checks.
        self._holder = uuid.uuid4().hex
        self.sqlite_version = sqlite3.sqlite_version
        if not sqlite_is_safe(self.sqlite_version):
            raise UnsafeSQLite(
                f"SQLite {self.sqlite_version} has the WAL-reset corruption bug; "
                "use a Python linked against 3.51.3+, 3.50.7+ or 3.44.6+"
            )
        self._conn = sqlite3.connect(self.db_path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._apply_schema()
        self._apply_durability()

    def _apply_schema(self) -> None:
        self._conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    def _apply_durability(self) -> None:
        """Per-connection settings; SQLite does not persist any of these.

        NORMAL in WAL mode can lose the last commits on power loss, and on
        macOS a plain fsync does not flush the drive cache, so fullfsync
        is what makes FULL mean durable there. Crash recovery and the
        retry rules both assume a committed transition survives. Applied
        after the schema script so nothing in it can override them.
        """
        for pragma in (
            "foreign_keys = ON",
            f"busy_timeout = {BUSY_TIMEOUT_MS}",
            "synchronous = FULL",
            "fullfsync = ON",
            "checkpoint_fullfsync = ON",
        ):
            self._conn.execute(f"PRAGMA {pragma}")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @contextlib.contextmanager
    def _tx(self) -> Iterator[None]:
        """Wrap a compound operation in one atomic transaction.

        Without this, a crash between two statements of the same logical
        operation (a state UPDATE, then a separate lease release, say)
        can leave the database in a state no single statement produced
        and no caller expects. Issue #44: a crash between transitioning
        a task to ``awaiting_approval`` and releasing its lease left the
        lease permanently live, so the task could never be leased again
        even after a human granted the approval.

        Callers must not nest this: SQLite does not support nested
        ``BEGIN``. Preconditions that should survive a rollback (like
        ``_require_lease``'s own audit record on failure) must run
        before entering this block, not inside it.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

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
                "INSERT INTO leases (id, task_id, owner, holder, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (uuid.uuid4().hex, task_id, self.owner, self._holder, expires_at),
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
        """True if this instance holds a live, unexpired lease on the task.

        Keyed on ``holder``, not ``owner``. ``owner`` is a stable name
        shared across a process restart on purpose (recover() uses it);
        two live instances can share it. ``holder`` is unique per
        TaskQueue construction, which is what makes this check mean
        "this specific instance", not "some instance with this name".
        """
        row = self._conn.execute(
            "SELECT 1 FROM leases WHERE task_id = ? AND holder = ? "
            f"AND released_at IS NULL AND expires_at > {NOW_MS}",
            (task_id, self._holder),
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
            "UPDATE leases SET expires_at = ? WHERE task_id = ? AND holder = ? "
            f"AND released_at IS NULL AND expires_at > {NOW_MS}",
            (expires_at, task_id, self._holder),
        )
        return cur.rowcount > 0

    def _require_lease(self, task_id: str) -> None:
        """Fencing check. Refuse to commit a result this holder does not
        currently, live, own. Unconditionally: no shortcut.

        Issue #47 (HL03). The previous version skipped this check
        entirely whenever this caller had never held any lease row at
        all for the task, on the theory that the task was probably
        still ``queued`` and ``_transition`` would give a clearer error.
        That reasoning only holds when nobody else has a live lease on
        it either. If a *different* holder's live lease already put
        the task in ``running``, the shortcut let this caller, with no
        relationship to the task whatsoever, walk straight through to
        ``_transition`` and succeed, since running -> succeeded is a
        legal transition on its own. There is no safe shortcut for
        this check; it must always hold.
        """
        if not self.owns_lease(task_id):
            self._record(task_id, "lease_lost", detail={"owner": self.owner})
            raise LeaseLost(
                f"no live lease on {task_id} is held by this instance "
                f"(owner {self.owner}); refusing to commit a result"
            )

    def start(self, task_id: str) -> None:
        self._require_lease(task_id)
        self._transition(task_id, "running")

    def succeed(self, task_id: str, result: dict | None = None) -> None:
        self._require_lease(task_id)
        with self._tx():
            self._transition(task_id, "succeeded", result=result)
            self._release_lease(task_id)

    def fail(self, task_id: str, error: str,
             retry_in: timedelta | None = None) -> None:
        """Fail a task, retrying it if attempts remain and it is safe to.

        Issue #51 (HL07): idempotency has to govern *every* retry, not
        only the crash-recovery path in ``recover()``. An ordinary
        failure is not proof nothing happened, a timeout after a real
        side effect (an email actually sent, the confirmation lost)
        looks identical to one where nothing started. Auto-retrying a
        non-idempotent task here would risk repeating that side effect.
        A non-idempotent failure is left in ``failed`` rather than
        requeued, the same "hold for review" outcome ``recover()``
        already gives a non-idempotent task that ran out of attempts.
        """
        self._require_lease(task_id)
        row = self._conn.execute(
            "SELECT attempts, max_attempts, idempotent FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise TransitionError(f"no such task: {task_id}")

        with self._tx():
            self._transition(task_id, "failed", error=error)
            self._release_lease(task_id)
            if row["idempotent"] and row["attempts"] < row["max_attempts"]:
                # `is not None`, not a truthiness check: timedelta(0) is
                # falsy, and an explicit "retry immediately" must not be
                # silently replaced by the default backoff.
                backoff = (retry_in if retry_in is not None
                           else timedelta(seconds=2 ** row["attempts"]))
                self._transition(task_id, "queued", available_in=backoff)

    def park_for_approval(self, task_id: str, reason: str) -> None:
        """Pause a task until a human decides. Releases the lease.

        The lease goes back because the task is not being worked on: a
        human may take minutes or days, and holding a lease that long
        would block recovery and mislead every other supervisor.
        """
        self._require_lease(task_id)
        with self._tx():
            self._transition(task_id, "awaiting_approval", error=reason)
            self._release_lease(task_id)

    def resume_after_approval(self, task_id: str) -> None:
        """Return an approved task to the queue so a worker can pick it up."""
        self._transition(task_id, "queued")

    def cancel(self, task_id: str, reason: str = "cancelled") -> None:
        with self._tx():
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
            # One transaction per stranded task, not one for the whole
            # batch: a crash partway through recovery must not leave an
            # already-reconciled task's writes half-applied just because
            # a later row in the same batch failed.
            with self._tx():
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
                "sqlite_version": self.sqlite_version,
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
