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
* The state file and its lock are opened without following a symlink and are
  used only when the open descriptor is a regular file with one link. If one
  is not, it is left as it is, the caller is told, and the alert still runs
  without it.
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
from collections.abc import Callable, Iterator
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


class StateFileRefused(OSError):
    """The state file or its lock is a symbolic link, not a regular file, or
    has more than one link, so it is not read or written."""


# O_NONBLOCK so a FIFO in the file's place cannot hold the open.
_STATE_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def _not_regular(path: Path) -> str | None:
    """Why ``path`` itself, not anything it links to, cannot be used."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode):
        return "is a symbolic link"
    if not stat.S_ISREG(info.st_mode):
        return "is not a regular file"
    if info.st_nlink != 1:
        return "has more than one link"
    return None


def _open_state(path: Path, flags: int) -> int:
    """Open ``path`` without following a symlink, only as a regular file with one link.

    The type and link count are read from the open descriptor, so the file
    that was checked is the file that is used. Raises StateFileRefused for a
    file that fails the check; other errors are raised as they are.
    """
    try:
        fd = os.open(path, flags | _STATE_FLAGS, 0o600)
    except OSError as exc:
        reason = _not_regular(path)
        if reason is None:
            raise
        raise StateFileRefused(f"{path} {reason}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise StateFileRefused(f"{path} is not a regular file")
        if info.st_nlink != 1:
            raise StateFileRefused(f"{path} has more than one link")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _due(state_file: Path | None, kind: str, interval: float, now: float) -> bool:
    if state_file is None:
        return True
    try:
        with os.fdopen(_open_state(state_file, os.O_RDONLY), encoding="utf-8") as stream:
            last = float(json.loads(stream.read()).get(kind, 0))
    except StateFileRefused:
        raise
    except (OSError, ValueError, AttributeError, TypeError):
        return True
    return now - last >= interval


def _remember(state_file: Path | None, kind: str, now: float) -> None:
    if state_file is None:
        return
    try:
        fd = _open_state(state_file, os.O_RDWR | os.O_CREAT)
    except StateFileRefused:
        raise
    except OSError:
        return                                # a lost timestamp only means one extra alert
    with contextlib.suppress(OSError), os.fdopen(fd, "r+", encoding="utf-8") as fh:
        try:
            state = json.loads(fh.read())
            if not isinstance(state, dict):
                state = {}
        except (OSError, ValueError):
            state = {}
        state[kind] = now
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps(state))


def _quiet(_note: str) -> None:
    return None


@contextlib.contextmanager
def _locked(state_file: Path | None, warn: Callable[[str], None]) -> Iterator[None]:
    """An exclusive lock for the check, the run and the timestamp."""
    if state_file is None:
        yield
        return
    lock = state_file.with_name(state_file.name + ".lock")
    try:
        fd = _open_state(lock, os.O_WRONLY | os.O_CREAT)
    except StateFileRefused as exc:
        warn(f"not using the alert lock: {exc}; this alert runs without it")
        yield
        return
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
         state_file: Path | None = None,
         warn: Callable[[str], None] | None = None) -> bool:
    """Run the hook once. True if it ran and exited 0, False otherwise.

    ``warn`` is told, in one line, when the state file or its lock is not
    used because it is a symbolic link, not a regular file, or has more than
    one link. The alert still runs then, without that file.
    """
    if not KIND.match(kind):
        raise ValueError("kind must be a short lowercase word")
    tell = warn if warn is not None else _quiet
    with _locked(state_file, tell):
        now = time.time()
        try:
            due = _due(state_file, kind, config.min_interval_seconds, now)
        except StateFileRefused as exc:
            tell(f"not using the alert state file: {exc}; this alert is not rate "
                 "limited and its time is not recorded")
            state_file, due = None, True
        if not due:
            return False
        if not _run_hook(config, kind, message):
            return False
        try:
            _remember(state_file, kind, now)
        except StateFileRefused as exc:
            tell(f"not recording this alert's time: {exc}")
        return True
