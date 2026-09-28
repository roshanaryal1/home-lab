"""R01 to R11: the eleven reproductions from the 2026-09-28 deep review.

Improvement plan item 2.4 (#61). Each test checks the real effect the
review observed (a file deleted, a lease left live, a canary overwritten),
not a log line or a return value standing in for it.

A reproduction whose fix has not landed yet is marked
``xfail(strict=True)`` with its issue number. Strict means that the day
the fix lands, the unexpected pass fails the build until the marker is
removed, so this file cannot quietly go stale in either direction.
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pytest

from lab import sandbox
from lab.broker import (
    ApprovalRequired,
    ExecutionBroker,
    ExecutionContext,
    ToolSession,
    Workspace,
)
from lab.journal import OperationJournal
from lab.policy import PolicyEngine, action_hash
from lab.queue import LeaseLost, Task, TaskQueue
from lab.supervisor import Supervisor, SupervisorConfig


@pytest.fixture()
def q(tmp_path: Path) -> TaskQueue:
    with TaskQueue(tmp_path / "lab.db", owner="sup") as queue:
        yield queue


def _broker(tmp_path: Path, q: TaskQueue,
            *task_ids: str) -> tuple[ExecutionBroker, dict[str, ExecutionContext]]:
    """A broker plus a live, leased context per named task."""
    for task_id in task_ids:
        q._conn.execute("INSERT INTO tasks (id, title) VALUES (?, ?)", (task_id, task_id))
    contexts = {}
    while (task := q.lease()) is not None:
        contexts[task.id] = ExecutionContext(task.id, "test", task.attempts, task.lease)
    broker = ExecutionBroker(tmp_path / "ws", policy=PolicyEngine(q._conn),
                             leases=q.owns_lease, journal=OperationJournal(q._conn))
    return broker, contexts


def _expire(q: TaskQueue, task_id: str) -> None:
    q._conn.execute(
        "UPDATE leases SET expires_at = datetime('now', '-1 second') "
        "WHERE task_id = ? AND released_at IS NULL", (task_id,))


def _live_leases(q: TaskQueue, task_id: str) -> int:
    return q._conn.execute(
        "SELECT COUNT(*) FROM leases WHERE task_id = ? AND released_at IS NULL",
        (task_id,)).fetchone()[0]


def test_r01_delete_needs_approval_and_policy_is_asked(tmp_path, q) -> None:
    """R01: fs.delete ran with no approval; the policy was never called."""
    broker, ctx = _broker(tmp_path, q, "t1")
    ws = broker.open_workspace("t1", {"fs.write", "fs.delete"})
    tools = broker.session(ctx["t1"])
    tools.submit("fs.write", path="f", content="x")
    with pytest.raises(ApprovalRequired):
        tools.submit("fs.delete", path="f")
    assert (ws.root / "f").exists()


def test_r02_one_task_cannot_read_another_tasks_workspace(tmp_path, q) -> None:
    """R02: a different worker label read another task's workspace.

    A session is bound to its own context, so t1's handler has no way to
    name t2. Forging a context that names t2 with t1's lease is refused.
    In-process this is an API boundary; the worker process (1.2b) and the
    lab account (4.5) make it an OS one.
    """
    broker, ctx = _broker(tmp_path, q, "t1", "t2")
    broker.open_workspace("t1", {"fs.read"})
    ws2 = broker.open_workspace("t2", {"fs.read"})
    (ws2.root / "secret.txt").write_text("t2 only")

    mine = broker.session(ctx["t1"]).submit("fs.read", path="secret.txt")
    assert not mine.ok and "t2 only" not in str(mine.detail)

    forged = ExecutionContext("t2", "test", 1, ctx["t1"].lease)
    stolen = broker.session(forged).submit("fs.read", path="secret.txt")
    assert not stolen.ok and "ContextRevoked" in stolen.error


def test_r03_a_never_owner_cannot_commit(tmp_path) -> None:
    """R03: an owner that never leased a task marked it succeeded."""
    db = tmp_path / "lab.db"
    with TaskQueue(db, owner="shared") as a, TaskQueue(db, owner="shared") as b:
        task_id = a.add_task("work")
        tok = a.lease().lease
        a.start(tok)
        with pytest.raises(LeaseLost):
            b.succeed(tok, {"forged": True})
        assert a.get(task_id).state == "running"


def test_r04_stale_process_cannot_finish_or_release_the_replacement(tmp_path) -> None:
    """R04: a stale process with the same owner completed work after
    recovery and released the replacement's lease."""
    db = tmp_path / "lab.db"
    with TaskQueue(db, owner="shared") as stale, TaskQueue(db, owner="shared") as fresh:
        task_id = stale.add_task("work", idempotent=True)
        old = stale.lease().lease
        stale.start(old)
        fresh.recover()
        new = fresh.lease().lease
        with pytest.raises(LeaseLost):
            stale.succeed(old, {"late": True})
        assert fresh.get(task_id).state == "leased"
        assert fresh.owns_lease(new)


def test_r05_failed_audit_insert_changes_nothing(q) -> None:
    """R05: a failed audit insert left the task succeeded, with no event
    and a live lease."""
    task_id = q.add_task("work")
    tok = q.lease().lease
    q.start(tok)
    q._conn.execute(
        "CREATE TRIGGER no_success_event BEFORE INSERT ON events "
        "WHEN NEW.kind = 'succeeded' BEGIN SELECT RAISE(ABORT, 'disk full'); END")
    with pytest.raises(Exception, match="disk full"):
        q.succeed(tok, {"ok": True})
    assert q.get(task_id).state == "running"
    assert _live_leases(q, task_id) == 1


