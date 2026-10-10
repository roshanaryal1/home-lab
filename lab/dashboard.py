"""A read-only status page (H7).

It shows what ``lab status`` shows: health, the operator mode, queue depth,
worker liveness, counters and what needs a person. It has no controls, no
forms, no script and no state of its own.

* Loopback only. ``make_server`` refuses any other bind address. Reaching it
  from another device is the operator's decision (a private tunnel to the
  loopback port), not something this module opens.
* GET only. Every other method is 405.
* The Host header must name a loopback address, which defeats DNS rebinding
  from a web page in the operator's own browser.
* The database is opened read-only per request; nothing is written.
* Every stored string is HTML-escaped before it reaches the page.
* ``/today`` and ``/today.json`` show the local day so far: what was created,
  finished, refused or approved, which schedules fired, and the denial counts.
  Task text is shown as an id, kind, state and title. Payloads, results,
  errors and approval intents are never shown.
* ``/metrics`` serves the same numbers in the Prometheus text format. It holds
  fixed names, fixed labels and counts only, never a task id or title.
"""

from __future__ import annotations

import html
import ipaddress
import json
import sqlite3
from datetime import datetime, tzinfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from lab import control, metrics
from lab.db import connect_readonly

DEFAULT_PORT = 8765
LOOPBACK_NAMES = {"localhost"}
CSP = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"
STYLE = """\
body{font:15px/1.5 system-ui,sans-serif;margin:2rem auto;max-width:44rem;padding:0 1rem}
table{border-collapse:collapse;margin:.5rem 0 1.5rem}td{padding:.15rem 1.5rem .15rem 0}
th{text-align:left;font-weight:600;padding:.15rem 1.5rem .15rem 0}
td.n{text-align:right;font-variant-numeric:tabular-nums}
.ok,.idle{color:#0a6b2d}.attention{color:#8a5a00}.unhealthy{color:#b00020}
@media (prefers-color-scheme:dark){body{background:#111;color:#eee}}
"""
PROMETHEUS_TYPE = "text/plain; version=0.0.4; charset=utf-8"


class DashboardError(ValueError):
    pass


def is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _open(db: Path) -> sqlite3.Connection:
    """The one way this module opens the database: read-only, never created."""
    return connect_readonly(db, timeout=5)


def _read(db: Path) -> tuple[metrics.Metrics, control.ControlState]:
    conn = _open(db)
    try:
        return metrics.collect(conn), control.get(conn)
    finally:
        conn.close()


def _read_today(db: Path, now: datetime | None = None, tz: tzinfo | None = None) -> metrics.Today:
    conn = _open(db)
    try:
        return metrics.today(conn, now=now, tz=tz)
    finally:
        conn.close()


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def render_page(db: Path) -> str:
    m, state = _read(db)
    rows = "".join(
        f"<tr><td>{_e(name)}</td><td class=n>{_e(count)}</td></tr>"
        for name, count in sorted(m.states.items()))
    counters = "".join(
        f"<tr><td>{_e(name)}</td><td class=n>{_e(value)}</td></tr>"
        for name, value in [*m.counters.items(), ("retries", m.retries),
                            ("recovered tasks", m.recoveries)])
    reasons = f" ({_e('; '.join(m.reasons))})" if m.reasons else ""
    reason = f", reason: {_e(state.reason)}" if state.reason else ""
    stalled_rows = "".join(
        f"<tr><td>{_e(task.task_id)}</td><td>{_e(task.state)}</td>"
        f"<td>{_e(task.agent_kind or '-')}</td>"
        f"<td class=n>quiet {_e(metrics._dur(task.minutes_since_event * 60))}</td></tr>"
        for task in m.stalled)
    threshold = _e(f"{m.stalled_minutes:g}")
    stalled = (f"<h2>Stalled</h2><p>no event for {threshold}m, lease renewed</p>"
               f"<table>{stalled_rows}</table>") if m.stalled else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Home lab status</title>
