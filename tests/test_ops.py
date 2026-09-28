"""Queue-aware sleep prevention and rotating structured logs (H5a, checklist section 5)."""

from __future__ import annotations

import json
import logging
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lab import keepawake, logsetup
from lab.cli import main
from lab.queue import TaskQueue

NOW = datetime(2026, 9, 29, 12, 0, 0, tzinfo=UTC)


@pytest.fixture()
def q(tmp_path: Path) -> TaskQueue:
    with TaskQueue(tmp_path / "lab.db", owner="t") as queue:
        yield queue


def test_pending_work_holds_the_machine_awake(q: TaskQueue) -> None:
    q.add_task("work")
    decision = keepawake.decide(q._conn, grace_seconds=600, now=datetime.now(UTC))
    assert decision.hold and "queued" in decision.reason


def test_an_empty_queue_releases_only_after_the_grace_period(q: TaskQueue) -> None:
    q.record_event(None, "tick")
    just_after = datetime.now(UTC)
    assert keepawake.decide(q._conn, 600, just_after).hold, "recent activity holds"
    later = just_after + timedelta(seconds=601)
    decision = keepawake.decide(q._conn, 600, later)
    assert not decision.hold and "idle" in decision.reason


def test_waiting_for_a_person_does_not_hold_the_machine(q: TaskQueue) -> None:
    task = q.get(q.add_task("send", capability_tier="approve"))
    from lab.policy import PolicyEngine
    PolicyEngine(q._conn).request_approval(task, "needs a person")
    q._conn.execute("UPDATE tasks SET state = 'awaiting_approval'")
    later = datetime.now(UTC) + timedelta(hours=2)
    assert not keepawake.decide(q._conn, 600, later).hold


def test_a_paused_lab_is_not_held_awake_for_queued_work(q: TaskQueue) -> None:
    from lab import control
    q.add_task("work")
    control.set_mode(q._conn, "paused", by="a")
    later = datetime.now(UTC) + timedelta(hours=2)
    assert not keepawake.decide(q._conn, 600, later).hold


class FakeProc:
    def __init__(self) -> None:
        self.running = True

    def terminate(self) -> None:
        self.running = False

    def poll(self) -> int | None:
        return None if self.running else 0


def test_the_holder_starts_one_caffeinate_and_releases_it(q: TaskQueue) -> None:
    spawned: list[FakeProc] = []

    def spawn() -> FakeProc:
        spawned.append(FakeProc())
        return spawned[-1]

    holder = keepawake.Holder(spawn)
    busy = keepawake.Decision(True, "1 queued")
    idle = keepawake.Decision(False, "idle")
    holder.apply(busy)
    holder.apply(busy)
    assert len(spawned) == 1, "never a second holder while one is running"
    holder.apply(idle)
    assert not spawned[0].running
    holder.apply(busy)
    assert len(spawned) == 2
    holder.close()
    assert not spawned[1].running


def test_a_holder_that_died_is_replaced() -> None:
    spawned: list[FakeProc] = []

    def spawn() -> FakeProc:
        spawned.append(FakeProc())
        return spawned[-1]

    holder = keepawake.Holder(spawn)
    holder.apply(keepawake.Decision(True, "x"))
    spawned[0].running = False
    holder.apply(keepawake.Decision(True, "x"))
    assert len(spawned) == 2


def test_cli_keepawake_once_prints_and_exits_by_decision(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "lab.db"
    with TaskQueue(db, owner="t") as queue:
        queue.add_task("work")
    assert main(["--db", str(db), "keepawake", "--once"]) == 0
    assert capsys.readouterr().out.startswith("hold")


# ------------------------------------------------------------------ logging


def _read(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_logs_are_json_lines_with_fixed_keys(tmp_path: Path) -> None:
    handler = logsetup.configure(tmp_path / "logs", name="t1")
    log = logging.getLogger("lab.test1")
    log.warning("hello %s", "world")
    handler.flush()
    (record,) = _read(tmp_path / "logs" / "t1.log")
    assert set(record) == {"ts", "level", "logger", "msg"}
    assert record["msg"] == "hello world" and record["level"] == "WARNING"
    logsetup.teardown(handler)


@pytest.mark.safety
def test_a_newline_or_escape_in_a_message_cannot_forge_a_second_record(tmp_path: Path) -> None:
    handler = logsetup.configure(tmp_path / "logs", name="t2")
    logging.getLogger("lab.test2").error(
        'task failed\n{"ts":"x","level":"INFO","logger":"lab","msg":"all clear"}\x1b[2J')
    handler.flush()
    lines = (tmp_path / "logs" / "t2.log").read_text().splitlines()
    assert len(lines) == 1
    assert "\x1b" not in lines[0]
    logsetup.teardown(handler)


def test_the_log_rotates_and_keeps_a_bounded_number_of_files(tmp_path: Path) -> None:
    handler = logsetup.configure(tmp_path / "logs", name="t3", max_bytes=2000, backups=2)
    log = logging.getLogger("lab.test3")
    for i in range(200):
        log.warning("line %d %s", i, "x" * 50)
    handler.flush()
    files = sorted(p.name for p in (tmp_path / "logs").iterdir())
    assert files == ["t3.log", "t3.log.1", "t3.log.2"]
    logsetup.teardown(handler)


def test_log_files_are_private(tmp_path: Path) -> None:
    handler = logsetup.configure(tmp_path / "logs", name="t4")
    logging.getLogger("lab.test4").warning("x")
    handler.flush()
    mode = stat.S_IMODE((tmp_path / "logs" / "t4.log").stat().st_mode)
    assert mode & 0o077 == 0
    logsetup.teardown(handler)
