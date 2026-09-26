"""Tests for the durable task queue.

The interesting cases are the recovery ones: the spec's whole point is
that a crash must not replay a destructive task, and that an expired
lease is detected rather than silently held forever.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from lab.queue import LeaseLost, TaskQueue, TransitionError


@pytest.fixture()
def q(tmp_path: Path) -> TaskQueue:
    with TaskQueue(tmp_path / "lab.db", owner="test-supervisor") as queue:
        yield queue


def test_added_task_starts_queued(q: TaskQueue) -> None:
    task_id = q.add_task("build the thing")
    task = q.get(task_id)
    assert task is not None
    assert task.state == "queued"
    assert task.attempts == 0


def test_lease_then_run_then_succeed(q: TaskQueue) -> None:
    task_id = q.add_task("build the thing")

    leased = q.lease()
    assert leased is not None and leased.id == task_id
    assert leased.state == "leased"
    assert leased.attempts == 1

    q.start(task_id)
    assert q.get(task_id).state == "running"

    q.succeed(task_id, {"ok": True})
    assert q.get(task_id).state == "succeeded"


def test_lease_returns_none_when_queue_empty(q: TaskQueue) -> None:
    assert q.lease() is None


def test_priority_then_fifo_ordering(q: TaskQueue) -> None:
    q.add_task("normal", priority=100)
    urgent = q.add_task("urgent", priority=1)
    assert q.lease().id == urgent


def test_leased_task_is_not_leased_twice(q: TaskQueue) -> None:
    q.add_task("only one")
    assert q.lease() is not None
    assert q.lease() is None, "a leased task must not be handed out again"


def test_illegal_transition_is_rejected(q: TaskQueue) -> None:
    task_id = q.add_task("no skipping")
    # queued -> succeeded skips leased and running entirely.
    with pytest.raises(TransitionError):
        q.succeed(task_id)


def test_terminal_state_cannot_transition(q: TaskQueue) -> None:
    task_id = q.add_task("done is done")
    q.lease()
    q.start(task_id)
    q.succeed(task_id)
    with pytest.raises(TransitionError):
        q.cancel(task_id)


def test_failure_retries_until_max_attempts(q: TaskQueue) -> None:
    task_id = q.add_task("flaky", max_attempts=2)

    q.lease()
    q.start(task_id)
    q.fail(task_id, "boom", retry_in=timedelta(0))
    assert q.get(task_id).state == "queued", "should retry while attempts remain"

    q.lease()
    q.start(task_id)
    q.fail(task_id, "boom again", retry_in=timedelta(0))
    task = q.get(task_id)
    assert task.state == "failed", "should stay failed once attempts are spent"
    assert task.attempts == 2
    assert task.last_error == "boom again"


def test_retry_backoff_makes_task_unavailable_immediately(q: TaskQueue) -> None:
    task_id = q.add_task("backoff", max_attempts=3)
    q.lease()
    q.start(task_id)
    q.fail(task_id, "transient", retry_in=timedelta(minutes=5))

    assert q.get(task_id).state == "queued"
    assert q.lease() is None, "backoff must keep the task out of the queue"


# ----------------------------------------------------------- recovery


def test_recovery_requeues_idempotent_task(q: TaskQueue) -> None:
    task_id = q.add_task("safe to repeat", idempotent=True)
    q.lease()
    q.start(task_id)
    # Simulate a crash: the process dies here, leaving state 'running'.

    stats = q.recover()
    assert stats == {"interrupted": 1, "requeued": 1, "held_for_review": 0}
    assert q.get(task_id).state == "queued"


def test_recovery_holds_non_idempotent_task_for_review(q: TaskQueue) -> None:
    """The core safety rule: never blindly replay a destructive task."""
    task_id = q.add_task("sends real email", idempotent=False)
    q.lease()
    q.start(task_id)

    stats = q.recover()
    assert stats == {"interrupted": 1, "requeued": 0, "held_for_review": 1}
    assert q.get(task_id).state == "interrupted"
    assert q.lease() is None, "an interrupted destructive task must not re-run"


def test_recovery_holds_idempotent_task_with_no_attempts_left(q: TaskQueue) -> None:
    task_id = q.add_task("repeatable but exhausted", idempotent=True,
                         max_attempts=1)
    q.lease()
    q.start(task_id)

    stats = q.recover()
    assert stats["requeued"] == 0
    assert stats["held_for_review"] == 1
    assert q.get(task_id).state == "interrupted"


def test_recovery_is_a_noop_on_a_clean_queue(q: TaskQueue) -> None:
    q.add_task("untouched")
    assert q.recover() == {"interrupted": 0, "requeued": 0, "held_for_review": 0}
    assert q.get(q.lease().id).state == "leased"


def test_recovery_releases_the_lease(q: TaskQueue) -> None:
    task_id = q.add_task("stranded", idempotent=True)
    q.lease()
    q.start(task_id)
    q.recover()

    live = q._conn.execute(
        "SELECT COUNT(*) AS n FROM leases "
        "WHERE task_id = ? AND released_at IS NULL",
        (task_id,),
    ).fetchone()["n"]
    assert live == 0, "recovery must not leave a live lease behind"


def test_restart_reclaims_its_own_stranded_work(tmp_path: Path) -> None:
    """A restart of the same supervisor picks its own work back up at once."""
    db = tmp_path / "lab.db"
    owner = "supervisor-main"

    with TaskQueue(db, owner=owner) as first:
        task_id = first.add_task("interrupted work", idempotent=True)
        first.lease()
        first.start(task_id)

    with TaskQueue(db, owner=owner) as restarted:
        assert restarted.get(task_id).state == "running"
        stats = restarted.recover()
        assert stats["requeued"] == 1
        assert restarted.lease().id == task_id


def test_a_different_supervisor_will_not_steal_live_work(
    tmp_path: Path,
) -> None:
    """The duplicate-execution fix, stated as a test.

    Supervisor A is still working. Supervisor B must not decide the task is
    abandoned and re-run it. Before the fix, B reclaimed and requeued it.
    """
    db = tmp_path / "lab.db"

    with TaskQueue(db, owner="supervisor-a") as a:
        task_id = a.add_task("long running work", idempotent=True)
        a.lease(ttl_seconds=300)
        a.start(task_id)

        with TaskQueue(db, owner="supervisor-b") as b:
            stats = b.recover()
            assert stats == {"interrupted": 0, "requeued": 0,
                             "held_for_review": 0}
            assert b.get(task_id).state == "running"
            assert b.lease() is None, "B must not be handed A's live task"


def test_expired_lease_is_reclaimed_by_another_supervisor(
    tmp_path: Path,
) -> None:
    """Genuinely abandoned work is still recovered, just not prematurely."""
    db = tmp_path / "lab.db"

    with TaskQueue(db, owner="supervisor-a") as a:
        task_id = a.add_task("abandoned", idempotent=True)
        a.lease(ttl_seconds=300)
        a.start(task_id)
        # The worker dies here. Nothing renews the lease, so it expires.
        a._conn.execute(
            "UPDATE leases SET expires_at = datetime('now', '-1 second') "
            "WHERE task_id = ?", (task_id,)
        )

    with TaskQueue(db, owner="supervisor-b") as b:
        stats = b.recover()
        assert stats["requeued"] == 1
        assert b.lease().id == task_id


def test_renewal_keeps_a_long_task_alive(tmp_path: Path) -> None:
    """Renewal is what stops a slow task from looking abandoned."""
    db = tmp_path / "lab.db"

    with TaskQueue(db, owner="supervisor-a") as a:
        task_id = a.add_task("slow", idempotent=True)
        a.lease(ttl_seconds=1)
        a.start(task_id)
        assert a.renew_lease(task_id, ttl_seconds=300) is True

        with TaskQueue(db, owner="supervisor-b") as b:
            assert b.recover()["interrupted"] == 0, "renewed lease is alive"


def test_worker_cannot_commit_after_losing_its_lease(tmp_path: Path) -> None:
    """Fencing: losing the race is fine, both workers committing is not."""
    db = tmp_path / "lab.db"

    with TaskQueue(db, owner="supervisor-a") as a:
        task_id = a.add_task("contended", idempotent=True)
        a.lease(ttl_seconds=300)
        a.start(task_id)
        a._conn.execute(
            "UPDATE leases SET expires_at = datetime('now', '-1 second') "
            "WHERE task_id = ?", (task_id,)
        )
        # A is still working, unaware. B reclaims and requeues.
        with TaskQueue(db, owner="supervisor-b") as b:
            b.recover()

        with pytest.raises(LeaseLost):
            a.succeed(task_id, {"written": "by the wrong worker"})


# -------------------------------------------------------------- audit


def test_every_transition_is_audited(q: TaskQueue) -> None:
    task_id = q.add_task("audited")
    q.lease()
    q.start(task_id)
    q.succeed(task_id)

    kinds = [row["kind"] for row in q.events(task_id)]
    assert kinds == ["created", "leased", "running", "succeeded"]


def test_counts_reports_each_state(q: TaskQueue) -> None:
    q.add_task("one")
    q.add_task("two")
    q.lease()
    assert q.counts() == {"queued": 1, "leased": 1}


# ------------------------------------------------------------- schema


def test_one_live_lease_per_task_is_enforced_by_the_database(q: TaskQueue) -> None:
    task_id = q.add_task("contended")
    q.lease()
    with pytest.raises(sqlite3.IntegrityError):
        q._conn.execute(
            "INSERT INTO leases (id, task_id, owner, expires_at) "
            "VALUES ('duplicate', ?, 'other', datetime('now', '+5 minutes'))",
            (task_id,),
        )


def test_capability_tier_is_constrained(q: TaskQueue) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        q._conn.execute(
            "INSERT INTO agents (id, name, kind, capability_tier) "
            "VALUES ('a1', 'rogue', 'coder', 'root')"
        )


def test_wal_mode_is_active(q: TaskQueue) -> None:
    mode = q._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
