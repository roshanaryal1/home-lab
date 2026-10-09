"""Owner-signed schedules (#361, feature 5).

A schedule may start work and may never approve it. The safety tests are the
draft claim S1: an unsigned, altered or replayed schedule never creates a
task, and a task a schedule creates never reaches an approve-tier action
without the owner's signature.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from lab import loop, schedule
from lab import operator as operator_keys
from lab.cli import main
from lab.model import BoundedModel, MockAdapter, ModelSpec
from lab.policy import Decision, PolicyEngine
from lab.queue import TaskQueue

T0 = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
NZ = ZoneInfo("Pacific/Auckland")


@pytest.fixture()
def keys(tmp_path: Path) -> tuple[Path, Path]:
    return operator_keys.generate(tmp_path / "keys")


@pytest.fixture()
def queue(tmp_path: Path):
    with TaskQueue(tmp_path / "lab.db") as q:
        yield q


def _add(q: TaskQueue, private: Path | None, name: str = "digest", rule: str = "daily 07:30",
         **kwargs: object) -> int:
    signer = operator_keys.load_private(private) if private is not None else None
    options: dict[str, object] = {"kind": "git.read", "title": "Morning digest", "by": "owner",
                                  "now": T0}
    options.update(kwargs)
    return schedule.add(q._conn, name, rule, signer=signer, **options)  # type: ignore[arg-type]


def _tasks(q: TaskQueue) -> list[sqlite3.Row]:
    return list(q._conn.execute("SELECT id, state, origin_type, origin_id, tainted, "
                                "capability_tier FROM tasks ORDER BY created_at"))


def _events(q: TaskQueue, kind: str) -> int:
    return int(q._conn.execute("SELECT COUNT(*) FROM events WHERE kind = ?",
                               (kind,)).fetchone()[0])


def _close(q: TaskQueue, task_id: str) -> None:
    q._conn.execute("UPDATE tasks SET state = 'succeeded' WHERE id = ?", (task_id,))


# ------------------------------------------------------------ rules


@pytest.mark.parametrize("text", ["daily 07:30", "weekly mon 09:00", "every 15 minutes",
                                  "every 1440 minutes", "DAILY 23:59"])
def test_good_rules_parse(text: str) -> None:
    schedule.parse_rule(text)


@pytest.mark.parametrize("text", ["daily 24:00", "daily 7:30", "weekly funday 09:00",
                                  "every 14 minutes", "every 1441 minutes", "hourly", ""])
def test_bad_rules_are_refused(text: str) -> None:
    with pytest.raises(schedule.ScheduleError):
        schedule.parse_rule(text)


def test_a_daily_slot_keeps_its_wall_clock_time_across_daylight_saving() -> None:
    # New Zealand moved its clocks forward on 2026-09-27 at 02:00.
    rule = schedule.parse_rule("daily 07:30")
    before = datetime(2026, 9, 25, 19, 30, tzinfo=UTC)       # 26 Sep 07:30 NZST
    first = schedule.next_slot(rule, NZ, before, anchor=T0 - timedelta(days=30))
    second = schedule.next_slot(rule, NZ, first, anchor=T0 - timedelta(days=30))
    assert first.astimezone(NZ).strftime("%m-%d %H:%M") == "09-27 07:30"
    assert second.astimezone(NZ).strftime("%m-%d %H:%M") == "09-28 07:30"
    assert first - before == timedelta(hours=23)
    assert second - first == timedelta(hours=24)


def test_a_time_the_clocks_skip_runs_an_hour_later_and_a_repeated_time_runs_once() -> None:
    rule = schedule.parse_rule("daily 02:30")
    anchor = T0 - timedelta(days=200)
    forward = schedule.next_slot(rule, NZ, datetime(2026, 9, 26, 0, 0, tzinfo=UTC),
                                 anchor=anchor)
    assert forward.astimezone(NZ).strftime("%m-%d %H:%M") == "09-27 03:30"
    back = schedule.next_slot(rule, NZ, datetime(2026, 4, 4, 0, 0, tzinfo=UTC), anchor=anchor)
    after_back = schedule.next_slot(rule, NZ, back, anchor=anchor)
    assert back.astimezone(NZ).strftime("%m-%d %H:%M %z") == "04-05 02:30 +1300"
    assert after_back.astimezone(NZ).strftime("%m-%d %H:%M") == "04-06 02:30"


def test_a_weekly_slot_is_strictly_after_the_moment_given() -> None:
    rule = schedule.parse_rule("weekly thu 00:00")
    after = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)          # a Thursday, 00:00
    assert schedule.next_slot(rule, ZoneInfo("UTC"), after, anchor=T0) == after + timedelta(days=7)


def test_an_every_rule_counts_from_when_the_schedule_was_made() -> None:
    rule = schedule.parse_rule("every 15 minutes")
    anchor = datetime(2026, 10, 1, 0, 7, tzinfo=UTC)
    after = anchor + timedelta(minutes=16)
    assert schedule.next_slot(rule, ZoneInfo("UTC"), after, anchor=anchor) == (
        anchor + timedelta(minutes=30))


# ------------------------------------------------------------ firing


def test_a_signed_schedule_fires_once_when_due(queue: TaskQueue, keys: tuple[Path, Path]) -> None:
    private, public = keys
    _add(queue, private, rule="daily 07:30")
    key = operator_keys.load_public(public)
    due = T0 + timedelta(hours=8)
    fired = schedule.fire_due(queue, due, key)
    assert [f.outcome for f in fired] == ["fired"]
    assert schedule.fire_due(queue, due, key) == []
    tasks = _tasks(queue)
    assert len(tasks) == 1
    assert (tasks[0]["origin_type"], tasks[0]["origin_id"], tasks[0]["tainted"]) == (
        "operator", "schedule:digest", 0)
    assert _events(queue, "schedule_fired") == 1


def test_nothing_fires_before_the_slot(queue: TaskQueue, keys: tuple[Path, Path]) -> None:
    private, public = keys
    _add(queue, private, rule="daily 07:30")
    key = operator_keys.load_public(public)
    assert schedule.fire_due(queue, T0 + timedelta(hours=7), key) == []
    assert _tasks(queue) == []


def test_missed_slots_fire_once_not_once_per_slot(queue: TaskQueue,
                                                   keys: tuple[Path, Path]) -> None:
    private, public = keys
    _add(queue, private, rule="daily 07:30")
    later = T0 + timedelta(days=5, hours=9)
    assert [f.outcome for f in schedule.fire_due(queue, later, operator_keys.load_public(public))] \
        == ["fired"]
    row = schedule.live(queue._conn)[0]
    assert schedule._parse_stamp(row["next_due_at"]) > later
    assert len(_tasks(queue)) == 1


def test_a_slot_is_skipped_while_the_last_task_is_open(queue: TaskQueue,
                                                       keys: tuple[Path, Path]) -> None:
    private, public = keys
    key = operator_keys.load_public(public)
    _add(queue, private, rule="daily 07:30")
    schedule.fire_due(queue, T0 + timedelta(hours=8), key)
    skipped = schedule.fire_due(queue, T0 + timedelta(days=1, hours=8), key)
    assert [f.outcome for f in skipped] == ["skipped"]
    assert len(_tasks(queue)) == 1
    _close(queue, _tasks(queue)[0]["id"])
    again = schedule.fire_due(queue, T0 + timedelta(days=2, hours=8), key)
    assert [f.outcome for f in again] == ["fired"]
    assert len(_tasks(queue)) == 2


@pytest.mark.safety
def test_an_unsigned_schedule_never_creates_a_task(queue: TaskQueue,
                                                   keys: tuple[Path, Path]) -> None:
    _, public = keys
    _add(queue, None, rule="daily 07:30")
    fired = schedule.fire_due(queue, T0 + timedelta(hours=8), operator_keys.load_public(public))
    assert [(f.outcome, f.reason) for f in fired] == [("refused", "signature")]
    assert _tasks(queue) == []
    assert _events(queue, "schedule_refused") == 1


@pytest.mark.safety
@pytest.mark.parametrize("column, value", [
    ("title", "Delete everything"),
    ("payload", '{"tools": ["fs.delete"]}'),
    ("capability_tier", "notify"),
    ("rule", "every 15 minutes"),
    ("tz", "Asia/Tokyo"),
    ("agent_kind", "skill.run"),
])
def test_a_schedule_altered_after_signing_never_creates_a_task(
        queue: TaskQueue, keys: tuple[Path, Path], column: str, value: str) -> None:
    private, public = keys
    _add(queue, private, rule="daily 07:30", tier="approve")
    queue._conn.execute(f"UPDATE schedules SET {column} = ?", (value,))  # nosemgrep
    fired = schedule.fire_due(queue, T0 + timedelta(days=1), operator_keys.load_public(public))
    assert [f.outcome for f in fired] == ["refused"]
    assert _tasks(queue) == []


@pytest.mark.safety
def test_a_signature_copied_to_another_schedule_does_not_verify(
        queue: TaskQueue, keys: tuple[Path, Path]) -> None:
    private, public = keys
    _add(queue, private, name="digest", rule="daily 07:30")
    _add(queue, None, name="cleanup", rule="daily 07:30", title="Clean up", tier="approve")
    queue._conn.execute(
        "UPDATE schedules SET signature = (SELECT signature FROM schedules WHERE name = 'digest') "
        "WHERE name = 'cleanup'")
    fired = schedule.fire_due(queue, T0 + timedelta(hours=8), operator_keys.load_public(public))
    assert sorted((f.name, f.outcome) for f in fired) == [("cleanup", "refused"),
                                                         ("digest", "fired")]
    assert [t["origin_id"] for t in _tasks(queue)] == ["schedule:digest"]


@pytest.mark.safety
def test_a_removed_schedule_never_fires_and_its_signed_row_cannot_come_back(
        queue: TaskQueue, keys: tuple[Path, Path]) -> None:
    private, public = keys
    _add(queue, private, rule="daily 07:30")
    schedule.remove(queue._conn, "digest", by="owner", now=T0)
    assert schedule.fire_due(queue, T0 + timedelta(days=3), operator_keys.load_public(public)) == []
    assert _tasks(queue) == []
    old = queue._conn.execute("SELECT * FROM schedules").fetchone()
    with pytest.raises(sqlite3.IntegrityError):
        queue._conn.execute(
            "INSERT INTO schedules (name, rule, tz, agent_kind, title, payload, weight, "
            "capability_tier, created_by, created_at, nonce, signature, next_due_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (old["name"], old["rule"], old["tz"], old["agent_kind"], old["title"],
             old["payload"], old["weight"], old["capability_tier"], old["created_by"],
             old["created_at"], old["nonce"], old["signature"], old["next_due_at"]))


@pytest.mark.safety
def test_a_scheduled_approve_tier_task_still_waits_for_the_owner(
        queue: TaskQueue, keys: tuple[Path, Path]) -> None:
    private, public = keys
    _add(queue, private, rule="daily 07:30", tier="approve")
    fired = schedule.fire_due(queue, T0 + timedelta(hours=8), operator_keys.load_public(public))
    task = queue.get(fired[0].task_id or "")
    assert task is not None and task.capability_tier == "approve"
    result = PolicyEngine(queue._conn).authorize(task)
    assert result.decision is Decision.NEEDS_APPROVAL


def test_without_an_operator_key_a_schedule_fires_unsigned(queue: TaskQueue) -> None:
    _add(queue, None, rule="daily 07:30")
    assert [f.outcome for f in schedule.fire_due(queue, T0 + timedelta(hours=8), None)] == ["fired"]


def test_a_second_live_schedule_with_the_same_name_is_refused(
        queue: TaskQueue, keys: tuple[Path, Path]) -> None:
    private, _ = keys
    _add(queue, private)
    with pytest.raises(schedule.ScheduleError, match="already exists"):
        _add(queue, private)
    schedule.remove(queue._conn, "digest", by="owner")
    _add(queue, private)       # a removed schedule frees its name


@pytest.mark.parametrize("kwargs, message", [
    ({"tz": "Mars/Olympus"}, "time zone"),
    ({"tier": "never"}, "tier"),
    ({"weight": "medium"}, "weight"),
    ({"kind": "Git Read"}, "task kind"),
])
def test_bad_options_are_refused(queue: TaskQueue, keys: tuple[Path, Path],
                                 kwargs: dict[str, str], message: str) -> None:
    private, _ = keys
    with pytest.raises(schedule.ScheduleError, match=message):
        _add(queue, private, **kwargs)


# ------------------------------------------------------------ tick


@pytest.mark.asyncio
async def test_tick_fires_a_due_schedule_and_its_approve_tier_task_parks(tmp_path: Path) -> None:
    db = tmp_path / "lab.db"
    past = datetime.now(UTC) - timedelta(days=2)
    with TaskQueue(db, owner="owner") as q:
        # The tick registers the proposal summarizer, so a proposal-kind
        # schedule is leased in the same pass and parks at the policy check.
        schedule.add(q._conn, "weekly-check", "every 15 minutes", kind=loop.PROPOSAL_KIND,
                     title="Check the backups", tier="approve", by="owner", signer=None,
                     now=past)
    rev = "a" * 40
    model = BoundedModel(ModelSpec("summarizer", rev, rev, context_tokens=8192,
                                   max_output_tokens=512, weights_mb=1000, heavy=False),
                         MockAdapter(['{"summary": "nothing"}']))
    report = await loop.tick(db, model)
    assert report.scheduled == 1
    with TaskQueue(db, owner="check") as q:
        rows = _tasks(q)
    assert len(rows) == 1 and rows[0]["state"] == "awaiting_approval"


# ------------------------------------------------------------ CLI


def test_the_cli_adds_lists_and_removes_a_signed_schedule(
        tmp_path: Path, keys: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    private, _ = keys
    db = tmp_path / "lab.db"
    TaskQueue(db).close()
    assert main(["--db", str(db), "schedule", "add", "digest", "--daily", "07:30",
                 "--tz", "Pacific/Auckland", "--kind", "git.read", "--title", "Morning digest",
                 "--key", str(private), "--by", "owner"]) == 0
    assert "added digest" in capsys.readouterr().out
    assert main(["--db", str(db), "schedule", "list"]) == 0
    listed = capsys.readouterr().out
    assert "digest" in listed and "git.read" in listed and "signed" in listed
    assert "UNSIGNED" not in listed
    assert main(["--db", str(db), "schedule", "remove", "digest", "--by", "owner"]) == 0
    assert main(["--db", str(db), "schedule", "list"]) == 0
    assert "no schedules" in capsys.readouterr().out


def test_the_cli_refuses_a_schedule_without_a_key(
        tmp_path: Path, capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LAB_OPERATOR_KEY", raising=False)
    db = tmp_path / "lab.db"
    TaskQueue(db).close()
    assert main(["--db", str(db), "schedule", "add", "digest", "--daily", "07:30",
                 "--kind", "git.read", "--title", "Morning digest", "--by", "owner"]) == 1
    assert "must be signed" in capsys.readouterr().err


def test_the_cli_reports_a_bad_rule_in_one_line(
        tmp_path: Path, keys: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    private, _ = keys
    db = tmp_path / "lab.db"
    TaskQueue(db).close()
    assert main(["--db", str(db), "schedule", "add", "digest", "--every-minutes", "5",
                 "--kind", "git.read", "--title", "Too often", "--key", str(private),
                 "--by", "owner"]) == 1
    assert "every N minutes needs N from 15" in capsys.readouterr().err
