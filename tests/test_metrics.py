"""Operational metrics from the event log (item #89)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lab import metrics
from lab.cli import main
from lab.egress import EgressDenied, EgressGateway
from lab.policy import PolicyEngine, Tier
from lab.queue import LeaseLost, Task, TaskQueue
from lab.supervisor import Supervisor, SupervisorConfig


@pytest.fixture()
def q(tmp_path: Path) -> TaskQueue:
    with TaskQueue(tmp_path / "lab.db") as queue:
        yield queue


def collect(q: TaskQueue, **kw) -> metrics.Metrics:
    return metrics.collect(q._conn, **kw)


def later(seconds: float) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


def run_to_success(q: TaskQueue) -> str:
    task_id = q.add_task("t")
    task = q.lease()
    assert task is not None and task.lease is not None
    q.start(task.lease)
    q.succeed(task.lease, {})
    return task_id


# ------------------------------------------------------- queue and worker


def test_an_empty_database_is_idle(q: TaskQueue) -> None:
    m = collect(q)
    assert m.health == "idle" and m.states == {} and m.oldest_queued_seconds is None
    assert m.last_success_at is None and all(v == 0 for v in m.counters.values())


def test_counts_per_state_and_the_age_of_the_oldest_queued_task(q: TaskQueue) -> None:
    for i in range(3):
        q.add_task(f"t{i}")
    q.lease()
    m = collect(q, now=later(600))
    assert m.states == {"queued": 2, "leased": 1}
    assert 590 < (m.oldest_queued_seconds or 0) < 610


def test_last_success_is_recorded(q: TaskQueue) -> None:
    run_to_success(q)
    m = collect(q, now=later(120))
    assert m.last_success_at and 110 < (m.last_success_age_seconds or 0) < 130
    assert m.states == {"succeeded": 1}


def test_an_approval_waiting_is_counted_aged_and_asks_for_attention(q: TaskQueue) -> None:
    q._conn.execute("INSERT INTO tasks (id, title) VALUES ('t1', 't1')")
    PolicyEngine(q._conn).authorize_tool("t1", "fs.delete", {"path": "x"}, Tier.APPROVE)
    m = collect(q)
    assert m.pending_approvals == 1 and m.health == "attention"
    assert "approval" in m.reasons[0]


# ------------------------------------------------------------------ health


@pytest.mark.safety
def test_a_running_task_on_an_expired_lease_is_unhealthy(q: TaskQueue) -> None:
    q.add_task("t")
    task = q.lease(ttl_seconds=1)
    assert task is not None and task.lease is not None
    q.start(task.lease)
    assert collect(q).health == "ok", "the lease is still live now"
    m = collect(q, now=later(30))
    assert m.health == "unhealthy" and "expired lease" in m.reasons[0]


@pytest.mark.safety
def test_work_waiting_with_no_worker_and_a_silent_log_is_unhealthy(q: TaskQueue) -> None:
    q.add_task("t")
    fresh = collect(q, now=later(10))
    assert fresh.health == "ok", "just enqueued: not stalled yet"
    stalled = collect(q, now=later(3600))
    assert stalled.health == "unhealthy" and "no live worker" in stalled.reasons[0]
    assert collect(q, now=later(3600), stall_seconds=7200).health == "ok"


def test_a_live_lease_means_a_worker_is_there(q: TaskQueue) -> None:
    q.add_task("a")
    q.add_task("b")
    task = q.lease(ttl_seconds=100000)
    assert task is not None
    m = collect(q, now=later(3600))
    assert m.live_leases == 1 and m.health == "ok"


# ---------------------------------------------------------------- counters
# Each counter is produced by the real code path that writes its event.


def test_policy_denials_count_task_level_tool_level_and_authority_refusals(
        q: TaskQueue, tmp_path: Path) -> None:
    policy = PolicyEngine(q._conn)
    task_id = q.add_task("never", capability_tier="never")
    task = q.get(task_id)
    assert task is not None
    policy.authorize(task)                                              # policy_deny
    q._conn.execute("INSERT INTO tasks (id, title) VALUES ('t1', 't1')")
    policy.authorize_tool("t1", "fs.delete", {"path": "x"}, Tier.NEVER)  # tool_deny
    assert collect(q).counters["policy_denials"] == 2


@pytest.mark.asyncio
async def test_authority_refusals_are_policy_denials(tmp_path: Path) -> None:
    sup = Supervisor(SupervisorConfig(db_path=tmp_path / "lab.db", idle_poll_seconds=0.01))

    async def handler(task: Task, tools) -> dict:
        return {}

    sup.register("leaky", handler, sensitive_data=True, external_action=True)
    sup.queue.add_task("t", agent_kind="leaky")               # unknown origin: untrusted
    await sup.run(max_tasks=1)
    assert metrics.collect(sup.queue._conn).counters["policy_denials"] == 1
    sup.close()


def test_rejected_approvals_are_counted(q: TaskQueue, tmp_path: Path) -> None:
    from lab import operator as op
    _, public = op.generate(tmp_path / "k")
    policy = PolicyEngine(q._conn, op.load_public(public))
    q._conn.execute("INSERT INTO tasks (id, title) VALUES ('t1', 't1')")
    res = policy.authorize_tool("t1", "fs.delete", {"path": "x"}, Tier.APPROVE)
    assert res.approval_id
    policy.grant(res.approval_id, decided_by="agent")                    # unsigned
    policy.authorize_tool("t1", "fs.delete", {"path": "x"}, Tier.APPROVE)
    assert collect(q).counters["approvals_rejected"] == 1


def test_egress_denials_are_counted(q: TaskQueue) -> None:
    policy = PolicyEngine(q._conn)
    gw = EgressGateway(lambda h, p: ["93.184.216.34"], lambda *a, **k: None,  # type: ignore[arg-type,return-value]
                       audit=lambda kind, d: policy.audit(d.get("task_id") or None, kind, d))
    for _ in range(2):
        with pytest.raises(EgressDenied):
            gw.fetch("https://evil.example.com/", frozenset({"docs.example.org"}), "t1")
    assert collect(q).counters["egress_denials"] == 2


def test_lease_losses_are_counted(q: TaskQueue) -> None:
    q.add_task("t")
    task = q.lease()
    assert task is not None and task.lease is not None
    q._conn.execute("UPDATE leases SET released_at = datetime('now')")
    with pytest.raises(LeaseLost):
        q.succeed(task.lease, {})
    assert collect(q).counters["lease_losses"] == 1


def test_retries_are_counted(q: TaskQueue) -> None:
    q.add_task("t", idempotent=True, max_attempts=3)
    task = q.lease()
    assert task is not None and task.lease is not None
    q.start(task.lease)
    q.fail(task.lease, "boom")
    assert q.counts() == {"queued": 1}
    assert collect(q).retries == 1


def test_recoveries_are_counted(tmp_path: Path) -> None:
    db = tmp_path / "lab.db"
    with TaskQueue(db, owner="a") as first:
        first.add_task("t", idempotent=True)
        task = first.lease()
        assert task is not None and task.lease is not None
        first.start(task.lease)
    with TaskQueue(db, owner="a") as second:
        second.recover()
        assert metrics.collect(second._conn).recoveries == 1


@pytest.mark.asyncio
async def test_a_wall_clock_kill_is_a_forced_termination(tmp_path: Path) -> None:
    sup = Supervisor(SupervisorConfig(db_path=tmp_path / "lab.db", idle_poll_seconds=0.01,
                                      task_timeout_seconds=0.2))

    async def slow(task: Task, tools) -> dict:
        await asyncio.sleep(30)
        return {}

    sup.register("slow", slow)
    sup.queue.add_task("t", agent_kind="slow")
    await sup.run(max_tasks=1)
    assert metrics.collect(sup.queue._conn).counters["forced_terminations"] == 1
    sup.close()


@pytest.mark.asyncio
async def test_lease_loss_and_emergency_stop_are_forced_terminations(tmp_path: Path) -> None:
    sup = Supervisor(SupervisorConfig(db_path=tmp_path / "lab.db", idle_poll_seconds=0.01,
                                      stop_grace_seconds=0.2))
    started = asyncio.Event()

    async def poller(task: Task, tools) -> dict:
        started.set()
        while True:
            await asyncio.sleep(0.02)

    sup.register("poll", poller)
    sup.queue.add_task("t", agent_kind="poll")
    run = asyncio.create_task(sup.run(max_tasks=1))
    await started.wait()
    await sup.emergency_stop()
    await asyncio.wait_for(run, timeout=10)
    assert metrics.collect(sup.queue._conn).counters["forced_terminations"] == 1
    sup.close()


@pytest.mark.asyncio
async def test_worker_errors_are_counted(tmp_path: Path, monkeypatch) -> None:
    sup = Supervisor(SupervisorConfig(db_path=tmp_path / "lab.db", idle_poll_seconds=0.01,
                                      max_consecutive_worker_errors=2))
    sup.queue.add_task("t")

    def broken(task):
        raise RuntimeError("injected")

    monkeypatch.setattr(sup.policy, "authorize", broken)

    async def handler(task: Task, tools) -> dict:
        return {}

    sup.register("x", handler)
    sup.queue._conn.execute("UPDATE tasks SET agent_kind = 'x'")
    await sup.run(max_tasks=2)
    assert metrics.collect(sup.queue._conn).counters["worker_errors"] >= 1
    sup.close()


def test_the_window_limits_counters_but_not_the_queue(q: TaskQueue) -> None:
    policy = PolicyEngine(q._conn)
    q._conn.execute("INSERT INTO tasks (id, title) VALUES ('t1', 't1')")
    policy.authorize_tool("t1", "fs.delete", {"path": "x"}, Tier.NEVER)
    assert collect(q, now=later(3600)).counters["policy_denials"] == 1
    assert collect(q, now=later(3600), window_hours=0.5).counters["policy_denials"] == 0
    assert collect(q, now=later(3600), window_hours=0.5).states == {"queued": 1}


def test_metrics_read_the_events_table_and_nothing_else_is_written(q: TaskQueue) -> None:
    before = q._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    tables = {r[0] for r in q._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    collect(q)
    assert q._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before
    assert not any("metric" in t for t in tables), "no second store"


# --------------------------------------------------------------------- CLI


def test_status_prints_counts_and_ages_from_a_test_db(tmp_path: Path, capsys) -> None:
    db = tmp_path / "lab.db"
    with TaskQueue(db) as queue:
        queue.add_task("a")
        run_to_success(queue)
    assert main(["--db", str(db), "status"]) == 0
    out = capsys.readouterr().out
    assert "health   OK" in out and "queued" in out and "succeeded" in out
    assert "oldest queued" in out and "policy_denials" in out
    assert main(["--db", str(db), "status", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["states"] == {"queued": 1, "succeeded": 1} and data["model"] is None


def test_status_of_a_stalled_worker_exits_nonzero_for_a_watchdog(
        tmp_path: Path, capsys) -> None:
    db = tmp_path / "lab.db"
    with TaskQueue(db) as queue:
        queue.add_task("stuck")
    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute("UPDATE tasks SET created_at = datetime('now', '-2 hours')")
    conn.execute("DROP TRIGGER events_no_update")
    conn.execute("UPDATE events SET created_at = datetime('now', '-2 hours')")
    conn.close()
    assert main(["--db", str(db), "status"]) == 2
    assert "UNHEALTHY" in capsys.readouterr().out


def test_status_of_a_missing_database_fails_clearly(tmp_path: Path, capsys) -> None:
    assert main(["--db", str(tmp_path / "nope.db"), "status"]) == 1
