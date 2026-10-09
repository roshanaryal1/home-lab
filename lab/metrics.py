"""Operational metrics derived from the event log (item #89).

There is no metrics store. Everything here is a query over the tables the
lab already keeps, most of all the append-only, hash-chained ``events``
table, so a number cannot disagree with the audit trail it came from and
there is no second thing to back up, migrate or trust.

What is reported:

* queue depth per state and the age of the oldest queued, running and
  approval-waiting task;
* worker health: whether anything is alive to take work, when the last
  task succeeded, when the log last moved;
* counters: policy denials, retries, lease losses, forced terminations,
  egress denials, rejected approvals, recoveries, worker errors;
* attention items that need a person, including stalled tasks: a task
  whose lease keeps renewing but which has written no event for a while
  (``stalled_tasks``, #376).

Model load time, peak memory and swap are not here: they need the model
adapter (5.1) and the Mac mini, and will be added as more event kinds
read the same way.

``health`` is the answer a watchdog needs:

* ``unhealthy``: a task claims to be running on a lease that has expired
  (the worker died or hung), or work is waiting, no live lease exists and
  the log has been silent past the stall threshold (nothing is picking
  work up);
* ``attention``: nothing is broken but a person is owed something
  (approvals waiting, an operation of unknown outcome, a stalled task);
* ``idle`` / ``ok`` otherwise.

A stalled task is ``attention``, not ``unhealthy``, on purpose. The
dead-man heartbeat and the nightly self-test both treat ``unhealthy`` as
"the lab is down". One hung task does not make the lab down, and a
stalled task should not stop the heartbeat or fail the self-test.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from lab.untrusted import clean

DEFAULT_STALL_SECONDS = 300.0
# A leased or running task with a renewed lease that writes no event for
# this long is reported as stalled.
DEFAULT_STALLED_MINUTES = 30.0

# Counter name -> the event kinds that count towards it. Each kind is
# written by exactly one code path, so summing does not double count.
COUNTERS: dict[str, tuple[str, ...]] = {
    "policy_denials": ("policy_deny", "tool_deny", "authority_refused"),
    "approvals_rejected": ("approval_rejected",),
    "egress_denials": ("egress_deny",),
    "lease_losses": ("lease_lost",),
    "forced_terminations": ("stopped", "stopped_on_lease_loss", "wall_clock_exceeded",
                            "resource_ceiling_exceeded"),
    "worker_errors": ("worker_error",),
    "tasks_tainted": ("task_tainted",),
}


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    text = stamp.replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text[:26], fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


@dataclass(frozen=True)
class Stalled:
    """A leased or running task that has written no event for a while (#376)."""

    task_id: str
    agent_kind: str | None
    state: str
    minutes_since_event: float


def _taken_expiry(conn: sqlite3.Connection, task_id: str, lease_id: str) -> str | None:
    """The ``expires_at`` written when lease ``lease_id`` was taken.

    The ``leased`` event of the claim records it in its detail JSON. None
    if that event cannot be found or read.
    """
    rows = conn.execute(
        "SELECT detail FROM events WHERE task_id = ? AND kind = 'leased' ORDER BY id",
        (task_id,)).fetchall()
    for (detail,) in rows:
        with contextlib.suppress(ValueError, TypeError):
            data = json.loads(detail or "{}")
            if isinstance(data, dict) and data.get("lease_id") == lease_id:
                expires = data.get("expires_at")
                return expires if isinstance(expires, str) else None
    return None


def stalled_tasks(conn: sqlite3.Connection, now: datetime, *,
                  quiet_minutes: float = DEFAULT_STALLED_MINUTES) -> list[Stalled]:
    """Tasks whose lease was renewed but which have written no event for a while.

    Read-only. Every statement is a SELECT. Nothing is cancelled, killed or
    requeued: the check flags a task for a person, and decides nothing.

    A task is stalled when all of these hold.

    * Its state is ``leased`` or ``running`` and it holds a live lease,
      which means ``leases.released_at`` is NULL and ``expires_at`` is after
      ``now``. An expired lease is reported by the health check instead.
    * The lease was renewed after it was taken. ``renew_lease`` moves
      ``leases.expires_at`` in place and writes no event, and the schema
      has no ``renewed_at`` column. The expiry the lease was taken with is
      kept in the task's ``leased`` event (detail JSON, keyed by
      ``lease_id``). A live lease whose ``expires_at`` now differs from it
      has been renewed. A lease whose original expiry cannot be read is
      not reported, because its renewal cannot be shown.
    * The newest event for the task is at least ``quiet_minutes`` old at
      ``now``. The boundary is inclusive, so exactly that many minutes is
      stalled.

    ``now`` must be timezone-aware (UTC). Event times come from
    ``events.created_at``, which has millisecond precision on rows that
    ``lab.audit.append_event`` wrote and second precision on older rows.
    """
    live = conn.execute(
        "SELECT t.id, t.agent_kind, t.state, l.id, l.expires_at "
        "FROM tasks t JOIN leases l ON l.task_id = t.id AND l.released_at IS NULL "
        "WHERE t.state IN ('leased', 'running') ORDER BY t.id").fetchall()
    stalled: list[Stalled] = []
    for task_id, agent_kind, state, lease_id, expires_now in live:
        expiry = _parse(expires_now)
        if expiry is None or expiry <= now:
            # An expired lease is not live: the health check reports it as expired.
            continue
        taken = _taken_expiry(conn, task_id, lease_id)
        if taken is None or taken == expires_now:
            continue
        last = conn.execute(
            "SELECT created_at FROM events WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,)).fetchone()
        moment = _parse(last[0]) if last else None
        if moment is None:
            continue
        silent = (now - moment).total_seconds() / 60
        if silent >= quiet_minutes:
            stalled.append(Stalled(task_id, agent_kind, state, round(silent, 2)))
    return stalled


def stalled_reason(task: Stalled) -> str:
    """One line naming a stalled task, for the health reasons and the alert."""
    return (f"task {task.task_id} ({clean(task.agent_kind or '-')}, {task.state}) "
            f"has written no event for {_dur(task.minutes_since_event * 60)}, "
            "though its lease was renewed")


@dataclass
class Metrics:
    generated_at: str
    window_hours: float | None
    states: dict[str, int]
    oldest_queued_seconds: float | None
    oldest_running_seconds: float | None
    oldest_approval_seconds: float | None
    last_success_at: str | None
    last_success_age_seconds: float | None
    last_event_age_seconds: float | None
    live_leases: int
    counters: dict[str, int]
    retries: int
    recoveries: int
    unresolved_operations: int
    pending_approvals: int
    health: str
    reasons: list[str] = field(default_factory=list)
    control_mode: str = "running"
    stalled: list[Stalled] = field(default_factory=list)
    stalled_minutes: float = DEFAULT_STALLED_MINUTES
    model: None = None       # load time, peak memory, swap: added with the adapter (5.1)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _age(now: datetime, stamp: str | None) -> float | None:
    moment = _parse(stamp)
    return None if moment is None else max(0.0, (now - moment).total_seconds())


def collect(conn: sqlite3.Connection, *, now: datetime | None = None,
            window_hours: float | None = None,
            stall_seconds: float = DEFAULT_STALL_SECONDS,
            stalled_minutes: float = DEFAULT_STALLED_MINUTES) -> Metrics:
    now = now or datetime.now(UTC)
    stalled = stalled_tasks(conn, now, quiet_minutes=stalled_minutes)
    now_text = now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    since = (now - timedelta(hours=window_hours)).strftime("%Y-%m-%d %H:%M:%S.000") \
        if window_hours else "0000-01-01 00:00:00.000"

    states = {r[0]: r[1] for r in conn.execute(
        "SELECT state, COUNT(*) FROM tasks GROUP BY state")}

    def oldest(state: str, column: str = "created_at") -> float | None:
        row = conn.execute(f"SELECT MIN({column}) FROM tasks WHERE state = ?",  # nosemgrep
                           (state,)).fetchone()
        return _age(now, row[0])

    last_success = conn.execute(
        "SELECT MAX(created_at) FROM events WHERE to_state = 'succeeded'").fetchone()[0]
    last_event = conn.execute("SELECT MAX(created_at) FROM events").fetchone()[0]
    live = int(conn.execute(
        "SELECT COUNT(*) FROM leases WHERE released_at IS NULL AND expires_at > ?",
        (now_text,)).fetchone()[0])
    expired_running = int(conn.execute(
        "SELECT COUNT(*) FROM tasks t JOIN leases l ON l.task_id = t.id "
        "AND l.released_at IS NULL WHERE t.state IN ('leased', 'running') "
        "AND l.expires_at <= ?", (now_text,)).fetchone()[0])

    def count(kinds: tuple[str, ...]) -> int:
        marks = ",".join("?" for _ in kinds)
        return int(conn.execute(
            f"SELECT COUNT(*) FROM events WHERE kind IN ({marks}) AND created_at >= ?",  # nosemgrep
            (*kinds, since)).fetchone()[0])

    counters = {name: count(kinds) for name, kinds in COUNTERS.items()}
    retries = int(conn.execute(
        "SELECT COUNT(*) FROM events WHERE from_state = 'failed' AND to_state = 'queued' "
        "AND created_at >= ?", (since,)).fetchone()[0])
    recoveries = 0
    for (detail,) in conn.execute(
            "SELECT detail FROM events WHERE kind = 'recovery' AND created_at >= ?", (since,)):
        with contextlib.suppress(ValueError, TypeError):
            recoveries += int(json.loads(detail or "{}").get("interrupted", 0))
    unresolved = int(conn.execute(
        "SELECT COUNT(*) FROM operations WHERE state IN ('executing', 'uncertain')"
    ).fetchone()[0])
    pending = int(conn.execute(
        "SELECT COUNT(*) FROM approvals WHERE state = 'pending'").fetchone()[0])

    try:
        control_mode = str(conn.execute("SELECT mode FROM control WHERE id = 1").fetchone()[0])
    except (sqlite3.OperationalError, TypeError):
        control_mode = "running"       # a database from before migration 12

    queued_age = oldest("queued")
    silent_for = _age(now, last_event)
    reasons: list[str] = []
    if expired_running:
        reasons.append(f"{expired_running} task(s) marked running on an expired lease: "
                       "the worker died or hung")
    runnable = states.get("queued", 0) > 0
    quiet = silent_for is None or silent_for > stall_seconds
    if runnable and live == 0 and control_mode == "running" and quiet \
            and (queued_age or 0) > stall_seconds:
        reasons.append(f"work has waited {int(queued_age or 0)}s with no live worker and "
                       f"no log activity for {int(silent_for or 0)}s")
    attention: list[str] = []
    if control_mode != "running":
        attention.append(f"lab is {control_mode} by the operator")
    if pending:
        attention.append(f"{pending} approval(s) waiting for a person")
    if unresolved:
        attention.append(f"{unresolved} operation(s) of unknown outcome need reconciling")
    attention.extend(stalled_reason(task) for task in stalled)
    if reasons:
        health = "unhealthy"
    elif attention:
        health, reasons = "attention", attention
    elif not runnable and live == 0 and not states.get("running"):
        health = "idle"
    else:
        health = "ok"

    return Metrics(
        generated_at=now.isoformat(timespec="seconds"), window_hours=window_hours,
        states=states, oldest_queued_seconds=queued_age,
        oldest_running_seconds=oldest("running", "updated_at"),
        oldest_approval_seconds=oldest("awaiting_approval", "updated_at"),
        last_success_at=last_success, last_success_age_seconds=_age(now, last_success),
        last_event_age_seconds=silent_for, live_leases=live, counters=counters,
        retries=retries, recoveries=recoveries, unresolved_operations=unresolved,
        pending_approvals=pending, health=health, reasons=reasons,
        control_mode=control_mode, stalled=stalled, stalled_minutes=stalled_minutes,
    )


def _dur(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    seconds = int(seconds)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def render(m: Metrics) -> str:
    lines = [f"health   {m.health.upper()}"
             + (f"  ({'; '.join(m.reasons)})" if m.reasons else "")]
    window = f"last {m.window_hours:g}h" if m.window_hours else "all time"
    lines.append(f"mode     {m.control_mode}")
    lines.append(f"as of    {m.generated_at}   counters: {window}")
    lines.append("")
    lines.append("queue")
    for state in ("queued", "leased", "running", "awaiting_approval", "interrupted",
                  "succeeded", "failed", "cancelled"):
        if m.states.get(state):
            lines.append(f"  {state:<18}{m.states[state]:>6}")
    lines.append(f"  oldest queued      {_dur(m.oldest_queued_seconds):>6}")
    lines.append(f"  oldest running     {_dur(m.oldest_running_seconds):>6}")
    lines.append(f"  oldest approval    {_dur(m.oldest_approval_seconds):>6}")
    lines.append("")
    lines.append("worker")
    lines.append(f"  live leases        {m.live_leases:>6}")
    lines.append(f"  last success ago   {_dur(m.last_success_age_seconds):>6}")
    lines.append(f"  log last moved     {_dur(m.last_event_age_seconds):>6} ago")
    if m.stalled:
        lines.append("")
        lines.append(f"stalled (no event for {m.stalled_minutes:g}m, lease renewed)")
        for task in m.stalled:
            lines.append(f"  {task.task_id}  {task.state:<8} "
                         f"{clean(task.agent_kind or '-'):<12} "
                         f"quiet {_dur(task.minutes_since_event * 60)}")
    lines.append("")
    lines.append("counters")
    for name, value in m.counters.items():
        lines.append(f"  {name:<22}{value:>6}")
    lines.append(f"  {'retries':<22}{m.retries:>6}")
    lines.append(f"  {'recovered tasks':<22}{m.recoveries:>6}")
    lines.append("")
    lines.append(f"needs a person: {m.pending_approvals} approval(s), "
                 f"{m.unresolved_operations} unresolved operation(s)")
    return "\n".join(lines)
