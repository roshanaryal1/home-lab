"""The operator's alert hook (H5b).

When the lab is unhealthy or its nightly self-test fails, a person must be
told. What "told" means (a push service, an email, a chat message) is the
operator's choice, so the lab runs one command the operator configured and
hands it the message on stdin. It never builds a shell line.

The trust rules, each with a test:

* The command comes only from a JSON file the operator owns. The file must be
  a regular file owned by the current user that no one else can write; the
  command must be an argv list whose first element is an absolute path. It is
  never read from the database, a task, a payload or a model.
* The message is data. It goes on stdin only, after control characters are
  removed and the text is bounded, so it cannot become an argument, an
  option or a terminal escape. The only environment the hook sees is PATH
  and ``LAB_ALERT_KIND``, a fixed lowercase word.
* The file is opened once, without following a symlink, and its owner and mode
  are read from the open descriptor, so the file that was checked is the file
  that is read.
* A hook that hangs is killed with its whole process group; one that fails is
  reported to the caller, never raised into the health check.
* The same kind of alert is not repeated inside ``min_interval_seconds``, so a
  stuck lab does not send one every five minutes. The check, the run and the
  timestamp happen under one lock on the state file, so two processes cannot
  both decide an alert is due.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import signal
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lab.untrusted import clean

MAX_MESSAGE_CHARS = 1000
KIND = re.compile(r"^[a-z_]{1,32}$")


class AlertConfigError(ValueError):
    """The alert configuration is missing, unsafe or malformed."""


@dataclass(frozen=True)
class AlertConfig:
    command: tuple[str, ...]
    timeout_seconds: float = 20.0
    min_interval_seconds: float = 3600.0


def load(path: str | Path) -> AlertConfig:
    path = Path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise AlertConfigError(f"cannot read alert configuration {path}: {exc.strerror}") from exc
    with os.fdopen(fd, encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())        # the descriptor, not the path
        if not stat.S_ISREG(info.st_mode):
            raise AlertConfigError(f"{path} is not a regular file")
        if info.st_uid != os.geteuid():
            raise AlertConfigError(f"{path} is not owned by the user running the lab")
        if info.st_mode & 0o022:
            raise AlertConfigError(f"{path} is writable by group or others; chmod 600 it")
        try:
            raw = stream.read()
        except (OSError, UnicodeDecodeError) as exc:
            raise AlertConfigError(f"cannot read alert configuration {path}") from exc
    try:
        data: Any = json.loads(raw)
    except ValueError as exc:
        raise AlertConfigError(f"{path} is not valid JSON") from exc
    command = data.get("command") if isinstance(data, dict) else None
    if (not isinstance(command, list) or not command
            or not all(isinstance(part, str) and part and "\x00" not in part
                       for part in command)):
        raise AlertConfigError('"command" must be a non-empty list of non-empty strings')
    if not os.path.isabs(command[0]):
        raise AlertConfigError('"command"[0] must be an absolute path, not looked up in PATH')
    timeout = data.get("timeout_seconds", 20.0)
    interval = data.get("min_interval_seconds", 3600.0)
    for name, value in (("timeout_seconds", timeout), ("min_interval_seconds", interval)):
        if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
            raise AlertConfigError(f"{name} must be a positive number")
    return AlertConfig(tuple(command), float(timeout), float(interval))


def _sanitize(message: str) -> str:
    text = " ".join(clean(message).split())
    return text[:MAX_MESSAGE_CHARS]


def _due(state_file: Path | None, kind: str, interval: float, now: float) -> bool:
    if state_file is None:
        return True
    try:
        last = float(json.loads(state_file.read_text()).get(kind, 0))
    except (OSError, ValueError, AttributeError, TypeError):
        return True
    return now - last >= interval


def _remember(state_file: Path | None, kind: str, now: float) -> None:
    if state_file is None:
        return
    with contextlib.suppress(OSError):        # a lost timestamp only means one extra alert
        try:
            state = json.loads(state_file.read_text())
            if not isinstance(state, dict):
                state = {}
        except (OSError, ValueError):
            state = {}
        state[kind] = now
        fd = os.open(state_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(state))


@contextlib.contextmanager
def _locked(state_file: Path | None):  # type: ignore[no-untyped-def]
    """An exclusive lock for the check, the run and the timestamp."""
    if state_file is None:
        yield
        return
    lock = state_file.with_name(state_file.name + ".lock")
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _run_hook(config: AlertConfig, kind: str, message: str) -> bool:
    try:
        # The argv is the operator's own file (see the module docstring), a
        # list run without a shell; the message is stdin only.
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
        proc = subprocess.Popen(
            list(config.command), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, shell=False, start_new_session=True,
            env={"PATH": "/usr/bin:/bin", "LAB_ALERT_KIND": kind})
    except (OSError, ValueError):
        return False
    try:
        proc.communicate(f"{kind}: {_sanitize(message)}\n", timeout=config.timeout_seconds)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        return False
    return proc.returncode == 0


def send(config: AlertConfig, *, kind: str, message: str,
         state_file: Path | None = None) -> bool:
    """Run the hook once. True if it ran and exited 0, False otherwise."""
    if not KIND.match(kind):
        raise ValueError("kind must be a short lowercase word")
    with _locked(state_file):
        now = time.time()
        if not _due(state_file, kind, config.min_interval_seconds, now):
            return False
        if not _run_hook(config, kind, message):
            return False
        _remember(state_file, kind, now)
        return True
