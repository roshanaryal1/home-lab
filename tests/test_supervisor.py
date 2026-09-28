"""Tests for the supervisor loop.

The load-bearing test here is the heavy-slot one: on a 32 GB machine a
second concurrent heavy model does not fit, so that limit has to be
enforced by the code rather than by convention.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from lab.broker import ToolSession
from lab.queue import Task, TaskQueue
from lab.supervisor import Supervisor, SupervisorConfig


def make_supervisor(tmp_path: Path, **kwargs) -> Supervisor:
    config = SupervisorConfig(db_path=tmp_path / "lab.db",
                              idle_poll_seconds=0.01, **kwargs)
    return Supervisor(config)


@pytest.mark.asyncio
async def test_runs_a_task_to_success(tmp_path: Path) -> None:
    sup = make_supervisor(tmp_path)
    seen: list[str] = []

    async def handler(task: Task, tools: ToolSession) -> dict:
        seen.append(task.title)
        return {"ok": True}

    sup.register("demo", handler)
    sup.queue.add_task("do the thing", agent_kind="demo")

    stats = await sup.run(max_tasks=1)
    assert seen == ["do the thing"]
    assert stats.succeeded == 1
    assert sup.queue.counts() == {"succeeded": 1}
    sup.close()


@pytest.mark.asyncio
async def test_handler_exception_marks_task_failed_and_retries(
    tmp_path: Path,
) -> None:
    sup = make_supervisor(tmp_path)

    async def boom(task: Task, tools: ToolSession) -> dict:
        raise ValueError("handler exploded")

    sup.register("demo", boom)
    # Idempotent: this test is about the retry mechanism, not about
    # whether an ordinary failure should retry at all (issue #51).
    task_id = sup.queue.add_task("doomed", agent_kind="demo", max_attempts=2,
                                  idempotent=True)

    await sup.run(max_tasks=1)
    task = sup.queue.get(task_id)
    # Attempts remain, so it goes back to queued rather than failed.
    assert task.state == "queued"
    assert "ValueError: handler exploded" in task.last_error
    sup.close()


@pytest.mark.asyncio
async def test_task_without_handler_is_cancelled_not_retried(
    tmp_path: Path,
) -> None:
    """A missing handler is not transient, so retrying would just spin."""
    sup = make_supervisor(tmp_path)
    task_id = sup.queue.add_task("orphan", agent_kind="nonexistent")

    await sup.run(max_tasks=1)
    task = sup.queue.get(task_id)
    assert task.state == "cancelled"
    assert "no handler" in task.last_error
    sup.close()


@pytest.mark.asyncio
async def test_only_one_heavy_task_runs_at_a_time(tmp_path: Path) -> None:
    """The 32 GB constraint: one heavy inference slot, enforced."""
    sup = make_supervisor(tmp_path, heavy_slots=1, light_slots=4)
    concurrent = 0
    peak = 0

    async def heavy(task: Task, tools: ToolSession) -> dict:
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        await asyncio.sleep(0.05)
        concurrent -= 1
        return {}

    sup.register("infer", heavy)
    for i in range(4):
        sup.queue.add_task(f"heavy-{i}", agent_kind="infer", weight="heavy")

    await sup.run(max_tasks=4)
    assert peak == 1, f"heavy slot breached: {peak} concurrent heavy tasks"
    assert sup.stats.succeeded == 4
    sup.close()


@pytest.mark.asyncio
async def test_light_tasks_run_concurrently(tmp_path: Path) -> None:
    sup = make_supervisor(tmp_path, heavy_slots=1, light_slots=3)
    concurrent = 0
    peak = 0

    async def light(task: Task, tools: ToolSession) -> dict:
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        await asyncio.sleep(0.05)
        concurrent -= 1
        return {}

    sup.register("io", light)
    for i in range(3):
        sup.queue.add_task(f"light-{i}", agent_kind="io")

    await sup.run(max_tasks=3)
    assert peak > 1, "light tasks should not be serialised"
    assert peak <= 3
    sup.close()


@pytest.mark.asyncio
async def test_priority_order_is_respected(tmp_path: Path) -> None:
    sup = make_supervisor(tmp_path, light_slots=1)
    order: list[str] = []

    async def record(task: Task, tools: ToolSession) -> dict:
        order.append(task.title)
        return {}

    sup.register("demo", record)
    sup.queue.add_task("low", agent_kind="demo", priority=200)
    sup.queue.add_task("high", agent_kind="demo", priority=1)

    await sup.run(max_tasks=2)
    assert order == ["high", "low"]
    sup.close()


@pytest.mark.asyncio
async def test_startup_recovers_interrupted_idempotent_task(
    tmp_path: Path,
) -> None:
    db = tmp_path / "lab.db"
    owner = "supervisor-under-test"
    with TaskQueue(db, owner=owner) as q:
        task_id = q.add_task("was running", agent_kind="demo", idempotent=True)
        tok = q.lease().lease
        q.start(tok)  # process dies here

    # A restart is the same logical supervisor, so it carries the same owner
    # name and may reclaim its own stranded work immediately.
    sup = Supervisor(SupervisorConfig(db_path=db, idle_poll_seconds=0.01,
                                      owner=owner))
    ran: list[str] = []

    async def handler(task: Task, tools: ToolSession) -> dict:
        ran.append(task.id)
        return {}

    sup.register("demo", handler)
    stats = await sup.run(max_tasks=1)

    assert stats.recovered["interrupted"] == 1
    assert stats.recovered["requeued"] == 1
    assert ran == [task_id], "recovered idempotent task should be re-run"
    sup.close()


@pytest.mark.asyncio
async def test_startup_does_not_rerun_interrupted_destructive_task(
    tmp_path: Path,
) -> None:
    """The safety property that matters most: no blind replay."""
    db = tmp_path / "lab.db"
    owner = "supervisor-under-test"
    with TaskQueue(db, owner=owner) as q:
        task_id = q.add_task("charge the card", agent_kind="demo",
                             idempotent=False)
        tok = q.lease().lease
        q.start(tok)

    sup = Supervisor(SupervisorConfig(db_path=db, idle_poll_seconds=0.01,
                                      owner=owner))
    ran: list[str] = []

    async def handler(task: Task, tools: ToolSession) -> dict:
        ran.append(task.id)
        return {}

    sup.register("demo", handler)
    stats = await sup.run(max_tasks=1)

    assert stats.recovered["held_for_review"] == 1
    assert ran == [], "a destructive task must never be silently replayed"
    assert sup.queue.get(task_id).state == "interrupted"
    sup.close()


@pytest.mark.asyncio
async def test_stop_ends_the_loop(tmp_path: Path) -> None:
    sup = make_supervisor(tmp_path)

    async def stopper() -> None:
        await asyncio.sleep(0.02)
        sup.stop()

    # Empty queue, so run() would otherwise poll forever.
    await asyncio.gather(sup.run(), stopper())
    assert sup.stats.leased == 0
    sup.close()


@pytest.mark.asyncio
async def test_empty_queue_with_max_tasks_terminates(tmp_path: Path) -> None:
    sup = make_supervisor(tmp_path)
    stats = await asyncio.wait_for(sup.run(max_tasks=5), timeout=2.0)
    assert stats.leased == 0
    sup.close()


@pytest.mark.asyncio
async def test_a_second_supervisor_refuses_to_start(tmp_path: Path) -> None:
    """Item 1.3: recovery reclaims by owner name, so two supervisors on one
    host must not both run. The second refuses before recovering."""
    from lab.supervisor import AlreadyRunning, acquire_singleton, release_singleton

    first = make_supervisor(tmp_path)
    second = make_supervisor(tmp_path)
    started = asyncio.Event()

    async def hold(task: Task, tools: ToolSession) -> dict:
        started.set()
        await asyncio.sleep(0.2)
        return {}

    first.register("demo", hold)
    first.queue.add_task("long", agent_kind="demo")
    run = asyncio.create_task(first.run(max_tasks=1))
    await started.wait()
    with pytest.raises(AlreadyRunning):
        await second.run(max_tasks=1)
    await run

    # Released on exit, so the next start succeeds.
    fd = acquire_singleton(tmp_path / "lab.db")
    release_singleton(fd)
    first.close()
    second.close()


@pytest.mark.asyncio
async def test_tool_approval_parks_then_grant_reruns_the_call(tmp_path: Path) -> None:
    """Item 1.1 end to end: a handler's approve-tier call parks the task,
    a human grants that exact call, and the rerun performs it once."""
    sup = make_supervisor(tmp_path)
    deleted: list[bool] = []

    async def cleanup(task: Task, tools: ToolSession) -> dict:
        if not tools.submit("fs.list").detail.get("entries"):
            tools.submit("fs.write", path="old.log", content="x")
        result = tools.submit("fs.delete", path="old.log")
        deleted.append(result.ok)
        return {"deleted": result.ok}

    sup.register("cleanup", cleanup, tools={"fs.write", "fs.list", "fs.delete"})
    task_id = sup.queue.add_task("clean", agent_kind="cleanup")

    stats = await sup.run(max_tasks=1)
    assert stats.awaiting_approval == 1 and deleted == []
    assert sup.queue.get(task_id).state == "awaiting_approval"
    assert (sup.broker._workspaces[task_id].root / "old.log").exists()

    (pending,) = sup.policy.pending()
    assert sup.policy.grant(pending["id"], decided_by="operator") == task_id

    sup.stats.leased = 0
    await sup.run(max_tasks=1)
    assert deleted == [True]
    assert sup.queue.get(task_id).state == "succeeded"
    assert task_id not in sup.broker._workspaces, "closed once terminal"
    sup.close()


@pytest.mark.asyncio
async def test_a_handler_gets_only_its_registered_tools(tmp_path: Path) -> None:
    """Item 1.2: the allowlist comes from trusted registration, not the task."""
    sup = make_supervisor(tmp_path)
    seen: dict = {}

    async def reader(task: Task, tools: ToolSession) -> dict:
        seen["ctx"] = tools.context
        seen["write"] = tools.submit("fs.write", path="x", content="y")
        return {}

    sup.register("reader", reader, tools={"fs.read"})
    task_id = sup.queue.add_task("read", agent_kind="reader",
                                 payload={"tools": ["fs.write"]})
    await sup.run(max_tasks=1)
    assert seen["ctx"].task_id == task_id and seen["ctx"].attempt == 1
    assert not seen["write"].ok and "ToolNotAllowed" in seen["write"].error
    sup.close()


@pytest.mark.asyncio
async def test_a_session_dies_with_its_lease(tmp_path: Path) -> None:
    """Item 1.2: once the lease is gone, the handler's session cannot act."""
    sup = make_supervisor(tmp_path)
    seen: dict = {}

    async def slow(task: Task, tools: ToolSession) -> dict:
        sup.queue._conn.execute(
            "UPDATE leases SET expires_at = datetime('now', '-1 second') "
            "WHERE task_id = ?", (task.id,))
        seen["after"] = tools.submit("fs.write", path="late", content="x")
        return {}

    sup.register("slow", slow, tools={"fs.write"})
    sup.queue.add_task("slow", agent_kind="slow")
    await sup.run(max_tasks=1)
    assert not seen["after"].ok and "ContextRevoked" in seen["after"].error
    sup.close()


