"""Supervisor heartbeat, watchdog and launchd definitions (item 6.2, #78).

launchd ``KeepAlive`` restarts a process that died. It does nothing for
one that is alive and stuck. So the supervisor writes a heartbeat from
its event loop, and a separate periodic job (the watchdog) kills the
process when the heartbeat goes stale; launchd then restarts it.

The heartbeat is a small JSON file next to the database: pid, time, and
the process start time. The watchdog signals only a pid it read from that
file, only when the recorded start time still matches ``ps`` (so a pid
that was reused by another process is never killed), and never pid 0 or 1.

What the watchdog does not do: judge whether the queue is healthy (that
is ``lab status``), or alert a person (parked with the M6, #79).
"""

from __future__ import annotations

import contextlib
import json
import os
import plistlib
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

SUPERVISOR_LABEL = "com.homelab.supervisor"
WATCHDOG_LABEL = "com.homelab.watchdog"
DEFAULT_MAX_AGE = 90.0
WATCHDOG_INTERVAL_SECONDS = 30
TICK_LABEL = "com.homelab.tick"
TICK_INTERVAL_SECONDS = 300
KEEPAWAKE_LABEL = "com.homelab.keepawake"
CHAT_LABEL = "com.homelab.chat"


def heartbeat_path(db: str | Path) -> Path:
    return Path(f"{db}.heartbeat")


def _start_time(pid: int) -> str | None:
    """The process start time as ``ps`` reports it, or None if there is no such process."""
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=5, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out or None


@dataclass(frozen=True)
class Heartbeat:
    pid: int
    ts: float
    started: str | None

    def age(self, now: float | None = None) -> float:
        return max(0.0, (time.time() if now is None else now) - self.ts)


def write_heartbeat(db: str | Path, *, stamp: float | None = None) -> None:
    """Atomic replace, so the watchdog never reads half a file."""
    path = heartbeat_path(db)
    pid = os.getpid()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"pid": pid, "ts": time.time() if stamp is None else stamp,
                               "started": _start_time(pid)}))
    os.replace(tmp, path)


def read_heartbeat(db: str | Path) -> Heartbeat | None:
    try:
        raw = json.loads(heartbeat_path(db).read_text())
        pid, ts, started = raw["pid"], raw["ts"], raw.get("started")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return None
    if isinstance(ts, bool) or not isinstance(ts, int | float):
        return None
    return Heartbeat(pid, float(ts), started if isinstance(started, str) else None)


@dataclass(frozen=True)
class Verdict:
    action: str      # healthy | killed | would_kill | no_heartbeat | not_running | corrupt
    pid: int | None = None
    age: float | None = None


def check(db: str | Path, *, max_age: float = DEFAULT_MAX_AGE,
          dry_run: bool = False) -> Verdict:
    path = heartbeat_path(db)
    if not path.exists():
        return Verdict("no_heartbeat")
    beat = read_heartbeat(db)
    if beat is None:
        return Verdict("corrupt")
    age = beat.age()
    if age <= max_age:
        return Verdict("healthy", beat.pid, age)
    current = _start_time(beat.pid)
    if current is None or (beat.started is not None and current != beat.started):
        return Verdict("not_running", beat.pid, age)
    if dry_run:
        return Verdict("would_kill", beat.pid, age)
    with contextlib.suppress(ProcessLookupError):
        os.kill(beat.pid, signal.SIGKILL)
    return Verdict("killed", beat.pid, age)


def _plist(label: str, args: list[str], workdir: str, **extra: object) -> bytes:
    return plistlib.dumps({
        "Label": label, "ProgramArguments": args, "WorkingDirectory": workdir,
        "StandardOutPath": "/var/log/homelab/" + label.rsplit(".", 1)[1] + ".log",
        "StandardErrorPath": "/var/log/homelab/" + label.rsplit(".", 1)[1] + ".err",
        **extra,
    })


