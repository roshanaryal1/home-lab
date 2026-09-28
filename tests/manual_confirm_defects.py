"""Regression check for the two defects confirmed on 2026-09-25.

Originally this script *reproduced* both bugs. It is kept, and inverted,
so the fixes stay honest: if either defect comes back, this reports it in
the same terms the original measurement used.

Run directly: python3 tests/manual_confirm_defects.py
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from lab.broker import ToolSession
from lab.queue import Task, TaskQueue
from lab.supervisor import Supervisor, SupervisorConfig


async def check_bounded_leasing(tmp: Path) -> bool:
    """Defect 1: the supervisor leased far more work than it could run.

    Measured before the fix: 1 light slot and 20 queued tasks gave 19
    leased and 1 running. All nineteen were needlessly exposed to a crash.
    """
    sup = Supervisor(SupervisorConfig(db_path=tmp / "a.db", heavy_slots=1,
                                      light_slots=1, idle_poll_seconds=0.01))

    async def slow(task: Task, tools: ToolSession) -> dict:
        await asyncio.sleep(0.3)
        return {}

    sup.register("x", slow)
    for i in range(20):
        sup.queue.add_task(f"t{i}", agent_kind="x")

    async def probe() -> dict:
        await asyncio.sleep(0.1)
        counts = sup.queue.counts()
        sup.stop()
        return counts

    _, counts = await asyncio.gather(sup.run(max_tasks=20), probe())
    held = counts.get("leased", 0) + counts.get("running", 0)
    print(f"  queue after 0.1s with 1 slot: {counts}")
    print(f"  leased or running: {held} (bounded means about 1)")
    sup.close()
    return held <= 2


async def check_long_task_keeps_its_lease(tmp: Path) -> bool:
    """Defect 2: a long task lost its lease and another worker re-ran it.

    Measured before the fix: a second supervisor saw a task the first was
    *still running*, judged it abandoned, and requeued it. That is
    duplicate execution, and it defeats the central guarantee.

    The fix is the heartbeat, which lives in the supervisor, so this
    exercises the supervisor rather than the raw queue.
    """
    db = tmp / "b.db"
    sup = Supervisor(SupervisorConfig(
        db_path=db, light_slots=1, idle_poll_seconds=0.01,
        lease_ttl_seconds=1,        # deliberately shorter than the task
        owner="supervisor-a",
    ))
    stolen = False

    async def long_task(task: Task, tools: ToolSession) -> dict:
        nonlocal stolen
        await asyncio.sleep(1.4)    # well past the 1 second lease TTL
        with TaskQueue(db, owner="supervisor-b") as other:
            stats = other.recover()
            if stats["interrupted"]:
                stolen = True
            print(f"  other supervisor's recover(): {stats}")
        return {}

    sup.register("slow", long_task)
    sup.queue.add_task("outlives its lease", agent_kind="slow",
                       idempotent=True)
    await sup.run(max_tasks=1)
    sup.close()
    print(f"  task stolen while still running: {stolen}")
    return not stolen


async def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    ok = True

    print("DEFECT 1: unbounded leasing")
    fixed = await check_bounded_leasing(tmp)
    print(f"  VERDICT: {'FIXED' if fixed else 'STILL PRESENT'}\n")
    ok &= fixed

    print("DEFECT 2: long task loses its lease and is re-run")
    fixed = await check_long_task_keeps_its_lease(tmp)
    print(f"  VERDICT: {'FIXED' if fixed else 'STILL PRESENT'}")
    ok &= fixed

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