# ------------------------------------------------ item 1.2, worker process


@pytest.mark.asyncio
async def test_reviewed_handler_runs_in_its_own_process(tmp_path: Path,
                                                        monkeypatch) -> None:
    import os
    monkeypatch.setenv("LAB_SUPERVISOR_SECRET", "must-not-reach-workers")
    sup = make_supervisor(tmp_path)
    sup.register_reviewed("probe", "lab.handlers.demo:describe_process")
    task_id = sup.queue.add_task("probe", agent_kind="probe")
    await sup.run(max_tasks=1)
    result = json.loads(sup.queue._conn.execute(
        "SELECT result FROM tasks WHERE id = ?", (task_id,)).fetchone()[0])
    assert sup.queue.get(task_id).state == "succeeded"
    assert result["pid"] != os.getpid()
    assert "LAB_SUPERVISOR_SECRET" not in result["env"]
    assert set(result["env"]) <= {"PATH", "HOME", "LANG", "PYTHONDONTWRITEBYTECODE",
                                  "__CF_USER_TEXT_ENCODING", "LC_CTYPE"}
    assert Path(result["cwd"]).name.startswith(f"task-{task_id}")
    sup.close()


@pytest.mark.asyncio
async def test_worker_tool_calls_go_through_the_broker(tmp_path: Path) -> None:
    sup = make_supervisor(tmp_path)
    sup.register_reviewed("note", "lab.handlers.demo:write_note",
                          tools={"fs.write", "fs.read"})
    task_id = sup.queue.add_task("note", agent_kind="note", payload={"note": "hi"})
    await sup.run(max_tasks=1)
    result = json.loads(sup.queue._conn.execute(
        "SELECT result FROM tasks WHERE id = ?", (task_id,)).fetchone()[0])
    assert result == {"read_back": "hi"}
    kinds = [r[0] for r in sup.queue._conn.execute(
        "SELECT kind FROM events WHERE task_id = ? AND kind LIKE 'tool_%'", (task_id,))]
    assert kinds == ["tool_allow", "tool_allow"]
    sup.close()