def supervisor_plist(*, user: str, python: str, workdir: str, db: str,
                     log_dir: str = "/var/log/homelab") -> bytes:
    """The supervisor as a LaunchDaemon: starts at boot, restarts on exit.

    ``ThrottleInterval`` is launchd's own bounded backoff: a crash loop
    restarts no more often than every 30 seconds. ``ExitTimeOut`` gives a
    SIGTERM-ed supervisor time to release its leases before SIGKILL.
    """
    return _plist(SUPERVISOR_LABEL, [python, "-m", "lab.supervisor", "--db", db], workdir,
                  UserName=user, RunAtLoad=True, KeepAlive=True,
                  ThrottleInterval=30, ExitTimeOut=30,
                  EnvironmentVariables={"LAB_LOG_DIR": log_dir})


def watchdog_plist(*, python: str, workdir: str, db: str) -> bytes:
    """A periodic job, deliberately independent of the supervisor it watches."""
    return _plist(WATCHDOG_LABEL, [python, "-m", "lab.cli", "--db", db, "watchdog"], workdir,
                  RunAtLoad=True, StartInterval=WATCHDOG_INTERVAL_SECONDS)


def tick_plist(*, user: str, python: str, workdir: str, db: str) -> bytes:
    """``lab tick`` every five minutes as the lab user. The model comes from
    ``LAB_MODEL_URL``, ``LAB_MODEL_NAME`` and ``LAB_MODEL_REVISION`` in the
    job's environment; without them the job exits 1 and does nothing."""
    return _plist(TICK_LABEL, [python, "-m", "lab.cli", "--db", db, "tick"], workdir,
                  UserName=user, RunAtLoad=False, StartInterval=TICK_INTERVAL_SECONDS)


def keepawake_plist(*, python: str, workdir: str, db: str, user: str | None = None) -> bytes:
    """``lab keepawake`` as a daemon: read-only on the database, holds
    ``caffeinate`` only while work is pending.

    It needs no privilege, so it should run as the lab account (#235). The
    committed copy still runs as root until the operator confirms on the
    mini that ``caffeinate`` under the lab account holds a power assertion
    from a LaunchDaemon with no login session.
    """
    extra: dict[str, object] = {} if user is None else {"UserName": user}
    return _plist(KEEPAWAKE_LABEL, [python, "-m", "lab.cli", "--db", db, "keepawake"], workdir,
                  RunAtLoad=True, KeepAlive=True, ThrottleInterval=30, **extra)


def selftest_plist(*, user: str, python: str, workdir: str, db: str,
                   alert_config: str) -> bytes:
    """The nightly self-test at 03:17, alerting on failure."""
    return _plist("com.homelab.selftest",
                  [python, "-m", "lab.cli", "--db", db, "selftest", "--alert-config",
                   alert_config], workdir, UserName=user,
                  StartCalendarInterval={"Hour": 3, "Minute": 17})


def statuscheck_plist(*, user: str, python: str, workdir: str, db: str,
                      alert_config: str) -> bytes:
    """``lab status`` every five minutes; it alerts when the lab is unhealthy."""
    return _plist("com.homelab.statuscheck",
                  [python, "-m", "lab.cli", "--db", db, "status", "--alert-config",
                   alert_config], workdir, UserName=user, StartInterval=300)


def chat_plist(*, user: str, python: str, workdir: str, db: str) -> bytes:
    """``lab chat`` as the lab user, restarted on exit (#239).

    The pairing is not in the committed file on purpose: the operator adds
    ``LAB_CHAT_ID`` to the installed, root-owned copy, where the lab account
    cannot change it. Without it the job exits 2 and answers nobody.
    """
    return _plist(CHAT_LABEL, [python, "-m", "lab.cli", "--db", db, "chat"], workdir,
                  UserName=user, RunAtLoad=True, KeepAlive=True, ThrottleInterval=30)
