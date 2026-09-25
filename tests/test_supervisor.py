"""Tests for the supervisor loop.

The load-bearing test here is the heavy-slot one: on a 32 GB machine a
second concurrent heavy model does not fit, so that limit has to be
enforced by the code rather than by convention.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lab.queue import Task, TaskQueue  # noqa: E402
from lab.supervisor import Supervisor, SupervisorConfig  # noqa: E402


def make_supervisor(tmp_path: Path, **kwargs) -> Supervisor:
    config = SupervisorConfig(db_path=tmp_path / "lab.db",
                              idle_poll_seconds=0.01, **kwargs)
    return Supervisor(config)


@pytest.mark.asyncio
async def test_runs_a_task_to_success(tmp_path: Path) -> None:
    sup = make_supervisor(tmp_path)
    seen: list[str] = []

    async def handler(task: Task) -> dict:
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

    async def boom(task: Task) -> dict:
        raise ValueError("handler exploded")

    sup.register("demo", boom)
    task_id = sup.queue.add_task("doomed", agent_kind="demo", max_attempts=2)

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

    async def heavy(task: Task) -> dict:
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        await asyncio.sleep(0.05)
        concurrent -= 1
        return {}

    sup.register("infer", heavy)
    for i in range(4):
        sup.queue.add_task(f"heavy-{i}", agent_kind="infer",
                           payload={"weight": "heavy"})

    await sup.run(max_tasks=4)
    assert peak == 1, f"heavy slot breached: {peak} concurrent heavy tasks"
    assert sup.stats.succeeded == 4
    sup.close()


@pytest.mark.asyncio
async def test_light_tasks_run_concurrently(tmp_path: Path) -> None:
    sup = make_supervisor(tmp_path, heavy_slots=1, light_slots=3)
    concurrent = 0
    peak = 0

    async def light(task: Task) -> dict:
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

    async def record(task: Task) -> dict:
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
    with TaskQueue(db, owner="crashed") as q:
        task_id = q.add_task("was running", agent_kind="demo", idempotent=True)
        q.lease()
        q.start(task_id)  # process dies here

    sup = Supervisor(SupervisorConfig(db_path=db, idle_poll_seconds=0.01))
    ran: list[str] = []

    async def handler(task: Task) -> dict:
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
    with TaskQueue(db, owner="crashed") as q:
        task_id = q.add_task("charge the card", agent_kind="demo",
                             idempotent=False)
        q.lease()
        q.start(task_id)

    sup = Supervisor(SupervisorConfig(db_path=db, idle_poll_seconds=0.01))
    ran: list[str] = []

    async def handler(task: Task) -> dict:
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
