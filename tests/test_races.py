"""Races and crashes against the queue (item 2.4, #61).

Three kinds of evidence, each on a real database file with more than one
connection:

* two-connection races, with threads released together by a barrier;
* a Hypothesis state machine that drives two queue instances through
  random operations and checks every invariant after every step;
* real processes killed with SIGKILL part-way through a transaction.

A process kill proves atomicity against a crash of the supervisor. It
does not prove durability against power loss: that needs the Mac mini
and stays in the drill runbook.
"""

from __future__ import annotations

import contextlib
import itertools
import os
import random
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from lab.queue import (
    LEGAL_TRANSITIONS,
    NOW_MS,
    TERMINAL_STATES,
    LeaseLost,
    LeaseToken,
    TaskQueue,
    TransitionError,
    _ts,
    _utcnow,
)

ROOT = Path(__file__).resolve().parent.parent
CHILD = ROOT / "tests" / "crash_child.py"
CHILD_TIMEOUT = 30


# ------------------------------------------------------------ invariants


def check_invariants(db_path: str | Path) -> None:
    """Assert everything that must hold in every committed state.

    Uses a plain connection and SELECTs only, so that after a killed
    writer it also exercises WAL recovery.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

        for task in conn.execute("SELECT * FROM tasks").fetchall():
            tid, state = task["id"], task["state"]
            live = conn.execute(
                "SELECT COUNT(*) FROM leases WHERE task_id = ? AND released_at IS NULL",
                (tid,)).fetchone()[0]
            if state in ("leased", "running"):
                assert live == 1, f"{tid} is {state} with {live} live leases"
            else:
                assert live == 0, f"{tid} is {state} but holds {live} live leases"
            assert task["executions"] <= task["attempts"], (
                f"{tid}: executions {task['executions']} > attempts {task['attempts']}")
            assert task["executions"] >= 0

            generations = [r[0] for r in conn.execute(
                "SELECT generation FROM leases WHERE task_id = ? ORDER BY generation",
                (tid,))]
            assert generations == list(range(1, len(generations) + 1)), (
                f"{tid}: lease generations {generations}")

            chain = conn.execute(
                "SELECT from_state, to_state FROM events "
                "WHERE task_id = ? AND to_state IS NOT NULL ORDER BY id",
                (tid,)).fetchall()
            assert chain, f"{tid} has no transition events"
            assert (chain[0]["from_state"], chain[0]["to_state"]) == (None, "queued")
            for prev, cur in itertools.pairwise(chain):
                assert cur["from_state"] == prev["to_state"], (
                    f"{tid}: event chain broken at {prev['to_state']} -> {cur['from_state']}")
                assert cur["to_state"] in LEGAL_TRANSITIONS[cur["from_state"]], (
                    f"{tid}: illegal {cur['from_state']} -> {cur['to_state']}")
            assert chain[-1]["to_state"] == state, (
                f"{tid}: last event says {chain[-1]['to_state']}, row says {state}")
            if state in TERMINAL_STATES:
                assert live == 0

        one_live = conn.execute(
            "SELECT task_id FROM leases WHERE released_at IS NULL "
            "GROUP BY task_id HAVING COUNT(*) > 1").fetchall()
        assert one_live == []
    finally:
        conn.close()


def snapshot(db_path: str | Path) -> dict[str, Any]:
    conn = sqlite3.connect(str(db_path))
    try:
        return {
            "tasks": conn.execute(
                "SELECT id, state, attempts, executions, last_error, available_at "
                "FROM tasks ORDER BY id").fetchall(),
            "leases": conn.execute(
                "SELECT id, task_id, holder, generation, expires_at, released_at "
                "FROM leases ORDER BY id").fetchall(),
            "events": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        }
    finally:
        conn.close()


def single_task_id(db_path: str | Path) -> str:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT id FROM tasks").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    return str(rows[0][0])


def lease_started(q: TaskQueue, *, idempotent: bool = True) -> tuple[str, LeaseToken]:
    task_id = q.add_task("race target", idempotent=idempotent, max_attempts=3)
    task = q.lease()
    assert task is not None and task.id == task_id and task.lease is not None
    q.start(task.lease)
    return task_id, task.lease


@contextlib.contextmanager
def before_next_transaction(q: TaskQueue, hook: Callable[[], None]) -> Iterator[None]:
    """Run ``hook`` once, just before ``q`` opens its next transaction.

    That is the gap between a scan and the write it justifies, made
    deterministic instead of hoped for.
    """
    original = q._tx
    fired: list[bool] = []

    @contextlib.contextmanager
    def patched() -> Iterator[None]:
        if not fired:
            fired.append(True)
            hook()
        with original():
            yield

    q._tx = patched  # type: ignore[method-assign]
    try:
        yield
    finally:
        del q._tx


# ------------------------------------------------------ two-connection races


def run_threads(targets: list[Callable[[], None]]) -> list[BaseException]:
    errors: list[BaseException] = []

    def wrap(target: Callable[[], None]) -> Callable[[], None]:
        def run() -> None:
            try:
                target()
            except BaseException as exc:
                errors.append(exc)
        return run

    threads = [threading.Thread(target=wrap(t), daemon=True) for t in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join(CHILD_TIMEOUT)
        assert not t.is_alive(), "thread did not finish"
    return errors


@pytest.mark.safety
def test_concurrent_leases_never_hand_out_a_task_twice(tmp_path: Path) -> None:
    db = tmp_path / "lab.db"
    seed = TaskQueue(db, owner="seed")
    task_ids = {seed.add_task(f"task {i}") for i in range(16)}
    workers = 4
    barrier = threading.Barrier(workers)
    claimed: list[LeaseToken] = []
    lock = threading.Lock()

    def worker(n: int) -> Callable[[], None]:
        def run() -> None:
            q = TaskQueue(db, owner=f"worker-{n}")
            barrier.wait(CHILD_TIMEOUT)
            while (task := q.lease(ttl_seconds=3600)) is not None:
                assert task.lease is not None
                with lock:
                    claimed.append(task.lease)
        return run

    assert run_threads([worker(n) for n in range(workers)]) == []
    assert {t.task_id for t in claimed} == task_ids
    assert len(claimed) == len(task_ids), "a task was leased more than once"
    assert all(t.generation == 1 for t in claimed)
    check_invariants(db)


@pytest.mark.safety
def test_finish_racing_a_same_owner_recover_has_exactly_one_winner(tmp_path: Path) -> None:
    db = tmp_path / "lab.db"
    main = TaskQueue(db, owner="main")
    for round_no in range(30):
        task_id = main.add_task(f"race {round_no}", idempotent=True)
        barrier = threading.Barrier(2)
        outcome: dict[str, Any] = {}

        def finisher(barrier: threading.Barrier = barrier,
                     outcome: dict[str, Any] = outcome) -> None:
            a = TaskQueue(db, owner="same")
            task = a.lease()
            assert task is not None and task.lease is not None
            a.start(task.lease)
            barrier.wait(CHILD_TIMEOUT)
            try:
                a.succeed(task.lease)
                outcome["succeed"] = "ok"
            except LeaseLost:
                outcome["succeed"] = "lost"

        def recoverer(barrier: threading.Barrier = barrier,
                      outcome: dict[str, Any] = outcome) -> None:
            b = TaskQueue(db, owner="same")
            barrier.wait(CHILD_TIMEOUT)
            outcome["recover"] = b.recover()

        errors = run_threads([finisher, recoverer])
        assert errors == [], f"round {round_no}: {errors}"
        state = main.get(task_id)
        assert state is not None
        if outcome["succeed"] == "ok":
            assert state.state == "succeeded"
            assert outcome["recover"]["interrupted"] == 0
        else:
            assert state.state == "queued"
            assert outcome["recover"]["requeued"] == 1
            main.cancel(task_id, "next round")
        check_invariants(db)


def test_concurrent_add_lease_start_succeed_has_no_lock_errors(tmp_path: Path) -> None:
    db = tmp_path / "lab.db"
    TaskQueue(db, owner="seed").close()
    workers, rounds = 4, 6
    barrier = threading.Barrier(workers)

    def worker(n: int) -> Callable[[], None]:
        def run() -> None:
            q = TaskQueue(db, owner=f"worker-{n}")
            barrier.wait(CHILD_TIMEOUT)
            for i in range(rounds):
                q.add_task(f"w{n}-{i}", idempotent=True)
                task = q.lease(ttl_seconds=3600)
                if task is not None and task.lease is not None:
                    q.start(task.lease)
                    q.succeed(task.lease, {"by": n})
        return run

    assert run_threads([worker(n) for n in range(workers)]) == []
    check_invariants(db)
    conn = sqlite3.connect(str(db))
    try:
        started = conn.execute(
            "SELECT task_id, COUNT(*) FROM events WHERE kind = 'running' GROUP BY task_id"
        ).fetchall()
        succeeded = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE state = 'succeeded'").fetchone()[0]
    finally:
        conn.close()
    assert all(n == 1 for _, n in started), "a task ran more than once"
    assert succeeded == len(started)


# ------------------------------------- recover() acting on a stale scan


@pytest.mark.safety
def test_recover_skips_a_task_that_finished_after_the_scan(tmp_path: Path) -> None:
    db = tmp_path / "lab.db"
    a = TaskQueue(db, owner="same")
    b = TaskQueue(db, owner="same")
    task_id, token = lease_started(a)

    with before_next_transaction(b, lambda: a.succeed(token)):
        assert b.recover() == {"interrupted": 0, "requeued": 0, "held_for_review": 0}

    task = a.get(task_id)
    assert task is not None and task.state == "succeeded"
    check_invariants(db)


@pytest.mark.safety
def test_recover_does_not_kill_a_lease_taken_after_the_scan(tmp_path: Path) -> None:
    db = tmp_path / "lab.db"
    a = TaskQueue(db, owner="first")
    slow, fast, other = (TaskQueue(db, owner="rec"), TaskQueue(db, owner="rec"),
                         TaskQueue(db, owner="other"))
    task_id, _ = lease_started(a)
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE leases SET expires_at = ? WHERE task_id = ?",
                 (_ts(_utcnow() - timedelta(seconds=10)), task_id))
    conn.commit()
    conn.close()
    fresh: list[LeaseToken] = []

    def reclaim_and_lease_again() -> None:
        assert fast.recover()["requeued"] == 1
        task = other.lease(ttl_seconds=3600)
        assert task is not None and task.lease is not None
        fresh.append(task.lease)

    with before_next_transaction(slow, reclaim_and_lease_again):
        assert slow.recover() == {"interrupted": 0, "requeued": 0, "held_for_review": 0}

    assert other.owns_lease(fresh[0]), "recover() released a lease it never scanned"
    task = a.get(task_id)
    assert task is not None and task.state == "leased"
    check_invariants(db)


# ------------------------------------------------- Hypothesis state machine


class QueueMachine(RuleBasedStateMachine):
    """Two queue instances on one database, driven at random."""

    def __init__(self) -> None:
        super().__init__()
        self._dir = tempfile.TemporaryDirectory()
        self.db = Path(self._dir.name) / "lab.db"
        self.queues = {"a": TaskQueue(self.db, owner="a"),
                       "b": TaskQueue(self.db, owner="b")}
        self.tasks: list[str] = []
        self.tokens: list[tuple[str, LeaseToken]] = []

    def teardown(self) -> None:
        for q in self.queues.values():
            q.close()
        self._dir.cleanup()

    def is_live(self, which: str, token: LeaseToken) -> bool:
        q = self.queues[which]
        row = q._conn.execute(
            "SELECT 1 FROM leases WHERE id = ? AND task_id = ? AND holder = ? "
            f"AND released_at IS NULL AND expires_at > {NOW_MS}",
            (token.lease_id, token.task_id, token.instance_id)).fetchone()
        return row is not None and token.instance_id == q._holder

    def pick(self, token_idx: int, as_other: bool) -> tuple[str, LeaseToken]:
        which, token = self.tokens[token_idx % len(self.tokens)]
        if as_other:
            which = "b" if which == "a" else "a"
        return which, token

    def act(self, token_idx: int, call: Callable[[TaskQueue, LeaseToken], object],
            *, as_other: bool = False) -> None:
        which, token = self.pick(token_idx, as_other)
        live = self.is_live(which, token)
        try:
            call(self.queues[which], token)
        except LeaseLost:
            assert not live, "LeaseLost raised for a live lease"
            return
        except TransitionError:
            assert live, "TransitionError before the lease was proven"
            return
        assert live, "an operation succeeded on a lease that was not live"

    @rule(idempotent=st.booleans(), max_attempts=st.integers(1, 3))
    def add_task(self, idempotent: bool, max_attempts: int) -> None:
        self.tasks.append(self.queues["a"].add_task(
            f"task {len(self.tasks)}", idempotent=idempotent, max_attempts=max_attempts))

    @rule(which=st.sampled_from(["a", "b"]))
    def lease(self, which: str) -> None:
        task = self.queues[which].lease(ttl_seconds=3600)
        if task is not None:
            assert task.lease is not None
            self.tokens.append((which, task.lease))

    @precondition(lambda self: self.tokens)
    @rule(idx=st.integers(0, 1000), other=st.booleans())
    def start(self, idx: int, other: bool) -> None:
        self.act(idx, lambda q, t: q.start(t), as_other=other)

    @precondition(lambda self: self.tokens)
    @rule(idx=st.integers(0, 1000), other=st.booleans())
    def succeed(self, idx: int, other: bool) -> None:
        self.act(idx, lambda q, t: q.succeed(t, {"ok": True}), as_other=other)

    @precondition(lambda self: self.tokens)
    @rule(idx=st.integers(0, 1000), retry=st.booleans())
    def fail(self, idx: int, retry: bool) -> None:
        self.act(idx, lambda q, t: q.fail(t, "boom", timedelta(0), retry=retry))

    @precondition(lambda self: self.tokens)
    @rule(idx=st.integers(0, 1000), other=st.booleans())
    def park(self, idx: int, other: bool) -> None:
        self.act(idx, lambda q, t: q.park_for_approval(t, "human"), as_other=other)

    @precondition(lambda self: self.tokens)
    @rule(idx=st.integers(0, 1000))
    def hold(self, idx: int) -> None:
        self.act(idx, lambda q, t: q.hold_for_review(t, "unknown outcome"))

    @precondition(lambda self: self.tokens)
    @rule(idx=st.integers(0, 1000), other=st.booleans())
    def release_unstarted(self, idx: int, other: bool) -> None:
        self.act(idx, lambda q, t: q.release_unstarted(t, "fault", timedelta(0)),
                 as_other=other)

    @precondition(lambda self: self.tokens)
    @rule(idx=st.integers(0, 1000), other=st.booleans())
    def renew(self, idx: int, other: bool) -> None:
        which, token = self.pick(idx, other)
        live = self.is_live(which, token)
        assert self.queues[which].renew_lease(token, 3600) == live

    @precondition(lambda self: self.tasks)
    @rule(idx=st.integers(0, 1000))
    def expire(self, idx: int) -> None:
        task_id = self.tasks[idx % len(self.tasks)]
        self.queues["a"]._conn.execute(
            "UPDATE leases SET expires_at = ? WHERE task_id = ? AND released_at IS NULL",
            (_ts(_utcnow() - timedelta(seconds=10)), task_id))

    @rule(which=st.sampled_from(["a", "b"]))
    def recover(self, which: str) -> None:
        self.queues[which].recover()

    @precondition(lambda self: self.tasks)
    @rule(idx=st.integers(0, 1000))
    def cancel(self, idx: int) -> None:
        with contextlib.suppress(TransitionError):
            self.queues["b"].cancel(self.tasks[idx % len(self.tasks)])

    @precondition(lambda self: self.tasks)
    @rule(idx=st.integers(0, 1000))
    def requeue_held(self, idx: int) -> None:
        self.queues["a"].requeue_held(self.tasks[idx % len(self.tasks)])

    @precondition(lambda self: self.tasks)
    @rule(idx=st.integers(0, 1000))
    def resume(self, idx: int) -> None:
        task_id = self.tasks[idx % len(self.tasks)]
        task = self.queues["a"].get(task_id)
        assert task is not None
        if task.state == "awaiting_approval":
            self.queues["a"].resume_after_approval(task_id)

    @invariant()
    def database_is_consistent(self) -> None:
        check_invariants(self.db)


QueueMachine.TestCase.settings = settings(
    max_examples=25, stateful_step_count=25, deadline=None, derandomize=True)
TestQueueMachine = pytest.mark.safety(QueueMachine.TestCase)


# ------------------------------------------------ real-process crash tests


def start_child(*args: str) -> subprocess.Popen[str]:
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    return subprocess.Popen(
        [sys.executable, str(CHILD), *args], cwd=str(ROOT), env=env, text=True,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def arm_and_kill(db: Path, operation: str, kill_point: str, *extra: str) -> None:
    proc = start_child("op", str(db), operation, kill_point, *extra)
    watchdog = threading.Timer(CHILD_TIMEOUT, proc.kill)
    watchdog.start()
    try:
        assert proc.stdout is not None and proc.stdin is not None
        assert proc.stderr is not None
        line = proc.stdout.readline().strip()
        assert line == "armed", f"child said {line!r}: {proc.stderr.read()}"
        before = snapshot(db)
        proc.stdin.write("go\n")
        proc.stdin.flush()
        code = proc.wait()
        assert code == -signal.SIGKILL, (
            f"{operation}@{kill_point}: exit {code}, "
            f"stdout {proc.stdout.read()!r}, stderr {proc.stderr.read()[-500:]}")
    finally:
        watchdog.cancel()
        if proc.poll() is None:
            proc.kill()
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
    assert snapshot(db) == before, (
        f"{operation}@{kill_point}: a killed transaction left partial writes")
    check_invariants(db)


CRASH_POINTS = [
    (operation, point)
    for operation in ("succeed", "fail", "park_for_approval", "hold_for_review",
                      "release_unstarted", "cancel", "recover")
    for point in ("record", "release_lease")
] + [("resume_after_approval", "record")]

# What a same-owner recover() does with the killed task afterwards.
RECOVERED = {"interrupted": 1, "requeued": 1, "held_for_review": 0}
NOTHING = {"interrupted": 0, "requeued": 0, "held_for_review": 0}


@pytest.mark.safety
@pytest.mark.parametrize(("operation", "kill_point"), CRASH_POINTS)
def test_kill_mid_transition_rolls_the_whole_operation_back(
        tmp_path: Path, operation: str, kill_point: str) -> None:
    db = tmp_path / "lab.db"
    arm_and_kill(db, operation, kill_point)
    task_id = single_task_id(db)

    q = TaskQueue(db, owner="somebody-else")
    assert q.recover() == NOTHING, "another owner reclaimed a live lease"

    restarted = TaskQueue(db, owner="crash-child")
    expected = NOTHING if operation == "resume_after_approval" else RECOVERED
    assert restarted.recover() == expected
    task = restarted.get(task_id)
    assert task is not None
    if operation == "resume_after_approval":
        assert task.state == "awaiting_approval"
    else:
        assert task.state == "queued"
    check_invariants(db)


@pytest.mark.safety
def test_kill_mid_finish_of_a_non_idempotent_task_holds_it_for_review(tmp_path: Path) -> None:
    db = tmp_path / "lab.db"
    arm_and_kill(db, "succeed", "record", "non-idempotent")
    task_id = single_task_id(db)

    restarted = TaskQueue(db, owner="crash-child")
    assert restarted.recover() == {"interrupted": 1, "requeued": 0, "held_for_review": 1}
    task = restarted.get(task_id)
    assert task is not None and task.state == "interrupted"
    check_invariants(db)


def test_repeated_kills_under_random_load_leave_a_consistent_database(tmp_path: Path) -> None:
    db = tmp_path / "lab.db"
    for round_no in range(6):
        proc = start_child("churn", str(db), str(round_no))
        watchdog = threading.Timer(CHILD_TIMEOUT, proc.kill)
        watchdog.start()
        try:
            assert proc.stdout is not None and proc.stderr is not None
            assert proc.stdout.readline().strip() == "ready", proc.stderr.read()
            time.sleep(random.Random(round_no).uniform(0.05, 0.4))
            proc.kill()
            proc.wait()
        finally:
            watchdog.cancel()
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()
        assert proc.returncode == -signal.SIGKILL
        check_invariants(db)
        TaskQueue(db, owner="crash-child").recover()
        check_invariants(db)
        conn = sqlite3.connect(str(db))
        live = conn.execute(
            "SELECT COUNT(*) FROM leases WHERE released_at IS NULL").fetchone()[0]
        conn.close()
        assert live == 0, "a killed supervisor's own leases survived its restart"
