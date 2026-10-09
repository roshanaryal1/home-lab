"""GET /metrics on the status page: Prometheus text, fixed names, loopback only."""

from __future__ import annotations

import dataclasses
import http.client
import json
import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import pytest

from lab import dashboard, metrics
from lab.queue import TaskQueue

HOSTILE = '<script>alert("x")</script>&"\''
HOSTILE_TITLE = 'evil"} 1\nevil_metric 2'
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
SAMPLE = re.compile(
    r'^[a-zA-Z_:][a-zA-Z0-9_:]*'
    r'(\{[a-zA-Z_][a-zA-Z0-9_]*="[^"\\\n]*"(,[a-zA-Z_][a-zA-Z0-9_]*="[^"\\\n]*")*\})?'
    r' -?[0-9.eE+-]+$')


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    path = tmp_path / "lab.db"
    with TaskQueue(path) as queue:
        queue.add_task(HOSTILE_TITLE)
        queue._conn.execute("UPDATE control SET reason = ?, set_by = ? WHERE id = 1",
                            (HOSTILE, HOSTILE))
        queue._conn.commit()
    return path


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


def fetch(addr: tuple[str, int], path: str = "/metrics", method: str = "GET",
          host: str | None = None) -> http.client.HTTPResponse:
    conn = http.client.HTTPConnection(addr[0], addr[1], timeout=5)
    conn.putrequest(method, path, skip_host=host is not None)
    if host is not None:
        conn.putheader("Host", host)
    conn.endheaders()
    return conn.getresponse()


def scrape(addr: tuple[str, int]) -> str:
    resp = fetch(addr)
    assert resp.status == 200
    return resp.read().decode()


def sample_lines(body: str) -> list[str]:
    return [line for line in body.splitlines() if not line.startswith("#")]


def test_metrics_is_served_as_prometheus_text_format_0_0_4(db: Path) -> None:
    with serving(db) as addr:
        resp = fetch(addr)
        body = resp.read().decode()
    assert resp.status == 200
    assert resp.getheader("Content-Type") == CONTENT_TYPE
    assert body.endswith("\n")


def test_every_line_is_a_comment_or_a_well_formed_sample(db: Path) -> None:
    with serving(db) as addr:
        body = scrape(addr)
    declared = {line.split()[2] for line in body.splitlines() if line.startswith("# TYPE ")}
    samples = sample_lines(body)
    assert samples, "no samples at all"
    for line in samples:
        assert SAMPLE.match(line), line
        name = re.split(r"[{ ]", line, maxsplit=1)[0]
        assert name in declared, f"{name} has no # TYPE line"


@pytest.mark.safety
def test_a_hostile_title_and_reason_never_reach_the_output(db: Path) -> None:
    with serving(db) as addr:
        body = scrape(addr)
    for hostile in ("evil", "alert", "script", "<", "&"):
        assert hostile not in body, hostile


@pytest.mark.safety
def test_an_unknown_state_is_counted_as_other_and_never_printed(db: Path) -> None:
    # The schema's CHECK on tasks.state refuses a hostile state, so this drives
    # the renderer with a Metrics value instead of writing one into the database.
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        base = metrics.collect(conn)
    finally:
        conn.close()
    hostile = 'x"} 1\nevil 2'
    body = metrics.render_prometheus(
        dataclasses.replace(base, states={hostile: 3, "queued": 1}))
    assert 'homelab_tasks{state="other"} 3' in body
    assert 'homelab_tasks{state="queued"} 1' in body
    assert "evil" not in body and 'x"' not in body
    for line in sample_lines(body):
        assert SAMPLE.match(line), line


def test_task_counts_use_the_fixed_state_names(db: Path) -> None:
    with serving(db) as addr:
        body = scrape(addr)
    for state in (*metrics.TASK_STATES, "other"):
        assert f'homelab_tasks{{state="{state}"}} ' in body, state
    assert 'homelab_tasks{state="queued"} 1' in body


def test_health_is_one_hot_and_agrees_with_status_json(db: Path) -> None:
    with serving(db) as addr:
        body = scrape(addr)
        health = json.loads(fetch(addr, "/status.json").read())["health"]
    pairs = re.findall(r'homelab_health\{state="(\w+)"\} (\d+)', body)
    values = {state: int(value) for state, value in pairs}
    assert values == {state: int(state == health) for state in metrics.HEALTH_STATES}


def test_every_counter_lab_status_reports_has_a_total(db: Path) -> None:
    with serving(db) as addr:
        body = scrape(addr)
    samples = sample_lines(body)
    for name in (*metrics.COUNTERS, "retries", "recovered_tasks"):
        assert f"# TYPE homelab_{name}_total counter\n" in body, name
        assert any(line.startswith(f"homelab_{name}_total ") for line in samples), name


def test_the_last_success_age_is_absent_until_a_task_succeeds(db: Path) -> None:
    with serving(db) as addr:
        body = scrape(addr)
    assert "# TYPE homelab_last_success_age_seconds gauge\n" in body
    assert not any(line.startswith("homelab_last_success_age_seconds")
                   for line in sample_lines(body))


def test_the_last_success_age_is_reported_once_a_task_has_succeeded(tmp_path: Path) -> None:
    path = tmp_path / "lab.db"
    with TaskQueue(path) as queue:
        queue.add_task("done")
        task = queue.lease()
        assert task is not None and task.lease is not None
        queue.start(task.lease)
        queue.succeed(task.lease, {})
    with serving(path) as addr:
        body = scrape(addr)
    assert re.search(r"^homelab_last_success_age_seconds \d+\.\d{3}$", body, re.MULTILINE)
    assert 'homelab_tasks{state="succeeded"} 1' in body


@pytest.mark.safety
def test_a_foreign_host_is_refused_on_metrics(db: Path) -> None:
    with serving(db) as addr:
        assert fetch(addr, host="evil.example.com").status == 403
        assert fetch(addr, host=f"127.0.0.1:{addr[1]}").status == 200


@pytest.mark.safety
@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_metrics_is_get_only(db: Path, method: str) -> None:
    with serving(db) as addr:
        resp = fetch(addr, method=method)
        assert resp.status == 405
        assert "GET" in (resp.getheader("Allow") or "")


def test_a_failure_that_is_retried_stays_counted_as_an_outcome(tmp_path: Path) -> None:
    path = tmp_path / "lab.db"
    with TaskQueue(path) as queue:
        task_id = queue.add_task("flaky", idempotent=True)
        task = queue.lease()
        assert task is not None and task.lease is not None
        queue.start(task.lease)
        queue.fail(task.lease, "boom", retry_in=timedelta(0))
        again = queue.get(task_id)
        assert again is not None and again.state == "queued", "the failure was retried"
        cancelled = queue.add_task("not wanted")
        queue.cancel(cancelled)
    with serving(path) as addr:
        body = scrape(addr)
    assert "# TYPE homelab_task_outcomes_total counter\n" in body
    assert 'homelab_task_outcomes_total{outcome="failed"} 1' in body
    assert 'homelab_task_outcomes_total{outcome="cancelled"} 1' in body
    assert 'homelab_task_outcomes_total{outcome="succeeded"} 0' in body
    assert 'homelab_tasks{state="failed"} 0' in body, "no task is failed now"
