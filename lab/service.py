"""Supervisor heartbeat, watchdog and launchd definitions (item 6.2, #78).

launchd ``KeepAlive`` restarts a process that died. It does nothing for
one that is alive and stuck. So the supervisor writes a heartbeat from
its event loop, and a separate periodic job (the watchdog) kills the
process when the heartbeat goes stale; launchd then restarts it.

The heartbeat is a small JSON file next to the database: pid, time, and
the process start time. The watchdog signals a pid it read from that file only
when the recorded start time still matches ``ps`` (so a pid that was reused by
another process is never killed), and never pid 0 or 1.

One more case, because the file alone cannot show it (2026-10-06, #271): a
supervisor that hangs after a restart, before its first heartbeat, leaves a file
that names a dead process, or none. Then the watchdog looks for a live process
whose command line *starts* with a python executable and continues exactly
``-m lab.supervisor --db <this database>``; one that has run longer than the
maximum age should have beaten by now and is killed. Just before the signal it
asks ``ps`` about that pid again, and only signals it if it is still that
process and has been running at least as long as before, so a pid that was reused
in between is left alone. If ``ps`` itself fails the verdict says so
(``lookup_failed``) instead of looking like "nothing found".

What the watchdog does not do: judge whether the queue is healthy (that
is ``lab status``), or alert a person (``lab status --alert-config`` and
the dead-man switch, ``lab heartbeat``, #79).
"""

from __future__ import annotations

import contextlib
import json
import os
import plistlib
import re
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
BACKUP_LABEL = "com.homelab.backup"
BACKUP_KEEP = 14
# The committed backup definition carries this placeholder; the operator puts
# the real folder into the installed root-owned copy (runbook step 4). The
# command refuses to run while the placeholder is still there.
BACKUP_DIR_PLACEHOLDER = "PASTE_BACKUP_DIR"
# The backup job runs this compiled launcher, not the interpreter, so that only
# the backup holds Full Disk Access (#287). Built from
# ops/backup-launcher/lab-backup.c and installed root-owned (runbook step 4).
BACKUP_LAUNCHER = "/opt/homelab-backup/lab-backup"
DEADMAN_LABEL = "com.homelab.heartbeat"
DEADMAN_INTERVAL_SECONDS = 300
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
    # healthy | killed | would_kill | starting | no_heartbeat | not_running | corrupt
    # | lookup_failed (ps could not be run, so the fallback could not look)
    action: str
    pid: int | None = None
    age: float | None = None
    uptime: float | None = None     # set when the process was found by its command line


def _etime_seconds(text: str) -> float | None:
    """``ps -o etime`` is ``[[dd-]hh:]mm:ss``."""
    match = re.fullmatch(r"(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)", text.strip())
    if match is None:
        return None
    days, hours, minutes, seconds = (int(g or 0) for g in match.groups())
    return float(((days * 24 + hours) * 60 + minutes) * 60 + seconds)


def _supervisor_line(db: str | Path) -> re.Pattern[str]:
    """A command line that starts with a python executable and then continues exactly
    ``-m lab.supervisor --db <db>``. A process that merely has those words somewhere in
    its arguments (``python -c ... -m lab.supervisor ...``, an editor, a shell) is not it."""
    return re.compile(r"^(?:\S*/)?python[\d.]*\s+-m lab\.supervisor --db "
                      + re.escape(str(db)) + r"(\s|$)")


