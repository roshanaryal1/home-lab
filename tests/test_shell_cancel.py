"""A stop reaches a shell command that is already running (#228).

The broker runs ``shell.run`` in a thread, and a thread cannot be cancelled from
outside, so cancelling the coroutine that awaits it used to leave the command running
until its timeout. The way in is now a cancel flag the run loop checks.
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import threading
import time
from pathlib import Path

import pytest

from lab import sandbox
from lab.broker import ApprovalRequired, ExecutionBroker, ExecutionContext, ToolRequest
from lab.journal import OperationJournal
from lab.policy import PolicyEngine
from lab.queue import TaskQueue
from lab.supervisor import Supervisor, SupervisorConfig

needs_sandbox = pytest.mark.skipif(not sandbox.available(),
                                   reason="no OS-level sandbox on this machine")
ENV = {"PATH": "/usr/bin:/bin"}


def _running(tag: str) -> int:
    out = subprocess.run(["pgrep", "-f", f"sleep {tag}"], capture_output=True, text=True)
    return len([p for p in out.stdout.split() if p])


def _wait_for(tag: str, present: bool, seconds: float = 5.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if (_running(tag) > 0) == present:
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------- the run loop itself


def test_the_cancel_flag_ends_a_running_command_within_a_second(tmp_path: Path) -> None:
    cancel = threading.Event()
    threading.Timer(0.4, cancel.set).start()
    started = time.monotonic()
    done = sandbox._execute(["/bin/sleep", "30.401"], cwd=str(tmp_path), env=ENV,
                            timeout=25, cap=1000, cancel=cancel)
    assert done.cancelled and not done.timed_out
    assert time.monotonic() - started < 3.0
    assert _wait_for("30.401", present=False), "the command was left running"


def test_a_flag_that_is_already_set_stops_the_command_at_once(tmp_path: Path) -> None:
    cancel = threading.Event()
    cancel.set()
    started = time.monotonic()
    done = sandbox._execute(["/bin/sleep", "30.402"], cwd=str(tmp_path), env=ENV,
                            timeout=25, cap=1000, cancel=cancel)
    assert done.cancelled and time.monotonic() - started < 3.0
    assert _wait_for("30.402", present=False)


def test_without_a_flag_a_command_runs_as_before(tmp_path: Path) -> None:
    done = sandbox._execute(["/bin/sh", "-c", "echo hi"], cwd=str(tmp_path), env=ENV,
                            timeout=5, cap=1000)
    assert done.stdout.strip() == b"hi" and not done.cancelled and not done.timed_out
    unused = threading.Event()               # a flag that is never set changes nothing
    done = sandbox._execute(["/bin/sh", "-c", "echo hi"], cwd=str(tmp_path), env=ENV,
                            timeout=5, cap=1000, cancel=unused)
    assert done.stdout.strip() == b"hi" and not done.cancelled


def test_the_timeout_still_ends_a_command_when_a_flag_is_present(tmp_path: Path) -> None:
    done = sandbox._execute(["/bin/sleep", "30.403"], cwd=str(tmp_path), env=ENV,
                            timeout=0.6, cap=1000, cancel=threading.Event())
    assert done.timed_out and not done.cancelled
    assert _wait_for("30.403", present=False)


@needs_sandbox
def test_sandbox_run_reports_a_cancelled_command(tmp_path: Path) -> None:
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    result = sandbox.run(["/bin/sleep", "30.404"], tmp_path, timeout=25, cancel=cancel)
    assert result.cancelled and not result.ok and result.stderr == "cancelled"
    assert _wait_for("30.404", present=False)


# ------------------------------------------------- through the real broker


@pytest.fixture()
def broker(tmp_path: Path):
    with TaskQueue(tmp_path / "lab.db") as queue:
        for task_id in ("t1", "t2"):
            queue._conn.execute("INSERT INTO tasks (id, title) VALUES (?, ?)", (task_id, task_id))
        contexts = {}
        while (task := queue.lease()) is not None:
            contexts[task.id] = ExecutionContext(task.id, "test", task.attempts, task.lease)
        b = ExecutionBroker(workspace_root=tmp_path / "workspaces",
                            policy=PolicyEngine(queue._conn), leases=queue.owns_lease,
                            journal=OperationJournal(queue._conn))
        b.contexts = contexts  # type: ignore[attr-defined]
        yield b


def _approved_session(broker: ExecutionBroker, task_id: str, seconds: str):
    """What an approved handler needs: ask once, have a person grant that exact call."""
    session = broker.session(broker.contexts[task_id])  # type: ignore[attr-defined]
    params = {"argv": ["/bin/sleep", seconds], "timeout": 25}
    with pytest.raises(ApprovalRequired) as asked:
        session.submit("shell.run", **params)
    broker.policy.grant(asked.value.approval_id, decided_by="operator")  # type: ignore[union-attr]
    return session, params


async def _await_process(tag: str, present: bool, seconds: float = 5.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if (_running(tag) > 0) == present:
            return True
        await asyncio.sleep(0.05)
    return False


@needs_sandbox
@pytest.mark.asyncio
async def test_an_emergency_stop_ends_a_shell_command_that_is_already_running(broker) -> None:
    broker.open_workspace("t1", {"shell.run"})
    session, params = _approved_session(broker, "t1", "30.405")
    call = asyncio.ensure_future(session.submit_async("shell.run", **params))
    assert await _await_process("30.405", present=True), "the command never started"
    started = time.monotonic()
    broker.revoke()
    result = await asyncio.wait_for(call, timeout=5)
    assert time.monotonic() - started < 3.0
    assert not result.ok and result.detail["cancelled"] is True
    assert await _await_process("30.405", present=False), "the command outlived the stop"


@needs_sandbox
@pytest.mark.asyncio
async def test_cancelling_one_task_ends_its_command_and_leaves_another_tasks_alone(
        broker) -> None:
    broker.open_workspace("t1", {"shell.run"})
    broker.open_workspace("t2", {"shell.run"})
    s1, p1 = _approved_session(broker, "t1", "30.406")
    s2, p2 = _approved_session(broker, "t2", "30.407")
    first = asyncio.ensure_future(s1.submit_async("shell.run", **p1))
    second = asyncio.ensure_future(s2.submit_async("shell.run", **p2))
    assert await _await_process("30.406", present=True)
    assert await _await_process("30.407", present=True)
    broker.cancel_running("t1")
    result = await asyncio.wait_for(first, timeout=5)
    assert result.detail["cancelled"] is True
    assert await _await_process("30.406", present=False)
    assert _running("30.407") == 1, "the other task's command must not be touched"
    broker.cancel_running("t2")
    assert (await asyncio.wait_for(second, timeout=5)).detail["cancelled"] is True
    assert await _await_process("30.407", present=False)


@needs_sandbox
@pytest.mark.asyncio
async def test_cancelling_the_awaiting_coroutine_alone_used_to_leave_the_command_running(
        broker) -> None:
    """The bug itself (#228): what the supervisor's stop did before this change."""
    broker.open_workspace("t1", {"shell.run"})
    session, params = _approved_session(broker, "t1", "30.408")
    call = asyncio.ensure_future(session.submit_async("shell.run", **params))
    assert await _await_process("30.408", present=True)
    broker.cancel_running("t1")                     # the supervisor now does this first
    call.cancel()                                   # then cancels the wait, as before
    with contextlib.suppress(asyncio.CancelledError):
        await call
    assert await _await_process("30.408", present=False), "the command was left running"


@needs_sandbox
def test_a_command_that_starts_just_after_a_revoke_is_cancelled_at_once(broker) -> None:
    # The window between the permission check and the command registering: a revoke that
    # lands there must still reach it. Called directly, after the revoke, to hit it
    # deterministically.
    ws = broker.open_workspace("t1", {"shell.run"})
    broker.revoke()
    request = ToolRequest("shell.run", {"argv": ["/bin/sleep", "30.409"], "timeout": 25}, "t1")
    started = time.monotonic()
    result = broker._tool_shell_run(request, ws)
    assert result.detail["cancelled"] is True and time.monotonic() - started < 3.0
    assert _wait_for("30.409", present=False)


def test_the_broker_forgets_finished_commands(broker) -> None:
    broker.cancel_running("nothing-is-running")          # harmless when nothing runs
    assert broker._shell_cancels == {}
    broker.revoke()
    assert broker._shell_cancels == {}


# ------------------------------------------------- the supervisor calls it


def _sup(tmp_path: Path) -> Supervisor:
    return Supervisor(SupervisorConfig(db_path=tmp_path / "lab.db", idle_poll_seconds=0.01))


def test_interrupting_a_task_cancels_its_shell_commands(tmp_path: Path, monkeypatch) -> None:
    sup = _sup(tmp_path)
    seen: list[str] = []
    monkeypatch.setattr(sup.broker, "cancel_running", seen.append)
    sup._interrupt("some-task", "emergency_stop")
    assert seen == ["some-task"]
    sup.close()


@pytest.mark.asyncio
async def test_a_task_that_ends_cancels_its_shell_commands_whichever_way(
        tmp_path: Path, monkeypatch) -> None:
    sup = _sup(tmp_path)
    seen: list[str] = []
    monkeypatch.setattr(sup.broker, "cancel_running", seen.append)

    async def fine(task, tools):
        return {"ok": True}

    async def broken(task, tools):
        raise RuntimeError("boom")

    sup.register("fine", fine)
    sup.register("broken", broken)
    a = sup.queue.add_task("a", agent_kind="fine")
    b = sup.queue.add_task("b", agent_kind="broken", max_attempts=1)
    await sup.run(max_tasks=2)
    assert a in seen and b in seen
    sup.close()