<style>
{STYLE}
</style></head><body>
<h1>Home lab</h1>
<p>health: <strong class="{_e(m.health)}">{_e(m.health.upper())}</strong>{reasons}<br>
mode: <strong>{_e(state.mode)}</strong>{reason}<br>
as of {_e(m.generated_at)}</p>
<h2>Queue</h2><table>{rows or "<tr><td>empty</td></tr>"}</table>
<p>oldest queued {_e(metrics._dur(m.oldest_queued_seconds))},
oldest running {_e(metrics._dur(m.oldest_running_seconds))},
live leases {_e(m.live_leases)},
last success {_e(metrics._dur(m.last_success_age_seconds))} ago</p>
{stalled}
<h2>Counters</h2><table>{counters}</table>
<p>needs a person: {_e(m.pending_approvals)} approval(s),
{_e(m.unresolved_operations)} unresolved operation(s)</p>
<p><a href="/today">Today</a> and <a href="/today.json">/today.json</a>: the local day so far</p>
<p><small>Read-only. Change the mode or approve work with the <code>lab</code> command.</small></p>
</body></html>
"""


def render_json(db: Path) -> str:
    m, state = _read(db)
    return json.dumps({**m.as_dict(), "control_mode": state.mode}, sort_keys=True)


TASK_HEADERS = ["task", "kind", "state", "title"]


def _section(heading: str, count: int, headers: list[str], rows: list[list[Any]]) -> str:
    """One titled table on the today page. Every cell is escaped."""
    title = f"<h2>{_e(heading)} ({count})</h2>"
    if not rows:
        return f"{title}<p>none</p>"
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{_e(cell)}</td>" for cell in row) + "</tr>"
                   for row in rows)
    return f"{title}<table><tr>{head}</tr>{body}</table>"


def render_today(db: Path, *, now: datetime | None = None, tz: tzinfo | None = None) -> str:
    t = _read_today(db, now, tz)
    tasks = [[x.task_id, x.kind or "-", x.state, x.title] for x in t.created]
    finished = [[x.task_id, x.kind or "-", x.state, x.title] for x in t.finished]
    waiting = [[x.task_id, x.kind or "-", x.state, x.title] for x in t.awaiting_approval]
    refused = [[x.task_id, x.kind or "-", x.state, x.title] for x in t.refused]
    decided = [[a.approval_id, a.task_id, a.state, a.decided_at, a.decided_by or "-"]
               for a in t.approvals]
    fired = [[s.fired_at, s.schedule or "-", s.task_id or "-"] for s in t.schedules]
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Home lab today</title>
<style>
{STYLE}
</style></head><body>
<h1>Home lab, today</h1>
<p>{_e(t.day)} ({_e(t.zone)}), as of {_e(t.generated_at)}<br>
UTC from {_e(t.window_start)} up to, not including, {_e(t.window_end)}</p>
<p><a href="/">status</a> and <a href="/today.json">/today.json</a></p>
{_section("Created today", len(t.created), TASK_HEADERS, tasks)}
{_section("Finished today", len(t.finished), TASK_HEADERS, finished)}
{_section("Waiting for approval now", len(t.awaiting_approval), TASK_HEADERS, waiting)}
{_section("Refused by policy today", len(t.refused), TASK_HEADERS, refused)}
{_section("Approvals decided today", len(t.approvals),
          ["approval", "task", "decision", "decided at", "by"], decided)}
{_section("Schedules fired today", len(t.schedules),
          ["fired at", "schedule", "task"], fired)}
<h2>Counts today</h2><table>
<tr><td>egress denials</td><td class=n>{_e(t.egress_denials)}</td></tr>
<tr><td>policy denials</td><td class=n>{_e(t.policy_denials)}</td></tr>
<tr><td>approvals rejected by the signature check</td>
<td class=n>{_e(t.approvals_rejected)}</td></tr>
</table>
<p><small>Read-only. Task titles are shown as stored, escaped.
No payloads or results are shown.</small></p>
</body></html>
"""


def render_today_json(db: Path, *, now: datetime | None = None, tz: tzinfo | None = None) -> str:
    return json.dumps(_read_today(db, now, tz).as_dict(), sort_keys=True)


def render_metrics(db: Path) -> str:
    m, _ = _read(db)
    return metrics.render_prometheus(m)


def _host_allowed(header: str | None) -> bool:
    if not header:
        return False
    host = header.rsplit(":", 1)[0] if not header.endswith("]") else header
    host = host.strip("[]")
    return host in LOOPBACK_NAMES or is_loopback(host)


def make_server(db: Path, *, host: str = "127.0.0.1",
                port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    if not is_loopback(host):
        raise DashboardError(f"the dashboard binds to loopback only, not {host!r}")
    if not db.exists():
        raise DashboardError(f"no database at {db}")

    class Handler(BaseHTTPRequestHandler):
        server_version = "lab-dashboard"
        sys_version = ""

        def _send(self, status: int, body: str, kind: str = "text/plain; charset=utf-8") -> None:
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            if status == 405:
                self.send_header("Allow", "GET, HEAD")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _serve(self) -> None:
            if not _host_allowed(self.headers.get("Host")):
                self._send(403, "forbidden\n")
                return
            path = self.path.split("?", 1)[0]
            try:
                if path == "/":
                    self._send(200, render_page(db), "text/html; charset=utf-8")
                elif path == "/status.json":
                    self._send(200, render_json(db), "application/json")
                elif path == "/today":
                    self._send(200, render_today(db), "text/html; charset=utf-8")
                elif path == "/today.json":
                    self._send(200, render_today_json(db), "application/json")
                elif path == "/metrics":
                    self._send(200, render_metrics(db), PROMETHEUS_TYPE)
                else:
                    self._send(404, "not found\n")
            except (sqlite3.DatabaseError, control.ControlError):
                self._send(503, "the database cannot be read\n")

        do_GET = do_HEAD = _serve

        def _refuse(self) -> None:
            self._send(405, "read-only\n")

        do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _refuse

        def log_message(self, format: str, *args: Any) -> None:
            pass

    return ThreadingHTTPServer((host, port), Handler)
