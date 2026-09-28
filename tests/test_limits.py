"""Clean environment and strict limits for every request (item 1.10, #50).

Done when: no supervisor variable reaches a command, an output flood or
a spawning command cannot freeze the controller, and malformed arguments
are refused. The executor tests run on any POSIX host; the sandboxed
ones need macOS Seatbelt.
"""

from __future__ import annotations

import os
import time

import pytest

from lab import broker as broker_mod
from lab import queue as queue_mod
from lab import sandbox
from lab.broker import ExecutionBroker, ExecutionContext, ToolSession
from lab.journal import OperationJournal
from lab.policy import PolicyEngine
from lab.queue import ChildLimitExceeded, Task, TaskQueue
from lab.supervisor import Supervisor, SupervisorConfig

needs_sandbox = pytest.mark.skipif(not sandbox.available(),
                                   reason="real confinement needs macOS Seatbelt")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_dead(pids: list[int], seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not any(_alive(p) for p in pids):
            return True
        time.sleep(0.05)
    return False


# ------------------------------------------------------------ executor


def test_an_output_flood_is_capped_and_does_not_hang(tmp_path) -> None:
    start = time.monotonic()
    done = sandbox._execute(["/bin/sh", "-c", "yes flood | head -c 20000000"],
                            cwd=str(tmp_path), env={"PATH": "/usr/bin:/bin"},
                            timeout=20, cap=4096)
    assert done.truncated and len(done.stdout) == 4096
    assert time.monotonic() - start < 15


def test_the_deadline_kills_the_whole_process_group(tmp_path) -> None:
    start = time.monotonic()
    done = sandbox._execute(
        ["/bin/sh", "-c", "sleep 60 & echo $!; sleep 60 & echo $!; wait"],
        cwd=str(tmp_path), env={"PATH": "/usr/bin:/bin"}, timeout=0.5, cap=4096)
    assert done.timed_out and time.monotonic() - start < 5
    pids = [int(x) for x in done.stdout.split()]
    assert len(pids) == 2 and _wait_dead(pids)


def test_background_children_do_not_outlive_the_call(tmp_path) -> None:
    done = sandbox._execute(["/bin/sh", "-c", "sleep 60 >/dev/null 2>&1 & echo $!"],
                            cwd=str(tmp_path), env={"PATH": "/usr/bin:/bin"},
                            timeout=10, cap=4096)
    assert not done.timed_out
    assert _wait_dead([int(done.stdout.strip())])


def test_requested_timeouts_are_clamped(tmp_path, monkeypatch) -> None:
    seen = {}

    def fake(cmd, **kw):
        seen.update(kw)
        return sandbox._Execution(0, b"", b"", False, False)

    monkeypatch.setattr(sandbox, "available", lambda: True)
    monkeypatch.setattr(sandbox, "_execute", fake)
    sandbox.run(["/bin/true"], tmp_path, timeout=10**9)
    assert seen["timeout"] == sandbox.MAX_TIMEOUT_SECONDS
    assert seen["cap"] == sandbox.MAX_OUTPUT_BYTES
    assert seen["env"] == sandbox.command_environment(tmp_path.resolve())


@needs_sandbox
def test_a_sandboxed_command_sees_only_the_minimal_environment(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LAB_TEST_TOKEN", "must-not-leak")
    result = sandbox.run(["/usr/bin/env"], tmp_path)
    assert result.ok
    names = {line.split("=", 1)[0] for line in result.stdout.splitlines() if "=" in line}
    assert "LAB_TEST_TOKEN" not in names
    assert names <= {"PATH", "HOME", "TMPDIR", "LANG", "__CF_USER_TEXT_ENCODING"}


# -------------------------------------------------------------- broker


@pytest.fixture()
def setup(tmp_path):
    with TaskQueue(tmp_path / "lab.db") as q:
        q._conn.execute("INSERT INTO tasks (id, title) VALUES ('t1', 't1')")
        task = q.lease()
        ctx = ExecutionContext(task.id, "test", task.attempts, task.lease)
        b = ExecutionBroker(tmp_path / "ws", policy=PolicyEngine(q._conn),
                            leases=q.owns_lease, journal=OperationJournal(q._conn))
        yield q, b, b.session(ctx)


@pytest.mark.parametrize(("tool", "params", "why"), [
    ("fs.read", {"path": "a", "follow": True}, "unknown parameters"),
    ("fs.write", {"path": "a"}, "missing parameter"),
    ("fs.write", {"path": "a", "content": 42}, "invalid value"),
    ("shell.run", {"argv": ["/bin/echo", 1]}, "invalid value"),
    ("shell.run", {"argv": []}, "invalid value"),
    ("shell.run", {"argv": ["/bin/echo"], "timeout": float("inf")}, "invalid value"),
    ("shell.run", {"argv": ["/bin/echo"], "timeout": float("nan")}, "invalid value"),
    ("shell.run", {"argv": ["/bin/echo"], "timeout": -1}, "invalid value"),
    ("shell.run", {"argv": ["/bin/echo"], "timeout": True}, "invalid value"),
    ("shell.run", {"argv": ["/bin/echo"], "env": {"X": "1"}}, "unknown parameters"),
])
def test_malformed_calls_are_refused_before_policy(setup, tool, params, why) -> None:
    q, b, tools = setup
    b.open_workspace("t1", {"fs.read", "fs.write", "shell.run"})
    result = tools.submit(tool, **params)
    assert not result.ok and "InvalidParams" in result.error and why in result.error
    assert q._conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0


def test_reads_are_capped(setup, monkeypatch) -> None:
    _q, b, tools = setup
    monkeypatch.setattr(broker_mod, "MAX_READ_BYTES", 10)
    b.open_workspace("t1", {"fs.write", "fs.read"})
    tools.submit("fs.write", path="big", content="x" * 11)
    result = tools.submit("fs.read", path="big")
    assert not result.ok and "QuotaExceeded" in result.error


def test_listings_are_capped(setup, monkeypatch) -> None:
    _q, b, tools = setup
    monkeypatch.setattr(broker_mod, "MAX_LIST_ENTRIES", 2)
    b.open_workspace("t1", {"fs.write", "fs.list"})
    for name in "abc":
        tools.submit("fs.write", path=name, content="x")
    result = tools.submit("fs.list")
    assert result.detail == {"entries": ["a", "b"], "truncated": True}


def test_tool_calls_per_task_are_capped(setup, monkeypatch) -> None:
    _q, b, tools = setup
    monkeypatch.setattr(broker_mod, "MAX_CALLS_PER_TASK", 3)
    b.open_workspace("t1", {"fs.list"})
    results = [tools.submit("fs.list") for _ in range(4)]
    assert [r.ok for r in results] == [True, True, True, False]
    assert "QuotaExceeded" in results[-1].error


def test_child_tasks_per_task_are_capped(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(queue_mod, "MAX_CHILDREN_PER_TASK", 2)
    with TaskQueue(tmp_path / "lab.db") as q:
        parent = q.add_task("parent")
        q.add_task("c1", parent_id=parent)
        q.add_task("c2", parent_id=parent)
        with pytest.raises(ChildLimitExceeded):
            q.add_task("c3", parent_id=parent)
        assert q.counts() == {"queued": 3}


# ---------------------------------------------------------- wall clock


def _sup(tmp_path, **kw) -> Supervisor:
    return Supervisor(SupervisorConfig(db_path=tmp_path / "lab.db",
                                       idle_poll_seconds=0.01, **kw))


@pytest.mark.asyncio
async def test_a_task_past_its_wall_clock_ceiling_is_failed(tmp_path) -> None:
    sup = _sup(tmp_path, task_timeout_seconds=0.3)
    sup.register_reviewed("sleep", "lab.handlers.demo:sleep_for")
    task_id = sup.queue.add_task("sleep", agent_kind="sleep", payload={"seconds": 30})
    start = time.monotonic()
    await sup.run(max_tasks=1)
    assert time.monotonic() - start < 10
    task = sup.queue.get(task_id)
    assert task.state == "failed" and "wall-clock ceiling" in task.last_error
    sup.close()


@pytest.mark.asyncio
async def test_a_handlers_own_timeout_is_an_ordinary_failure(tmp_path) -> None:
    sup = _sup(tmp_path, task_timeout_seconds=30)

    async def impatient(task: Task, tools: ToolSession) -> dict:
        raise TimeoutError("upstream API timed out")

    sup.register("x", impatient)
    task_id = sup.queue.add_task("x", agent_kind="x")
    await sup.run(max_tasks=1)
    task = sup.queue.get(task_id)
    assert task.state == "failed" and "upstream API timed out" in task.last_error
    sup.close()
