"""Queue-aware sleep prevention (H5a, checklist section 5).

Holding the machine awake unconditionally wastes power and hides a stuck
queue; letting it sleep with work pending stalls the lab. The rule:

* hold while any task is queued, leased or running, or the log moved
  within the grace period (so a burst of work is not interrupted by a
  sleep between two ticks);
* release once the queue has been empty and the log quiet for the grace
  period;
* never hold for approvals waiting on a person, or while the operator has
  paused or stopped the lab.

``decide`` is a pure read of the database. ``Holder`` owns at most one
``caffeinate`` process, replaces one that died, and releases it on idle
and on exit. On the Mac mini ``lab keepawake`` runs as a LaunchDaemon; the
function of ``caffeinate -i`` is the only thing that needs macOS.
"""

from __future__ import annotations

import sqlite3
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

DEFAULT_GRACE_SECONDS = 600.0


@dataclass(frozen=True)
class Decision:
    hold: bool
    reason: str


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(stamp[:26], fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def decide(conn: sqlite3.Connection, grace_seconds: float = DEFAULT_GRACE_SECONDS,
           now: datetime | None = None) -> Decision:
    now = now or datetime.now(UTC)
    try:
        mode = str(conn.execute("SELECT mode FROM control WHERE id = 1").fetchone()[0])
    except (sqlite3.OperationalError, TypeError):
        mode = "running"                       # a database from before the control switch
    if mode != "running":
        return Decision(False, f"lab is {mode} by the operator")
    pending = int(conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE state IN ('queued', 'leased', 'running')"
    ).fetchone()[0])
    if pending:
        return Decision(True, f"{pending} task(s) queued or running")
    last = _parse(conn.execute("SELECT MAX(created_at) FROM events").fetchone()[0])
    if last is not None and (now - last).total_seconds() <= grace_seconds:
        return Decision(True, f"activity within the last {grace_seconds:g}s")
    return Decision(False, f"idle for more than {grace_seconds:g}s")


class Process(Protocol):
    def terminate(self) -> None: ...
    def poll(self) -> int | None: ...


def spawn_caffeinate() -> subprocess.Popen[bytes]:
    """``-i`` prevents idle sleep; the display may still sleep."""
    return subprocess.Popen(["caffeinate", "-i"], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Holder:
    def __init__(self, spawn: Callable[[], Process] = spawn_caffeinate) -> None:
        self._spawn = spawn
        self._proc: Process | None = None

    @property
    def holding(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def apply(self, decision: Decision) -> None:
        if decision.hold and not self.holding:
            self._proc = self._spawn()
        elif not decision.hold and self._proc is not None:
            self.close()

    def close(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
        self._proc = None
