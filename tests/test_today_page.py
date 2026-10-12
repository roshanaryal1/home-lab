"""The today page on the status page (#390).

``/today`` and ``/today.json`` show what happened on the machine's local day
so far: tasks created, finished, waiting for approval and refused by policy,
approvals decided, schedules fired, and egress and policy denial counts.

The page is read-only. It never shows a payload, a result, an error or an
approval's intent. Every test fixes the clock, so the day is the same on any
machine.
"""

from __future__ import annotations

import http.client
import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from lab import audit, dashboard, metrics, schedule
from lab.audit import append_event
from lab.policy import PolicyEngine
from lab.queue import TaskQueue

NZ = ZoneInfo("Pacific/Auckland")  # UTC+13 in October 2026 (daylight time)
# 22:00 on 9 October in Auckland, after every fixture time below. In UTC that is
# 09:00 on 9 October.
NOW = datetime(2026, 10, 9, 22, 0, tzinfo=NZ)
# The local day 9 October runs from 2026-10-08 11:00 UTC up to, but not
# including, 2026-10-09 11:00 UTC.
DAY_START = "2026-10-08 11:00:00"
DAY_END = "2026-10-09 11:00:00"
# Stored times are UTC text. These two are on 8 October and 9 October locally.
LOCAL_8_OCT = "2026-10-07 21:00:00"  # 10:00 on 8 October in Auckland
LOCAL_9_OCT = "2026-10-08 18:00:00"  # 07:00 on 9 October in Auckland

HOSTILE = '<script>alert("x")</script>&"\''
PAYLOAD = "PAYLOAD-SECRET-7f3a"
RESULT = "RESULT-BODY-9c1e"
ERROR = "ERROR-TEXT-4d2b"


def utc_text(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    path = tmp_path / "lab.db"
    with TaskQueue(path):
        pass
    return path


@pytest.fixture()
def q(db: Path) -> Iterator[TaskQueue]:
    with TaskQueue(db) as queue:
        yield queue


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    """Stamp every event written from now on with the given UTC time.

    ``lab.audit`` reads ``_now`` for each event, as test_stalled.py does.
    """
    def set_clock(utc: str) -> None:
        monkeypatch.setattr(audit, "_now", lambda: f"{utc}.000")
    return set_clock


# ------------------------------------------------------------------ helpers


def set_created(q: TaskQueue, task_id: str, utc: str) -> None:
    """Give a task the creation time it would have had on that day."""
    q._conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (utc, task_id))


def new_task(q: TaskQueue, clock: Callable[[str], None], title: str, *,
             created: str, kind: str = "digest") -> str:
    clock(created)
    task_id = q.add_task(title, {"token": PAYLOAD}, agent_kind=kind, max_attempts=1)
    set_created(q, task_id, created)
    return task_id


def finish(q: TaskQueue, clock: Callable[[str], None], title: str, *, created: str,
           finished: str, outcome: str = "succeeded") -> str:
    task_id = new_task(q, clock, title, created=created)
    clock(finished)
    if outcome == "cancelled":
        q.cancel(task_id, "stopped by the operator")
        return task_id
    leased = q.lease()
    assert leased is not None and leased.id == task_id and leased.lease is not None
    q.start(leased.lease)
    if outcome == "succeeded":
        q.succeed(leased.lease, {"body": RESULT})
    else:
        q.fail(leased.lease, ERROR)
    return task_id


def park(q: TaskQueue, clock: Callable[[str], None], title: str, *, created: str,
         parked: str) -> str:
    task_id = new_task(q, clock, title, created=created)
    clock(parked)
    leased = q.lease()
    assert leased is not None and leased.id == task_id and leased.lease is not None
    q.start(leased.lease)
    q.park_for_approval(leased.lease, "needs a person")
    return task_id


def record(q: TaskQueue, clock: Callable[[str], None], at: str, kind: str,
           task_id: str | None = None, detail: dict[str, object] | None = None) -> None:
    clock(at)
    append_event(q._conn, task_id, kind, detail=detail)


