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
* attention items that need a person.

Model load time, peak memory and swap are not here: they need the model
adapter (5.1) and the Mac mini, and will be added as more event kinds
read the same way.

``health`` is the answer a watchdog needs:

* ``unhealthy``: a task claims to be running on a lease that has expired
  (the worker died or hung), or work is waiting, no live lease exists and
  the log has been silent past the stall threshold (nothing is picking
  work up);
* ``attention``: nothing is broken but a person is owed something
  (approvals waiting, an operation of unknown outcome);
* ``idle`` / ``ok`` otherwise.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

DEFAULT_STALL_SECONDS = 300.0

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

# The states a task's outcome is counted in. A count comes from the state
# changes in the event log, so a failure that is retried stays counted.
OUTCOMES = ("succeeded", "failed", "cancelled")


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
    outcomes: dict[str, int] = field(default_factory=dict)
    model: None = None       # load time, peak memory, swap: added with the adapter (5.1)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _age(now: datetime, stamp: str | None) -> float | None:
    moment = _parse(stamp)
    return None if moment is None else max(0.0, (now - moment).total_seconds())


def collect(conn: sqlite3.Connection, *, now: datetime | None = None,
            window_hours: float | None = None,
            stall_seconds: float = DEFAULT_STALL_SECONDS) -> Metrics:
    now = now or datetime.now(UTC)
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
    outcomes = {outcome: int(conn.execute(
        "SELECT COUNT(*) FROM events WHERE to_state = ? AND from_state IS NOT to_state "
        "AND created_at >= ?", (outcome, since)).fetchone()[0]) for outcome in OUTCOMES}
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
        control_mode=control_mode, outcomes=outcomes,
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


TASK_STATES = ("queued", "leased", "running", "awaiting_approval", "interrupted",
               "succeeded", "failed", "cancelled")
HEALTH_STATES = ("idle", "ok", "attention", "unhealthy")


def render_prometheus(m: Metrics) -> str:
    """The numbers in the Prometheus text format, version 0.0.4.

    Only metric names, fixed label values and numbers are written. No task
    id, title, path or reason reaches the output. A task state this code does
    not know is counted under ``state="other"`` and never printed.
    """
    out: list[str] = []

    def family(name: str, kind: str, help_text: str, samples: list[str]) -> None:
        out.append(f"# HELP {name} {help_text}")
        out.append(f"# TYPE {name} {kind}")
        out.extend(samples)

    other = sum(n for state, n in m.states.items() if state not in TASK_STATES)
    tasks = [(state, m.states.get(state, 0)) for state in TASK_STATES] + [("other", other)]
    family("homelab_tasks", "gauge", "Tasks in each state.",
           [f'homelab_tasks{{state="{state}"}} {n}' for state, n in tasks])
    family("homelab_health", "gauge", "1 for the current health state, 0 for the others.",
           [f'homelab_health{{state="{s}"}} {int(m.health == s)}' for s in HEALTH_STATES])
    family("homelab_pending_approvals", "gauge", "Approvals waiting for a person.",
           [f"homelab_pending_approvals {m.pending_approvals}"])
    family("homelab_unresolved_operations", "gauge",
           "Operations of unknown outcome that need reconciling.",
           [f"homelab_unresolved_operations {m.unresolved_operations}"])
    family("homelab_live_leases", "gauge", "Leases held by a live worker.",
           [f"homelab_live_leases {m.live_leases}"])
    age = m.last_success_age_seconds
    family("homelab_last_success_age_seconds", "gauge",
           "Seconds since a task last succeeded. No sample until one has.",
           [] if age is None else [f"homelab_last_success_age_seconds {age:.3f}"])
    family("homelab_task_outcomes_total", "counter",
           "Tasks that reached each outcome, counted from the event log. A failure "
           "that was retried stays counted.",
           [f'homelab_task_outcomes_total{{outcome="{o}"}} {m.outcomes.get(o, 0)}'
            for o in OUTCOMES])
    counters = [*m.counters.items(), ("retries", m.retries), ("recovered_tasks", m.recoveries)]
    for name, value in counters:
        family(f"homelab_{name}_total", "counter",
               f"{name.replace('_', ' ').capitalize()} over all time.",
               [f"homelab_{name}_total {value}"])
    return "\n".join(out) + "\n"
