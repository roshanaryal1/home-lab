"""Operator controls: pause, drain, stop, cancel (item 6.3, #79).

The mode lives in the database, so a person at another terminal can
change it and a running supervisor obeys within one poll. Stopping an
active task must leave no later tool effect.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from lab import control
from lab.audit import verify_chain
from lab.broker import ToolSession
from lab.cli import main
from lab.queue import Task, TaskQueue
from lab.supervisor import Supervisor, SupervisorConfig


def _sup(tmp_path: Path, **kw) -> Supervisor:
    return Supervisor(SupervisorConfig(db_path=tmp_path / "lab.db",
                                       idle_poll_seconds=0.01, **kw))


async def _until(predicate, timeout: float = 5.0) -> None:
    end = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < end, "condition never held"
        await asyncio.sleep(0.01)


def test_starts_running_and_records_every_change(tmp_path: Path) -> None:
    with TaskQueue(tmp_path / "lab.db", owner="t") as q:
        assert control.get(q._conn).mode == "running"
        control.set_mode(q._conn, "paused", by="roshan", reason="deploying")
        state = control.get(q._conn)
        assert (state.mode, state.set_by, state.reason) == ("paused", "roshan", "deploying")
        rows = q._conn.execute(
            "SELECT detail FROM events WHERE kind = 'control_changed'").fetchall()
        assert len(rows) == 1
        assert verify_chain(q._conn).ok


def test_unknown_mode_is_refused(tmp_path: Path) -> None:
    with TaskQueue(tmp_path / "lab.db", owner="t") as q:
        with pytest.raises(control.ControlError):
            control.set_mode(q._conn, "yolo", by="x")
        with pytest.raises(sqlite3.IntegrityError):
            q._conn.execute("UPDATE control SET mode = 'yolo'")


@pytest.mark.safety
@pytest.mark.asyncio
async def test_a_paused_lab_leases_nothing_until_resumed(tmp_path: Path) -> None:
    sup = _sup(tmp_path)
    ran: list[str] = []

    async def handler(task: Task, tools: ToolSession) -> dict:
        ran.append(task.id)
        return {}

    sup.register("h", handler)
    control.set_mode(sup.queue._conn, "paused", by="roshan")
    task_id = sup.queue.add_task("t", agent_kind="h")
    run = asyncio.create_task(sup.run())
    await asyncio.sleep(0.3)
    assert ran == [] and sup.queue.get(task_id).state == "queued"

    control.set_mode(sup.queue._conn, "running", by="roshan")
    await _until(lambda: ran == [task_id])
    sup.stop()
    await asyncio.wait_for(run, timeout=5)
    sup.close()


@pytest.mark.asyncio
async def test_drain_finishes_running_work_starts_none_and_exits(tmp_path: Path) -> None:
    sup = _sup(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()
    ran: list[str] = []

    async def handler(task: Task, tools: ToolSession) -> dict:
        ran.append(task.id)
        started.set()
        await release.wait()
        return {}

    sup.register("h", handler)
    first = sup.queue.add_task("first", agent_kind="h")
    run = asyncio.create_task(sup.run())
    await asyncio.wait_for(started.wait(), 5)
    control.set_mode(sup.queue._conn, "draining", by="roshan")
    second = sup.queue.add_task("second", agent_kind="h")
    await asyncio.sleep(0.2)
    assert not run.done(), "must wait for the running task"
    release.set()
    await asyncio.wait_for(run, timeout=5)
    assert ran == [first]
    assert sup.queue.get(first).state == "succeeded"
    assert sup.queue.get(second).state == "queued"
    sup.close()


@pytest.mark.safety
@pytest.mark.asyncio
async def test_stop_from_the_database_leaves_no_later_tool_effect(tmp_path: Path) -> None:
    sup = _sup(tmp_path, stop_grace_seconds=0.2)
    seen: list[bool] = []

    async def poller(task: Task, tools: ToolSession) -> dict:
        while True:
            seen.append(tools.submit("fs.list").ok)
            await asyncio.sleep(0.02)

    sup.register("poll", poller, tools={"fs.list"})
    sup.queue.add_task("poll", agent_kind="poll")
    run = asyncio.create_task(sup.run())
    await _until(lambda: seen)
    # A different connection, as a person at another terminal would use.
    other = TaskQueue(tmp_path / "lab.db", owner="cli")
    control.set_mode(other._conn, "stopped", by="roshan", reason="runaway")
    other.close()
    await asyncio.wait_for(run, timeout=10)
    cut = len(seen)
    await asyncio.sleep(0.2)
    assert len(seen) == cut, "the handler kept running after the stop"
    assert all(seen[i] is False for i in range(_first_denied(seen), len(seen)))
    sup.close()


def _first_denied(seen: list[bool]) -> int:
    for i, ok in enumerate(seen):
        if not ok:
            return i
    return len(seen)


def test_cancel_refuses_a_running_task(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "lab.db"
    with TaskQueue(db, owner="t") as q:
        queued = q.add_task("waiting")
        running = q.add_task("running")
        assert q.lease() is not None      # highest priority first: 'waiting'
        # Force a deterministic split: mark one running directly.
        q._conn.execute("UPDATE tasks SET state = 'running' WHERE id = ?", (running,))
    assert main(["--db", str(db), "cancel", running, "--by", "roshan"]) == 1
    assert "control stop" in capsys.readouterr().err
    with TaskQueue(db, owner="t") as q:
        q._conn.execute("UPDATE tasks SET state = 'queued' WHERE id = ?", (queued,))
    assert main(["--db", str(db), "cancel", queued, "--by", "roshan"]) == 0
    with TaskQueue(db, owner="t") as q:
        assert q.get(queued).state == "cancelled"


def test_cli_control_and_status(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "lab.db"
    TaskQueue(db, owner="t").close()
    assert main(["--db", str(db), "control", "pause", "--by", "roshan",
                 "--reason", "look at\x1b[31m this"]) == 0
    capsys.readouterr()
    assert main(["--db", str(db), "control", "show"]) == 0
    out = capsys.readouterr().out
    assert "paused" in out and "\x1b" not in out
    main(["--db", str(db), "status"])
    assert "paused" in capsys.readouterr().out
    assert main(["--db", str(db), "control", "resume", "--by", "roshan"]) == 0
    with TaskQueue(db, owner="t") as q:
        assert control.get(q._conn).mode == "running"


# ------------------------------------------------- signed resume (H3)


def _keys(tmp_path: Path):
    from lab import operator as op
    private_path, public_path = op.generate(tmp_path / "keys")
    return op.load_private(private_path), public_path


@pytest.mark.safety
@pytest.mark.asyncio
async def test_an_unsigned_resume_is_ignored_when_an_operator_key_is_configured(
        tmp_path: Path) -> None:
    private, public = _keys(tmp_path)
    sup = _sup(tmp_path, operator_public_key=public)
    ran: list[str] = []

    async def handler(task: Task, tools: ToolSession) -> dict:
        ran.append(task.id)
        return {}

    sup.register("h", handler)
    control.set_mode(sup.queue._conn, "paused", by="roshan")
    task_id = sup.queue.add_task("t", agent_kind="h")
    run = asyncio.create_task(sup.run())
    # The agent account writes the row directly, with no signature.
    sup.queue._conn.execute("UPDATE control SET mode = 'running', generation = generation + 1")
    await asyncio.sleep(0.3)
    assert ran == [], "an unsigned resume must not start work"

    control.set_mode(sup.queue._conn, "running", by="roshan", signer=private)
    await _until(lambda: ran == [task_id])
    sup.stop()
    await asyncio.wait_for(run, 5)
    sup.close()


@pytest.mark.safety
def test_a_signature_from_another_key_or_another_generation_is_not_a_resume(
        tmp_path: Path) -> None:
    from lab import operator as op
    private, public = _keys(tmp_path)
    other_path, _ = op.generate(tmp_path / "other")
    other = op.load_private(other_path)
    pub = op.load_public(public)
    with TaskQueue(tmp_path / "lab.db", owner="t") as q:
        control.set_mode(q._conn, "paused", by="a")
        control.set_mode(q._conn, "running", by="a", signer=other)
        assert control.effective(q._conn, pub).mode == "paused"      # wrong key

        control.set_mode(q._conn, "running", by="a", signer=private)
        good = control.get(q._conn)
        assert control.effective(q._conn, pub).mode == "running"

        # replay: the same signature copied onto a later generation
        control.set_mode(q._conn, "paused", by="a")
        q._conn.execute("UPDATE control SET mode = 'running', signature = ?, "
                        "generation = generation + 1", (good.signature,))
        assert control.effective(q._conn, pub).mode == "paused"


def test_without_an_operator_key_resume_works_as_before(tmp_path: Path) -> None:
    with TaskQueue(tmp_path / "lab.db", owner="t") as q:
        control.set_mode(q._conn, "paused", by="a")
        control.set_mode(q._conn, "running", by="a")
        assert control.effective(q._conn, None).mode == "running"


def test_cli_resume_signs_with_the_operator_key(tmp_path: Path,
                                                capsys: pytest.CaptureFixture[str]) -> None:
    from lab import operator as op
    private_path, public_path = op.generate(tmp_path / "keys")
    db = tmp_path / "lab.db"
    TaskQueue(db, owner="t").close()
    main(["--db", str(db), "control", "pause", "--by", "roshan"])
    assert main(["--db", str(db), "control", "resume", "--by", "roshan",
                 "--key", str(private_path)]) == 0
    capsys.readouterr()
    with TaskQueue(db, owner="t") as q:
        assert control.effective(q._conn, op.load_public(public_path)).mode == "running"