def test_r06_unsafe_task_is_not_requeued_after_ordinary_failure(q) -> None:
    """R06: a task marked unsafe to repeat was requeued after a failure."""
    task_id = q.add_task("send email", idempotent=False, max_attempts=3)
    tok = q.lease().lease
    q.start(tok)
    q.fail(tok, "timeout", retry_in=timedelta(0))
    assert q.get(task_id).state == "failed"
    assert q.lease() is None


@pytest.mark.asyncio
async def test_r07_policy_error_does_not_strand_a_leased_task(tmp_path, monkeypatch) -> None:
    """R07: a policy error killed a worker, run() returned normally, and
    the task stayed leased."""
    sup = Supervisor(SupervisorConfig(db_path=tmp_path / "lab.db", idle_poll_seconds=0.01))

    async def handler(task: Task, tools: ToolSession) -> dict:
        return {}

    def broken(task: Task):
        raise RuntimeError("policy store unreachable")

    sup.register("x", handler)
    monkeypatch.setattr(sup.policy, "authorize", broken)
    task_id = sup.queue.add_task("work", agent_kind="x")
    await sup.run(max_tasks=1)
    try:
        assert sup.queue.get(task_id).state != "leased"
        assert _live_leases(sup.queue, task_id) == 0
        assert sup.stats.worker_errors == 1
    finally:
        sup.close()


def test_r08_an_approval_expired_before_use_is_not_spent(q) -> None:
    """R08: an approval that expired between lookup and use was spent.

    Consumption is one UPDATE that re-checks expiry and the intent hash
    at the moment of use (item 1.4), so the late consumer gets nothing.
    """
    task_id = q.add_task("delete", capability_tier="approve")
    policy = PolicyEngine(q._conn)
    task = q.get(task_id)
    approval_id = policy.request_approval(task, "needs a human")
    policy.grant(approval_id, decided_by="operator", valid_for=timedelta(minutes=5))
    # Lookup succeeded a moment ago; the window closes before consumption.
    q._conn.execute("UPDATE approvals SET expires_at = datetime('now', '-1 second') "
                    "WHERE id = ?", (approval_id,))
    assert policy._consume(approval_id, action_hash(task)) is False
    consumed = q._conn.execute("SELECT consumed_at FROM approvals WHERE id = ?",
                               (approval_id,)).fetchone()[0]
    assert consumed is None


def test_r09_directory_swapped_for_symlink_after_check(tmp_path, q, monkeypatch) -> None:
    """R09: a directory swapped for a symlink after the path check sent a
    write outside the workspace."""
    broker, ctx = _broker(tmp_path, q, "t1")
    ws = broker.open_workspace("t1", {"fs.write"})
    (ws.root / "sub").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    real_resolve = Workspace.resolve

    def racing_resolve(self: Workspace, relative: str) -> Path:
        checked = real_resolve(self, relative)
        # The attacker wins the window between check and use.
        (self.root / "sub").rmdir()
        (self.root / "sub").symlink_to(outside)
        return checked

    monkeypatch.setattr(Workspace, "resolve", racing_resolve)
    broker.session(ctx["t1"]).submit("fs.write", path="sub/x.txt", content="pwn")
    assert not (outside / "x.txt").exists()


def _fake_sandbox(monkeypatch) -> dict:
    """Stand in for sandbox-exec so the host-side behaviour is testable on
    any platform; the kernel sandbox is not what R10 and R11 are about."""
    seen: dict = {}

    def fake_execute(cmd, **kwargs):
        seen["cmd"], seen["kwargs"] = cmd, kwargs
        return sandbox._Execution(0, b"", b"", False, False)

    monkeypatch.setattr(sandbox, "available", lambda: True)
    monkeypatch.setattr(sandbox, "_execute", fake_execute)
    return seen


def test_r10_planted_profile_link_does_not_overwrite_outside_file(tmp_path, monkeypatch) -> None:
    """R10: a planted .sandbox.sb symlink made the supervisor overwrite a
    file outside the workspace before any command started."""
    _fake_sandbox(monkeypatch)
    ws = tmp_path / "ws"
    ws.mkdir()
    canary = tmp_path / "canary.txt"
    canary.write_text("original")
    (ws / ".sandbox.sb").symlink_to(canary)
    sandbox.run(["/bin/echo", "hi"], ws)
    assert canary.read_text() == "original"


def test_r11_supervisor_environment_does_not_reach_commands(tmp_path, monkeypatch) -> None:
    """R11: sandboxed commands received the supervisor's full environment."""
    seen = _fake_sandbox(monkeypatch)
    monkeypatch.setenv("LAB_TEST_TOKEN", "must-not-leak")
    ws = tmp_path / "ws"
    ws.mkdir()
    sandbox.run(["/usr/bin/env"], ws)
    env = seen["kwargs"].get("env")
    assert env is not None, "no explicit environment: the child inherits everything"
    assert "LAB_TEST_TOKEN" not in env
    assert os.environ["LAB_TEST_TOKEN"] == "must-not-leak"
