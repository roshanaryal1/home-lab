"""Structured, rotating, private logs (H5a, checklist section 5).

One JSON object per line with four fixed keys (``ts``, ``level``,
``logger``, ``msg``), and an ``exc`` key holding the traceback when the
record carries one. ``json.dumps`` with ASCII output escapes newlines
and control characters, so text that came from a task, a web page or a
model cannot forge a second record or move a terminal cursor. Files are
created 0600 in a 0700 directory, rotate at a size, and keep a bounded
number of backups. Long-term copies go to the external SSD by pointing
``LAB_LOG_DIR`` there. ``LAB_LOG_LEVEL`` picks DEBUG, INFO (the default),
WARNING or ERROR.
"""

from __future__ import annotations

import io
import json
import logging
import os
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

DEFAULT_MAX_BYTES = 5_000_000
DEFAULT_BACKUPS = 5
LEVEL_ENV = "LAB_LOG_LEVEL"
LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO,
          "WARNING": logging.WARNING, "ERROR": logging.ERROR}


class LogLevelError(ValueError):
    """``LAB_LOG_LEVEL`` names something other than the four levels."""


def level_from_env() -> int:
    """The level ``LAB_LOG_LEVEL`` asks for, in any case. Unset means INFO. Any
    other value is refused, not guessed at, so a typo cannot silence the log."""
    value = os.environ.get(LEVEL_ENV)
    if value is None:
        return logging.INFO
    if value.upper() not in LEVELS:
        raise LogLevelError(
            f"{LEVEL_ENV} must be DEBUG, INFO, WARNING or ERROR, not {value!r}")
    return LEVELS[value.upper()]


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname, "logger": record.name, "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            entry["msg"] += f" | {record.exc_info[0].__name__}: {record.exc_info[1]}"
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=True)


class PrivateRotatingFileHandler(RotatingFileHandler):
    """Every file it opens, including after a rotation, is mode 0600."""

    def _open(self) -> io.TextIOWrapper:
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.fchmod(fd, 0o600)          # also tightens a file that already existed
        return os.fdopen(fd, "a", encoding=self.encoding)


def configure(log_dir: str | Path, *, name: str = "lab", level: int = logging.INFO,
              max_bytes: int = DEFAULT_MAX_BYTES,
              backups: int = DEFAULT_BACKUPS) -> logging.Handler:
    directory = Path(log_dir)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)            # an existing directory may be looser
    for old in directory.glob(f"{name}.log*"):
        if old.is_file() and not old.is_symlink():
            old.chmod(0o600)          # so may the current file and its rotated backups
    handler = PrivateRotatingFileHandler(directory / f"{name}.log", maxBytes=max_bytes,
                                         backupCount=backups, encoding="utf-8")
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("lab")
    logger.setLevel(level)
    logger.addHandler(handler)
    return handler


def teardown(handler: logging.Handler) -> None:
    logging.getLogger("lab").removeHandler(handler)
    handler.close()
