"""Stop the work, not only its result (item 1.8, #52).

Done when: after an injected policy error the worker recovers and the
task is released; after lease loss or a stop, no later tool call
succeeds and no child process survives. That holds for worker processes, which the
stop kills, and, since #228, for a `shell.run` command that is already running (a
cancel flag ends it; tests/test_shell_cancel.py). It does not hold for a process that
leaves the command's process group with setsid() (#223).
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from lab import sandbox
from lab.broker import ToolSession
from lab.queue import Task
from lab.supervisor import Supervisor, SupervisorConfig


def _sup(tmp_path: Path, **kw) -> Supervisor:
    return Supervisor(SupervisorConfig(db_path=tmp_path / "lab.db",
                                       idle_poll_seconds=0.01, **kw))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _pid_from_workspace(sup: Supervisor, task_id: str) -> int:
    for _ in range(200):
        ws = sup.broker._workspaces.get(task_id)
        if ws is not None and (ws.root / "pid").exists():
            # The worker creates the file before it writes the pid (#198): an
            # empty read means "not yet", not a failure.
            text = (ws.root / "pid").read_text().strip()
            if text.isdigit():
                return int(text)
        await asyncio.sleep(0.02)
    raise AssertionError("worker never reported its pid")


async def _wait_dead(pid: int, seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        await asyncio.sleep(0.02)
    return False


@pytest.mark.asyncio
async def test_a_worker_recovers_after_an_injected_policy_error(tmp_path, monkeypatch) -> None:
    sup = _sup(tmp_path, light_slots=1)
    real = sup.policy.authorize
    calls = {"n": 0}

    def flaky(task):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("policy store unreachable")
        return real(task)

    async def ok(task: Task, tools: ToolSession) -> dict:
        return {"ok": True}

    monkeypatch.setattr(sup.policy, "authorize", flaky)
    sup.register("x", ok)
    first = sup.queue.add_task("first", agent_kind="x", priority=1)
    second = sup.queue.add_task("second", agent_kind="x", priority=2)
    await sup.run(max_tasks=2)
    assert sup.queue.get(first).state == "queued", "released, nothing ran"
    assert sup.queue.get(second).state == "succeeded", "the slot kept working"
    assert sup.stats.worker_errors == 1 and sup.healthy
    kinds = [r["kind"] for r in sup.queue.events(first)]
    assert "worker_error" in kinds
    sup.close()


@pytest.mark.asyncio
async def test_repeated_errors_stop_the_slot_and_report_unhealthy(tmp_path, monkeypatch) -> None:
    sup = _sup(tmp_path, light_slots=1, max_consecutive_worker_errors=3)

    def broken(task):
        raise RuntimeError("policy store unreachable")

    monkeypatch.setattr(sup.policy, "authorize", broken)
    sup.register("x", lambda task, tools: None)
    for i in range(5):
        sup.queue.add_task(f"t{i}", agent_kind="x", max_attempts=1)
    await sup.run(max_tasks=5)
    assert not sup.healthy and "3 consecutive errors" in sup.unhealthy_reason
    assert sup.stats.worker_errors == 3
    assert sup.queue.counts().get("leased", 0) == 0, "nothing stranded"
    sup.close()


@pytest.mark.asyncio
async def test_lease_loss_ends_the_worker_process(tmp_path, monkeypatch) -> None:
    # The lease is lost only when renew_lease is patched below. A 0.3 s lease
    # could also expire on its own on a slow CI runner before the worker's pid
    # was checked, which failed the alive assertion for the wrong reason.
    sup = _sup(tmp_path, lease_ttl_seconds=1.5)
    sup.register_reviewed("sleep", "lab.handlers.demo:record_pid_then_sleep",
                          tools={"fs.write"})
    task_id = sup.queue.add_task("sleep", agent_kind="sleep", payload={"seconds": 60})

    run = asyncio.create_task(sup.run(max_tasks=1))
    pid = await _pid_from_workspace(sup, task_id)
    assert _alive(pid)
    monkeypatch.setattr(sup.queue, "renew_lease", lambda *a, **k: False)
    await asyncio.wait_for(run, timeout=10)

    assert await _wait_dead(pid), "the worker outlived its lease"
    assert sup.stats.lease_losses == 1
    assert sup.queue.get(task_id).state == "running", "no result written"
    kinds = [r["kind"] for r in sup.queue.events(task_id)]
    assert "stopped_on_lease_loss" in kinds and "succeeded" not in kinds
    sup.close()


@pytest.mark.asyncio
async def test_emergency_stop_revokes_then_kills(tmp_path) -> None:
    sup = _sup(tmp_path, stop_grace_seconds=0.3)
    sup.register_reviewed("sleep", "lab.handlers.demo:record_pid_then_sleep",
                          tools={"fs.write"})
    seen: list = []

    async def poller(task: Task, tools: ToolSession) -> dict:
        while True:
            seen.append(tools.submit("fs.list").ok)
            await asyncio.sleep(0.02)

    sup.register("poll", poller, tools={"fs.list"})
    sleeper = sup.queue.add_task("sleep", agent_kind="sleep", payload={"seconds": 60})
    sup.queue.add_task("poll", agent_kind="poll")

    run = asyncio.create_task(sup.run(max_tasks=2))
    pid = await _pid_from_workspace(sup, sleeper)
    while not seen:
        await asyncio.sleep(0.02)
    start = time.monotonic()
    await sup.emergency_stop()
    stopped_at = len(seen)
    await asyncio.wait_for(run, timeout=10)

    assert time.monotonic() - start < 5
    assert await _wait_dead(pid), "a worker process survived the stop"
    assert seen[stopped_at:] == [False] * len(seen[stopped_at:]), \
        "a tool call succeeded after authority was revoked"
    assert sup.stats.stopped == 2
    sup.close()


@pytest.mark.asyncio
async def test_a_long_tool_call_does_not_starve_the_heartbeat(tmp_path, monkeypatch) -> None:
    """Worker-process tool calls run off the event loop."""
    def slow_run(argv, workspace, **kw):
        time.sleep(1.0)
        return sandbox.SandboxResult(ok=True, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox, "run", slow_run)
    sup = _sup(tmp_path)
    task_id = sup.queue.add_task("t")

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.05)

    from lab.broker import ExecutionContext
    from lab.queue import LeaseToken
    sup.broker.open_workspace(task_id, {"shell.run"})
    token = LeaseToken(task_id, "l", 1, sup.queue._holder)
    ctx = ExecutionContext(task_id, "t", 1, token)
    monkeypatch.setattr(sup.broker, "_check_context", lambda ctx: None)
    monkeypatch.setattr(sup.broker, "_authorize", lambda request, ws: None)
    t = asyncio.create_task(ticker())
    result = await sup.broker.session(ctx).submit_async("shell.run", argv=["/bin/true"])
    t.cancel()
    assert result.ok
    assert ticks >= 10, f"event loop blocked during the call ({ticks} ticks)"
    sup.close()


@pytest.mark.asyncio
async def test_pid_helper_waits_out_an_empty_pid_file(tmp_path) -> None:
    # #198: the worker creates the pid file empty and writes it a moment later;
    # the helper used to crash with int('') when it read in between.
    from types import SimpleNamespace
    (tmp_path / "pid").write_text("")
    sup = SimpleNamespace(broker=SimpleNamespace(
        _workspaces={"t": SimpleNamespace(root=tmp_path)}))

    async def write_later() -> None:
        await asyncio.sleep(0.1)
        (tmp_path / "pid").write_text("4242")

    writer = asyncio.create_task(write_later())
    assert await _pid_from_workspace(sup, "t") == 4242
    await writer


@pytest.mark.asyncio
async def test_pid_helper_still_fails_when_no_pid_ever_arrives(tmp_path) -> None:
    from types import SimpleNamespace
    (tmp_path / "pid").write_text("")
    sup = SimpleNamespace(broker=SimpleNamespace(
        _workspaces={"t": SimpleNamespace(root=tmp_path)}))
    with pytest.raises(AssertionError, match="never reported its pid"):
        await _pid_from_workspace(sup, "t")