def decide(q: TaskQueue, clock: Callable[[str], None], task_id: str, decision: str, *,
           at: str) -> str:
    """Request an approval for a task, then grant or deny it at ``at``."""
    engine = PolicyEngine(q._conn)
    task = q.get(task_id)
    assert task is not None
    clock(at)
    approval_id = engine.request_approval(task, "send the monthly report")
    if decision == "granted":
        engine.grant(approval_id, decided_by="roshan")
    else:
        engine.deny(approval_id, decided_by="roshan")
    # decided_at is written by SQL with the real clock, so put the test's time in.
    q._conn.execute("UPDATE approvals SET decided_at = ? WHERE id = ?",
                    (f"{at}.000", approval_id))
    return approval_id


def fire_schedule(q: TaskQueue, clock: Callable[[str], None], *, at: str) -> str:
    """Add an unsigned daily schedule and fire it at ``at`` (UTC). Returns the task id."""
    schedule.add(q._conn, "digest", "daily 07:30", kind="git.read", title="Morning digest",
                 by="owner", signer=None, tz="UTC",
                 now=datetime(2026, 10, 7, 0, 0, tzinfo=UTC))
    clock(at)
    fired = schedule.fire_due(q, datetime(2026, 10, 8, 12, 30, tzinfo=UTC), None)
    assert [f.outcome for f in fired] == ["fired"]
    task_id = fired[0].task_id
    assert task_id is not None
    set_created(q, task_id, at)
    return task_id


def page_text(db: Path) -> str:
    return dashboard.render_today(db, now=NOW, tz=NZ)


def page_data(db: Path) -> dict[str, Any]:
    return json.loads(dashboard.render_today_json(db, now=NOW, tz=NZ))


