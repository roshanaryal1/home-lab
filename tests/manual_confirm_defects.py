"""Confirm (or refute) the two reviewer claims that are testable."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / "RnD/llm-architects/home-lab"))
from lab.queue import Task, TaskQueue
from lab.supervisor import Supervisor, SupervisorConfig


async def test_unbounded_leasing(tmp):
    """Claim: supervisor leases far more tasks than it can run."""
    sup = Supervisor(SupervisorConfig(db_path=tmp/"a.db", heavy_slots=1,
                                      light_slots=1, idle_poll_seconds=0.01))
    started = 0
    async def slow(task: Task) -> dict:
        nonlocal started
        started += 1
        await asyncio.sleep(0.3)
        return {}
    sup.register("x", slow)
    for i in range(20):
        sup.queue.add_task(f"t{i}", agent_kind="x")

    async def probe():
        await asyncio.sleep(0.1)   # let it lease
        counts = sup.queue.counts()
        sup.stop()
        return counts
    _, counts = await asyncio.gather(sup.run(max_tasks=20), probe())
    print(f"  after 0.1s with 1 light slot: {counts}")
    leased = counts.get("leased", 0) + counts.get("running", 0)
    print(f"  leased-or-running = {leased} (should be ~1 if bounded)")
    sup.close()
    return leased

async def test_lease_expiry(tmp):
    """Claim: a long task's lease expires while it is still running."""
    q = TaskQueue(tmp/"b.db", owner="s1")
    tid = q.add_task("long one", idempotent=True)
    q.lease(ttl_seconds=1)     # 1 second lease
    q.start(tid)
    await asyncio.sleep(1.5)   # outlive it
    # A second supervisor recovering would see an expired lease.
    q2 = TaskQueue(tmp/"b.db", owner="s2")
    stats = q2.recover()
    print(f"  recover() while s1 still 'working': {stats}")
    state = q2.get(tid).state
    print(f"  task state now: {state}")
    q.close()
    q2.close()
    return stats

async def main():
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    print("CLAIM 1: unbounded leasing")
    leased = await test_unbounded_leasing(tmp)
    print(f"  VERDICT: {'CONFIRMED BUG' if leased > 3 else 'not reproduced'}\n")
    print("CLAIM 2: no lease renewal, long task loses its lease")
    stats = await test_lease_expiry(tmp)
    print(f"  VERDICT: {'CONFIRMED BUG' if stats['interrupted'] > 0 else 'not reproduced'}")

asyncio.run(main())
