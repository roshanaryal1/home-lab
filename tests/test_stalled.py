"""A task whose lease keeps renewing but which writes no event (#376).

``lab status`` reports such a task as attention. The check never cancels,
kills or requeues anything, and it only reads the database.

Time is a fake clock. The queue reads ``lab.queue._utcnow`` for lease expiry
and the audit log reads ``lab.audit._now`` for event times, so the tests
replace both. Every event and expiry then lands at a minute the test chose.
The clock starts two hours in the past, so a long lease TTL stays valid
against the real clock that the SQL lease checks use.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lab import audit, dashboard, metrics
from lab import queue as queue_mod
from lab.cli import main
from lab.queue import LeaseToken, TaskQueue

TTL = 10 * 3600


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.start = start
        self.now = start

    def at(self, minutes: float) -> datetime:
        return self.start + timedelta(minutes=minutes)

    def move_to(self, minutes: float) -> None:
        self.now = self.at(minutes)

    def utcnow(self) -> datetime:
        return self.now

    def stamp(self) -> str:
        return f"{self.now.strftime('%Y-%m-%d %H:%M:%S')}.{self.now.microsecond // 1000:03d}"


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock(datetime.now(UTC).replace(microsecond=0) - timedelta(hours=2))
    monkeypatch.setattr(queue_mod, "_utcnow", fake.utcnow)
    monkeypatch.setattr(audit, "_now", fake.stamp)
    return fake


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "lab.db"


@pytest.fixture()
def q(db_path: Path, clock: FakeClock) -> Iterator[TaskQueue]:
    with TaskQueue(db_path, owner="test") as queue:
        yield queue


def take(q: TaskQueue, *, kind: str = "coder") -> tuple[str, LeaseToken]:
    task_id = q.add_task("work", agent_kind=kind)
    task = q.lease(ttl_seconds=TTL)
    assert task is not None and task.lease is not None and task.id == task_id
    return task_id, task.lease


def renew_at(q: TaskQueue, clock: FakeClock, token: LeaseToken, minutes: float) -> None:
    clock.move_to(minutes)
    assert q.renew_lease(token, ttl_seconds=TTL) is True


def stalled_at(clock: FakeClock, q: TaskQueue, minutes: float,
               **kwargs: float) -> list[metrics.Stalled]:
    return metrics.stalled_tasks(q._conn, clock.at(minutes), **kwargs)


def dump(path: Path) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return list(conn.iterdump())
    finally:
        conn.close()


# ------------------------------------------------------------ the rule


def test_a_fresh_event_means_the_task_is_not_stalled(q: TaskQueue, clock: FakeClock) -> None:
    task_id, token = take(q)
    renew_at(q, clock, token, 5)
    clock.move_to(20)
    q.start(token)
    assert stalled_at(clock, q, 45) == [], "25 minutes since the start event"
    clock.move_to(40)
    q.record_event(task_id, "heartbeat")
    assert stalled_at(clock, q, 65) == [], "25 minutes since the heartbeat"


def test_a_renewed_lease_with_no_event_past_the_limit_is_stalled(
        q: TaskQueue, clock: FakeClock) -> None:
    task_id, token = take(q, kind="researcher")
    clock.move_to(1)
    q.start(token)
    renew_at(q, clock, token, 10)
    renew_at(q, clock, token, 20)
    assert stalled_at(clock, q, 40) == [
        metrics.Stalled(task_id, "researcher", "running", 39.0)]


def test_an_expired_lease_is_not_stalled(q: TaskQueue, clock: FakeClock) -> None:
    # Renewed once, then the worker stopped renewing: by minute 40 the lease has
    # expired, which the health check reports, so it is not counted as stalled.
    _, token = take(q)
    clock.move_to(1)
    q.start(token)
    renew_at(q, clock, token, 5)
    assert stalled_at(clock, q, 5 + TTL / 60 + 1) == []


def test_a_leased_task_whose_lease_renews_is_stalled_too(q: TaskQueue, clock: FakeClock) -> None:
    task_id, token = take(q)
    renew_at(q, clock, token, 10)
    assert stalled_at(clock, q, 40) == [metrics.Stalled(task_id, "coder", "leased", 40.0)]


def test_the_limit_is_thirty_minutes_inclusive_and_can_be_changed(
        q: TaskQueue, clock: FakeClock) -> None:
    task_id, token = take(q)
    renew_at(q, clock, token, 1)
    assert stalled_at(clock, q, 29.5) == [], "under the limit"
    assert [t.task_id for t in stalled_at(clock, q, 30)] == [task_id], "exactly the limit"
    assert stalled_at(clock, q, 30, quiet_minutes=60) == [], "a longer limit"


@pytest.mark.parametrize("finish", ["succeeded", "failed", "cancelled"])
def test_a_finished_task_is_not_stalled(q: TaskQueue, clock: FakeClock, finish: str) -> None:
    task_id, token = take(q)
    renew_at(q, clock, token, 5)
    if finish == "cancelled":
        q.cancel(task_id)
    else:
        clock.move_to(6)
        q.start(token)
        if finish == "succeeded":
            q.succeed(token, {})
        else:
            q.fail(token, "boom", retry=False)
    task = q.get(task_id)
    assert task is not None and task.state == finish
    assert stalled_at(clock, q, 120) == []


@pytest.mark.parametrize("started", [False, True])
def test_a_lease_that_was_never_renewed_is_not_stalled(
        q: TaskQueue, clock: FakeClock, started: bool) -> None:
    _, token = take(q)
    if started:
        clock.move_to(1)
        q.start(token)
    assert stalled_at(clock, q, 60) == [], "no renewal, so nothing is shown to be stuck"


def test_only_the_current_lease_counts(q: TaskQueue, clock: FakeClock) -> None:
    task_id, first = take(q)
    renew_at(q, clock, first, 5)
    q.release_unstarted(first, "no worker free")
    clock.move_to(10)
    task = q.lease(ttl_seconds=TTL)
    assert task is not None and task.id == task_id and task.lease is not None
    assert stalled_at(clock, q, 90) == [], "the new lease was never renewed"


def test_a_lease_whose_original_expiry_cannot_be_read_is_not_reported(
        db_path: Path, q: TaskQueue, clock: FakeClock) -> None:
    task_id, token = take(q)
    clock.move_to(1)
    q.start(token)
    renew_at(q, clock, token, 5)
    assert [t.task_id for t in stalled_at(clock, q, 60)] == [task_id], "control"
    raw = sqlite3.connect(db_path, isolation_level=None)
    raw.execute("DROP TRIGGER events_no_update")
    raw.execute("UPDATE events SET detail = '{}' WHERE task_id = ? AND kind = 'leased'",
                (task_id,))
    raw.close()
    assert stalled_at(clock, q, 60) == [], "the renewal cannot be shown, so it is not flagged"


# ------------------------------------------------------- read-only check


def test_the_check_changes_nothing_in_the_database(
        db_path: Path, q: TaskQueue, clock: FakeClock) -> None:
    task_id, token = take(q)
    clock.move_to(1)
    q.start(token)
    renew_at(q, clock, token, 5)
    before = dump(db_path)
    readonly = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        flagged = metrics.stalled_tasks(readonly, clock.at(60))
        metrics.collect(readonly, now=clock.at(60))
    finally:
        readonly.close()
    assert [t.task_id for t in flagged] == [task_id]
    assert dump(db_path) == before
    task = q.get(task_id)
    assert task is not None and task.state == "running", "still running"
    assert q.owns_lease(token), "and the lease is still held"


# ------------------------------------------------- where it is reported


def _recorder(tmp_path: Path) -> tuple[Path, Path]:
    """A notifier that records the alert kind and the message it gets on stdin."""
    out = tmp_path / "recorded.jsonl"
    script = tmp_path / "notify.py"
    script.write_text(
        "import json, os, sys\n"
        f"OUT = {str(out)!r}\n"
        "with open(OUT, 'a') as handle:\n"
        "    handle.write(json.dumps({'kind': os.environ.get('LAB_ALERT_KIND'),\n"
        "                             'stdin': sys.stdin.read()}) + '\\n')\n")
    script.chmod(0o700)
    return script, out


def _alert_config(tmp_path: Path, script: Path) -> Path:
    path = tmp_path / "alert.json"
    path.write_text(json.dumps({"command": [sys.executable, str(script)]}))
    path.chmod(0o600)
    return path


def test_status_names_the_stalled_task_as_attention_and_keeps_the_exit_code(
        db_path: Path, q: TaskQueue, clock: FakeClock,
        capsys: pytest.CaptureFixture[str]) -> None:
    task_id, token = take(q)
    clock.move_to(1)
    q.start(token)
    renew_at(q, clock, token, 10)
    assert main(["--db", str(db_path), "status"]) == 0, "attention is not unhealthy"
    out = capsys.readouterr().out
    assert "health   ATTENTION" in out and task_id in out and "stalled" in out
    assert main(["--db", str(db_path), "status", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [t["task_id"] for t in data["stalled"]] == [task_id]
    assert data["stalled"][0]["state"] == "running" and data["stalled_minutes"] == 30.0
    assert main(["--db", str(db_path), "status", "--stalled-minutes", "100000"]) == 0
    assert "stalled" not in capsys.readouterr().out, "a longer limit reports nothing"


def test_the_stalled_alert_names_the_task_and_has_its_own_kind(
        db_path: Path, q: TaskQueue, clock: FakeClock, tmp_path: Path) -> None:
    task_id, token = take(q)
    clock.move_to(1)
    q.start(token)
    renew_at(q, clock, token, 10)
    script, out = _recorder(tmp_path)
    cfg = _alert_config(tmp_path, script)
    assert main(["--db", str(db_path), "status", "--alert-config", str(cfg)]) == 0
    records = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(records) == 1 and records[0]["kind"] == "stalled"
    assert task_id in records[0]["stdin"] and "though its lease was renewed" in records[0]["stdin"]


def test_the_status_page_lists_the_stalled_task(
        db_path: Path, q: TaskQueue, clock: FakeClock) -> None:
    task_id, token = take(q)
    clock.move_to(1)
    q.start(token)
    renew_at(q, clock, token, 10)
    page = dashboard.render_page(db_path)
    assert "<h2>Stalled</h2>" in page and task_id in page
    assert task_id in dashboard.render_json(db_path)


def test_a_status_line_cannot_carry_terminal_control_characters(
        db_path: Path, q: TaskQueue, clock: FakeClock,
        capsys: pytest.CaptureFixture[str]) -> None:
    _, token = take(q, kind="coder\x1b[2J")
    clock.move_to(1)
    q.start(token)
    renew_at(q, clock, token, 10)
    assert main(["--db", str(db_path), "status"]) == 0
    out = capsys.readouterr().out
    assert "coder[2J" in out and "\x1b" not in out