@contextmanager
def serving(db: Path) -> Iterator[tuple[str, int]]:
    server = dashboard.make_server(db, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[0], server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def fetch(addr: tuple[str, int], path: str = "/", method: str = "GET",
          host: str | None = None) -> http.client.HTTPResponse:
    conn = http.client.HTTPConnection(addr[0], addr[1], timeout=5)
    conn.putrequest(method, path, skip_host=host is not None)
    if host is not None:
        conn.putheader("Host", host)
    conn.endheaders()
    return conn.getresponse()


# ------------------------------------------------------------- the local day


def test_the_local_day_is_the_day_in_the_zone_not_the_utc_day() -> None:
    day, start, end = metrics.local_day(NOW, NZ)
    assert day.isoformat() == "2026-10-09"
    assert utc_text(start) == DAY_START and utc_text(end) == DAY_END


def test_a_day_when_the_clocks_change_is_measured_in_real_hours() -> None:
    # Auckland's clocks went back at 03:00 on 5 April 2026, so that day has 25 hours.
    _, start, end = metrics.local_day(datetime(2026, 4, 5, 12, 0, tzinfo=NZ), NZ)
    assert end - start == timedelta(hours=25)


@pytest.fixture()
def machine_zone(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    if not hasattr(time, "tzset"):
        pytest.skip("this platform cannot change the process time zone")
    monkeypatch.setenv("TZ", "Pacific/Auckland")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_with_no_zone_given_the_machine_zone_sets_the_day(machine_zone: None) -> None:
    day, start, end = metrics.local_day(NOW)
    assert day.isoformat() == "2026-10-09"
    assert utc_text(start) == DAY_START and utc_text(end) == DAY_END
    _, start, end = metrics.local_day(datetime(2026, 4, 5, 12, 0, tzinfo=NZ))
    assert end - start == timedelta(hours=25)


# ------------------------------------------------------------- what is listed


def test_the_page_lists_a_task_created_and_finished_today_and_not_one_from_yesterday(
        q: TaskQueue, clock: Callable[[str], None], db: Path) -> None:
    old = finish(q, clock, "weekly tidy", created=LOCAL_8_OCT,
                 finished="2026-10-07 21:04:00")
    new = finish(q, clock, "morning digest", created=LOCAL_9_OCT,
                 finished="2026-10-08 18:03:00")
    page = page_text(db)
    assert "morning digest" in page and new in page
    assert "weekly tidy" not in page and old not in page
    data = page_data(db)
    assert [t["task_id"] for t in data["created"]] == [new]
    finished = data["finished"]
    assert [(t["task_id"], t["state"], t["kind"]) for t in finished] == [
        (new, "succeeded", "digest")]


def test_the_day_includes_its_first_second_and_excludes_the_one_after_its_last(
        q: TaskQueue, clock: Callable[[str], None], db: Path) -> None:
    edges = {
        "first": "2026-10-08 11:00:00",   # local midnight, 9 October: in
        "last": "2026-10-09 10:59:59",    # 23:59:59, 9 October: in
        "before": "2026-10-08 10:59:59",  # 23:59:59, 8 October: out
        "after": "2026-10-09 11:00:00",   # local midnight, 10 October: out
    }
    ids = {name: new_task(q, clock, name, created=utc) for name, utc in edges.items()}
    late = datetime(2026, 10, 9, 23, 59, 59, tzinfo=NZ)
    listed = {t["task_id"] for t in
              json.loads(dashboard.render_today_json(db, now=late, tz=NZ))["created"]}
    assert listed == {ids["first"], ids["last"]}


def test_nothing_stamped_after_now_is_shown(
        q: TaskQueue, clock: Callable[[str], None], db: Path) -> None:
    # NOW is 22:00 on 9 October in Auckland, which is 09:00 UTC on 9 October.
    early = new_task(q, clock, "before now", created="2026-10-09 08:59:00")
    new_task(q, clock, "after now", created="2026-10-09 09:30:00")
    assert [t["task_id"] for t in page_data(db)["created"]] == [early]


def test_a_task_parked_yesterday_and_still_waiting_is_listed_as_waiting(
        q: TaskQueue, clock: Callable[[str], None], db: Path) -> None:
    waiting = park(q, clock, "send the invoice", created=LOCAL_8_OCT,
                   parked="2026-10-07 21:01:00")
    data = page_data(db)
    assert [t["task_id"] for t in data["awaiting_approval"]] == [waiting]


def test_tasks_refused_by_policy_are_listed_and_counted(
        q: TaskQueue, clock: Callable[[str], None], db: Path) -> None:
    refused = [
        finish(q, clock, "publish a post", created=LOCAL_9_OCT,
               finished="2026-10-08 19:00:00", outcome="cancelled"),
        finish(q, clock, "email a friend", created=LOCAL_9_OCT,
               finished="2026-10-08 19:01:00", outcome="cancelled"),
        finish(q, clock, "delete a folder", created=LOCAL_9_OCT,
               finished="2026-10-08 19:02:00", outcome="cancelled"),
    ]
    record(q, clock, "2026-10-08 19:00:00", "policy_deny", refused[0], {"tier": "never"})
    record(q, clock, "2026-10-08 19:01:00", "tool_deny", refused[1], {"tool": "mail.send"})
    record(q, clock, "2026-10-08 19:02:00", "authority_refused", refused[2],
           {"reason": "the rule of two"})
    data = page_data(db)
    assert {t["task_id"] for t in data["refused"]} == set(refused)
    assert data["policy_denials"] == 3


def test_approvals_decided_today_are_listed_with_their_outcome(
        q: TaskQueue, clock: Callable[[str], None], db: Path) -> None:
    granted = new_task(q, clock, "publish the note", created=LOCAL_9_OCT)
    denied = new_task(q, clock, "send the report", created=LOCAL_9_OCT)
    old = new_task(q, clock, "old approval", created=LOCAL_8_OCT)
    a_granted = decide(q, clock, granted, "granted", at="2026-10-08 19:30:00")
    a_denied = decide(q, clock, denied, "denied", at="2026-10-08 20:00:00")
    decide(q, clock, old, "granted", at="2026-10-07 09:00:00")
    listed = {a["approval_id"]: a["state"] for a in page_data(db)["approvals"]}
    assert listed == {a_granted: "granted", a_denied: "denied"}


def test_schedules_fired_today_and_the_denial_counts(
        q: TaskQueue, clock: Callable[[str], None], db: Path) -> None:
    task = new_task(q, clock, "fetch a page", created=LOCAL_9_OCT)
    fired = fire_schedule(q, clock, at="2026-10-08 12:30:00")
    record(q, clock, "2026-10-08 19:00:00", "egress_deny", task,
           {"host": "example.com", "reason": "not on the list", "hop": 0})
    record(q, clock, "2026-10-08 19:01:00", "egress_deny", None,
           {"host": "example.org", "reason": "not on the list", "hop": 0})
    record(q, clock, "2026-10-07 09:00:00", "egress_deny", None,
           {"host": "example.net", "reason": "not on the list", "hop": 0})
    record(q, clock, "2026-10-08 19:05:00", "approval_rejected", task,
           {"approval_id": "0" * 32, "reason": "no valid operator signature"})
    data = page_data(db)
    assert data["egress_denials"] == 2
    assert data["policy_denials"] == 0
    assert data["approvals_rejected"] == 1
    assert [(s["schedule"], s["task_id"]) for s in data["schedules"]] == [
        ("digest", fired)]
    page = page_text(db)
    assert "egress denials" in page.lower() and "Schedules fired today" in page


def test_a_hostile_title_is_escaped_on_the_page(
        q: TaskQueue, clock: Callable[[str], None], db: Path) -> None:
    task_id = finish(q, clock, HOSTILE, created=LOCAL_9_OCT, finished="2026-10-08 18:03:00")
    page = page_text(db)
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in page
    assert "<script" not in page.lower() and "<form" not in page.lower()
    assert task_id in page
    # The JSON view keeps the title as data: a string, not markup.
    assert HOSTILE in [t["title"] for t in page_data(db)["finished"]]


def test_payloads_results_and_errors_never_appear(
        q: TaskQueue, clock: Callable[[str], None], db: Path) -> None:
    finish(q, clock, "a good run", created=LOCAL_9_OCT, finished="2026-10-08 18:03:00")
    finish(q, clock, "a bad run", created=LOCAL_9_OCT, finished="2026-10-08 18:04:00",
           outcome="failed")
    body = page_text(db) + dashboard.render_today_json(db, now=NOW, tz=NZ)
    for secret in (PAYLOAD, RESULT, ERROR):
        assert secret not in body


# ----------------------------------------------------------- the web server


@pytest.mark.safety
def test_the_today_pages_refuse_a_foreign_host_header(db: Path) -> None:
    with serving(db) as addr:
        for path in ("/today", "/today.json"):
            assert fetch(addr, path, host="evil.example.com").status == 403
        assert fetch(addr, "/today", host=f"127.0.0.1:{addr[1]}").status == 200
        assert fetch(addr, "/today.json", host=f"localhost:{addr[1]}").status == 200


@pytest.mark.safety
@pytest.mark.parametrize("path", ["/today", "/today.json"])
@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_only_get_is_served_on_the_today_pages(db: Path, path: str, method: str) -> None:
    with serving(db) as addr:
        resp = fetch(addr, path, method=method)
        assert resp.status == 405
        assert "GET" in (resp.getheader("Allow") or "")


@pytest.mark.safety
def test_the_today_pages_send_the_status_page_headers_and_no_controls(db: Path) -> None:
    with serving(db) as addr:
        page = fetch(addr, "/today")
        body = page.read().decode()
        data = fetch(addr, "/today.json")
        payload = json.loads(data.read())
        unknown = fetch(addr, "/today/extra").status
    assert page.status == 200 and page.getheader("Content-Type", "").startswith("text/html")
    csp = page.getheader("Content-Security-Policy", "")
    assert "default-src 'none'" in csp and "no-store" in (page.getheader("Cache-Control") or "")
    assert "<form" not in body.lower() and "<script" not in body.lower()
    assert data.status == 200 and data.getheader("Content-Type") == "application/json"
    assert {"day", "zone", "created", "finished", "awaiting_approval", "refused",
            "approvals", "schedules", "egress_denials", "policy_denials",
            "approvals_rejected"} <= set(payload)
    assert unknown == 404


@pytest.mark.safety
def test_serving_the_today_pages_never_writes_to_the_database(tmp_path: Path) -> None:
    path = tmp_path / "lab.db"
    with TaskQueue(path) as queue:
        set_created(queue, queue.add_task("a task"), LOCAL_9_OCT)
    before = path.read_bytes()
    with serving(path) as addr:
        fetch(addr, "/today").read()
        fetch(addr, "/today.json").read()
    assert path.read_bytes() == before


@pytest.mark.safety
def test_the_connection_behind_the_today_pages_is_read_only(db: Path) -> None:
    conn = dashboard._open(db)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("CREATE TABLE probe (x)")
    finally:
        conn.close()


def test_a_database_that_cannot_be_read_gives_503(tmp_path: Path) -> None:
    path = tmp_path / "lab.db"
    path.write_bytes(b"")  # an empty file has no tables
    with serving(path) as addr:
        assert fetch(addr, "/today").status == 503


def test_the_status_page_links_to_today(db: Path) -> None:
    with serving(db) as addr:
        body = fetch(addr).read().decode()
    assert 'href="/today"' in body and 'href="/today.json"' in body
