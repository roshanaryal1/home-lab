"""CPU and memory ceilings per task (H2, #16).

A worker process that exceeds either is killed with its whole process
group, and the task fails permanently with the reason recorded and audited.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from lab.supervisor import Supervisor, SupervisorConfig


def _sup(tmp_path: Path, **kw) -> Supervisor:
    return Supervisor(SupervisorConfig(db_path=tmp_path / "lab.db",
                                       idle_poll_seconds=0.01, **kw))


def _events(sup: Supervisor, task_id: str, kind: str) -> list[dict]:
    rows = sup.queue._conn.execute(
        "SELECT detail FROM events WHERE task_id = ? AND kind = ?", (task_id, kind)).fetchall()
    return [json.loads(r["detail"]) for r in rows]


@pytest.mark.safety
@pytest.mark.asyncio
async def test_a_task_over_its_memory_ceiling_is_killed_and_failed_for_good(
        tmp_path: Path) -> None:
    sup = _sup(tmp_path, task_max_rss_mb=64, ceiling_poll_seconds=0.05)
    sup.register_reviewed("hog", "lab.handlers.demo:hold_memory")
    task_id = sup.queue.add_task("hog", agent_kind="hog", payload={"mb": 300, "seconds": 30},
                                 idempotent=True)
    start = time.monotonic()
    await asyncio.wait_for(sup.run(max_tasks=1), timeout=20)
    task = sup.queue.get(task_id)
    assert task.state == "failed", "a memory hog must not be retried"
    assert "memory ceiling of 64 MB" in (task.last_error or "")
    assert time.monotonic() - start < 15
    (event,) = _events(sup, task_id, "resource_ceiling_exceeded")
    assert event["resource"] == "memory" and event["limit_mb"] == 64
    assert event["observed_mb"] > 64
    assert sup.queue.get(task_id).attempts <= 1
    sup.close()


@pytest.mark.safety
@pytest.mark.asyncio
async def test_a_task_over_its_cpu_ceiling_is_failed_for_good(tmp_path: Path) -> None:
    sup = _sup(tmp_path, task_max_cpu_seconds=1)
    sup.register_reviewed("spin", "lab.handlers.demo:spin_cpu")
    task_id = sup.queue.add_task("spin", agent_kind="spin", payload={"seconds": 30},
                                 idempotent=True)
    await asyncio.wait_for(sup.run(max_tasks=1), timeout=20)
    task = sup.queue.get(task_id)
    assert task.state == "failed" and "cpu ceiling of 1s" in (task.last_error or "")
    (event,) = _events(sup, task_id, "resource_ceiling_exceeded")
    assert event["resource"] == "cpu"
    sup.close()


@pytest.mark.asyncio
async def test_a_well_behaved_task_is_untouched_by_the_ceilings(tmp_path: Path) -> None:
    sup = _sup(tmp_path, task_max_rss_mb=512, task_max_cpu_seconds=30, ceiling_poll_seconds=0.05)
    sup.register_reviewed("note", "lab.handlers.demo:write_note", tools={"fs.write", "fs.read"})
    task_id = sup.queue.add_task("n", agent_kind="note", payload={"note": "hi"})
    await asyncio.wait_for(sup.run(max_tasks=1), timeout=20)
    assert sup.queue.get(task_id).state == "succeeded"
    assert _events(sup, task_id, "resource_ceiling_exceeded") == []
    sup.close()


def test_the_defaults_are_bounded_not_unlimited() -> None:
    config = SupervisorConfig(db_path="x.db")
    assert config.task_max_rss_mb is not None and config.task_max_rss_mb > 0
    assert config.task_max_cpu_seconds is not None and config.task_max_cpu_seconds > 0


def test_status_counts_a_ceiling_kill_as_a_forced_termination() -> None:
    from lab.metrics import COUNTERS
    assert "resource_ceiling_exceeded" in COUNTERS["forced_terminations"]


def test_rss_sampler_reads_this_process_group(tmp_path: Path) -> None:
    from lab.worker import group_rss_mb
    assert group_rss_mb(os.getpgrp()) > 0
    assert group_rss_mb(2**22 + 99) == 0