def _ps(*args: str) -> tuple[int, str] | None:
    """``(exit status, stdout)`` of ``ps``, or None if it could not be run at all."""
    try:
        proc = subprocess.run(["ps", *args], capture_output=True, text=True, timeout=10,
                              check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.returncode, proc.stdout


def supervisor_processes(db: str | Path) -> list[tuple[int, float]] | None:
    """Live ``(pid, seconds running)`` of this lab's supervisor for ``db``, or None if
    ``ps`` failed (so the caller can say it could not look, not that nothing is there)."""
    result = _ps("-axo", "pid=,etime=,command=")
    if result is None or result[0] != 0:
        return None
    needle = _supervisor_line(db)
    found = []
    for line in result[1].splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit() or int(parts[0]) == os.getpid():
            continue
        uptime = _etime_seconds(parts[1])
        if uptime is not None and needle.search(parts[2]):
            found.append((int(parts[0]), uptime))
    return found


def _still_that_supervisor(db: str | Path, pid: int, min_uptime: float) -> bool:
    """Asked again just before a signal: is ``pid`` still this lab's supervisor, and has it
    been running at least as long as when it was chosen? A reused pid is a younger process."""
    result = _ps("-o", "etime=,command=", "-p", str(pid))
    if result is None or result[0] != 0:
        return False
    parts = result[1].strip().split(None, 1)
    if len(parts) < 2:
        return False
    uptime = _etime_seconds(parts[0])
    return (uptime is not None and uptime >= min_uptime
            and _supervisor_line(db).search(parts[1]) is not None)


def _unseen_supervisor(db: str | Path, max_age: float, dry_run: bool,
                       otherwise: Verdict) -> Verdict:
    """The heartbeat names no live process, is missing, or is unreadable. A supervisor that
    is running anyway, and has been for longer than ``max_age``, should have written one by
    now: it hung before its first beat (2026-10-06, #271)."""
    running = supervisor_processes(db)
    if running is None:
        return Verdict("lookup_failed", otherwise.pid, otherwise.age)
    stuck = [(pid, up) for pid, up in running if up > max_age]
    if stuck:
        pid, up = max(stuck, key=lambda item: item[1])
        if dry_run:
            return Verdict("would_kill", pid, otherwise.age, up)
        if not _still_that_supervisor(db, pid, up):
            return otherwise                      # gone, or the pid now belongs to another
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        return Verdict("killed", pid, otherwise.age, up)
    if running:
        pid, up = running[0]
        return Verdict("starting", pid, otherwise.age, up)
    return otherwise


def check(db: str | Path, *, max_age: float = DEFAULT_MAX_AGE,
          dry_run: bool = False) -> Verdict:
    path = heartbeat_path(db)
    if not path.exists():
        return _unseen_supervisor(db, max_age, dry_run, Verdict("no_heartbeat"))
    beat = read_heartbeat(db)
    if beat is None:
        return _unseen_supervisor(db, max_age, dry_run, Verdict("corrupt"))
    age = beat.age()
    if age <= max_age:
        return Verdict("healthy", beat.pid, age)
    current = _start_time(beat.pid)
    if current is None or (beat.started is not None and current != beat.started):
        return _unseen_supervisor(db, max_age, dry_run, Verdict("not_running", beat.pid, age))
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


def keepawake_plist(*, user: str, python: str, workdir: str, db: str) -> bytes:
    """``lab keepawake`` as a daemon, as the lab user: read-only on the
    database, holds ``caffeinate`` only while work is pending.

    It needs no privilege, so it runs as the lab account (#235). On the mini
    the installed copy changes only after the operator's check that
    ``caffeinate`` run as the lab account holds a power assertion (runbook,
    "Moving keep-awake to the lab account").
    """
    return _plist(KEEPAWAKE_LABEL, [python, "-m", "lab.cli", "--db", db, "keepawake"], workdir,
                  UserName=user, RunAtLoad=True, KeepAlive=True, ThrottleInterval=30)


def selftest_plist(*, user: str, python: str, workdir: str, db: str,
                   alert_config: str) -> bytes:
    """The nightly self-test at 03:17. It alerts on failure and also sends a
    short "ok", so a result arrives every morning (#80)."""
    return _plist("com.homelab.selftest",
                  [python, "-m", "lab.cli", "--db", db, "selftest", "--alert-config",
                   alert_config, "--report-ok"], workdir, UserName=user,
                  StartCalendarInterval={"Hour": 3, "Minute": 17})


def statuscheck_plist(*, user: str, python: str, workdir: str, db: str,
                      alert_config: str) -> bytes:
    """``lab status`` every five minutes; it alerts when the lab is unhealthy."""
    return _plist("com.homelab.statuscheck",
                  [python, "-m", "lab.cli", "--db", db, "status", "--alert-config",
                   alert_config], workdir, UserName=user, StartInterval=300)


def backup_command(*, python: str, db: str, alert_config: str,
                   keep: int = BACKUP_KEEP) -> list[str]:
    """The one command the backup launcher runs (#287). The launcher has it built
    in; ``tests/test_backup_launcher.py`` checks the two agree. ``-I`` keeps the
    environment and the working directory from adding code to the run."""
    return [python, "-I", "-m", "lab.cli", "--db", db, "backup", "--keep", str(keep),
            "--alert-config", alert_config]


def backup_plist(*, user: str, workdir: str, launcher: str = BACKUP_LAUNCHER,
                 backup_dir: str = BACKUP_DIR_PLACEHOLDER) -> bytes:
    """A daily backup at 02:47 as the lab user (#67): snapshot, prove the new
    backup restores, keep the newest 14, alert on any failure. The folder
    comes from ``LAB_BACKUP_DIR``, set by the operator in the installed copy.

    The job runs the backup launcher, which takes no arguments and runs
    ``backup_command`` with a fixed environment, so Full Disk Access is given to
    the launcher instead of the interpreter every lab service shares (#287)."""
    return _plist(BACKUP_LABEL, [launcher], workdir, UserName=user,
                  StartCalendarInterval={"Hour": 2, "Minute": 47},
                  EnvironmentVariables={"LAB_BACKUP_DIR": backup_dir})


def heartbeat_plist(*, user: str, python: str, workdir: str, db: str, url_file: str) -> bytes:
    """``lab heartbeat`` every five minutes as the lab user: the dead-man
    switch ping, sent only while the lab is healthy (#79)."""
    return _plist(DEADMAN_LABEL,
                  [python, "-m", "lab.cli", "--db", db, "heartbeat", "--url-file", url_file],
                  workdir, UserName=user, RunAtLoad=True, StartInterval=DEADMAN_INTERVAL_SECONDS)


def chat_plist(*, user: str, python: str, workdir: str, db: str) -> bytes:
    """``lab chat`` as the lab user, restarted on exit (#239).

    The pairing is not in the committed file on purpose: the operator adds
    ``LAB_CHAT_ID`` to the installed, root-owned copy, where the lab account
    cannot change it. Without it the job exits 2 and answers nobody.
    """
    return _plist(CHAT_LABEL, [python, "-m", "lab.cli", "--db", db, "chat"], workdir,
                  UserName=user, RunAtLoad=True, KeepAlive=True, ThrottleInterval=30)
