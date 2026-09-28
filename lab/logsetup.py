"""Structured, rotating, private logs (H5a, checklist section 5).

One JSON object per line with four fixed keys (``ts``, ``level``,
``logger``, ``msg``). ``json.dumps`` with ASCII output escapes newlines
and control characters, so text that came from a task, a web page or a
model cannot forge a second record or move a terminal cursor. Files are
created 0600 in a 0700 directory, rotate at a size, and keep a bounded
number of backups. Long-term copies go to the external SSD by pointing
``LAB_LOG_DIR`` there.
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


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        if record.exc_info and record.exc_info[0] is not None:
            msg += f" | {record.exc_info[0].__name__}: {record.exc_info[1]}"
        return json.dumps({
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname, "logger": record.name, "msg": msg,
        }, ensure_ascii=True)


class PrivateRotatingFileHandler(RotatingFileHandler):
    """Every file it opens, including after a rotation, is mode 0600."""

    def _open(self) -> io.TextIOWrapper:
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        return os.fdopen(fd, "a", encoding=self.encoding)


def configure(log_dir: str | Path, *, name: str = "lab", level: int = logging.INFO,
              max_bytes: int = DEFAULT_MAX_BYTES,
              backups: int = DEFAULT_BACKUPS) -> logging.Handler:
    directory = Path(log_dir)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
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