@pytest.mark.asyncio
async def test_worker_approval_parks_and_grant_completes(tmp_path: Path) -> None:
    sup = make_supervisor(tmp_path)
    sup.register_reviewed("del", "lab.handlers.demo:delete_note",
                          tools={"fs.write", "fs.list", "fs.delete"})
    task_id = sup.queue.add_task("del", agent_kind="del")
    await sup.run(max_tasks=1)
    assert sup.queue.get(task_id).state == "awaiting_approval"
    (pending,) = sup.policy.pending()
    sup.policy.grant(pending["id"], decided_by="operator")
    sup.stats.leased = 0
    await sup.run(max_tasks=1)
    assert sup.queue.get(task_id).state == "succeeded"
    sup.close()


@pytest.mark.asyncio
async def test_worker_exception_fails_the_task(tmp_path: Path) -> None:
    sup = make_supervisor(tmp_path)
    sup.register_reviewed("boom", "lab.handlers.demo:explode")
    task_id = sup.queue.add_task("boom", agent_kind="boom")
    await sup.run(max_tasks=1)
    task = sup.queue.get(task_id)
    assert task.state == "failed"
    assert "handler exploded in the worker" in task.last_error
    sup.close()


@pytest.mark.parametrize("ref", [
    "os:system", "lab.queue:TaskQueue", "lab.handlers", "lab.handlersx.demo:write_note",
    "lab.handlers.demo:write_note; import os",
])
def test_only_reviewed_handlers_can_be_registered(tmp_path: Path, ref: str) -> None:
    sup = make_supervisor(tmp_path)
    with pytest.raises(ValueError, match="not a reviewed handler"):
        sup.register_reviewed("x", ref)
    sup.close()
