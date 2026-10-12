"""Read-only export of memory, the action log and task results (#381).

``lab export --to DIR`` writes a new folder under DIR holding five files:
``memory.json`` and ``memory.md`` (memory items, every column plus who
read each one and which proposal it came from), ``events.jsonl`` (the
events table, one object per row, which is the action log, with every
column exactly as stored, so the hash chain can be checked from the
export alone), and
``tasks.json`` and ``tasks.md`` (every task row, with payload and result
parsed from JSON). The database is opened read-only and is never written.

Every file is private: the folder is 0700 and each file is 0600. The
folder name carries the UTC time, and an existing folder is never reused.

All text from the database is untrusted. The JSON files keep it as data,
with non-ASCII escaped so a bidirectional override shows as a visible
escape. The Markdown files run it through ``lab.untrusted.clean`` and then
escape the characters that could start a link, an emphasis run, a heading,
a list, a table, a code span or raw HTML, so ``[x](http://evil)`` and
``<script>`` read as literal text.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lab.db import connect_readonly
from lab.untrusted import clean

FOLDER_MODE = 0o700
FILE_MODE = 0o600
# Every character that Markdown or HTML could give meaning to, so that a
# backslash in front of it turns it into plain text.
_MARKDOWN_META = re.compile(r"([\\`*_\[\]()#+\-!|<>&~=])")


class ExportError(ValueError):
    """The export was refused, or could not be written."""


@dataclass(frozen=True)
class ExportReport:
    folder: Path
    memory_items: int
    events: int
    tasks: int


_MEMORY_SQL = (
    "SELECT m.*, p.task_id AS proposed_by_task, p.tainted AS proposal_tainted "
    "FROM memories AS m LEFT JOIN memory_proposals AS p ON p.id = m.proposal_id "
    "ORDER BY m.id"
)


def _refuse_constant(name: str) -> Any:
    raise ValueError(f"{name} is not strict JSON")


def _parse(value: str | None) -> Any:
    """A column that holds JSON comes back parsed. Anything else stays text."""
    if value is None:
        return None
    try:
        return json.loads(value, parse_constant=_refuse_constant)
    except (ValueError, RecursionError):
        return value


def _uses(conn: sqlite3.Connection) -> dict[int, list[str]]:
    found: dict[int, set[str]] = {}
    for row in conn.execute("SELECT memory_id, task_id FROM memory_uses"):
        found.setdefault(row["memory_id"], set()).add(row["task_id"])
    return {memory_id: sorted(tasks) for memory_id, tasks in found.items()}


def _read(db: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Memory items, events and tasks, all from one read snapshot."""
    if not db.exists():
        raise ExportError(f"no database at {db}")
    try:
        conn = connect_readonly(db)
    except sqlite3.Error as exc:
        raise ExportError(f"cannot open {db}: {exc}") from exc
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")  # one snapshot, so the three tables agree with each other
        try:
            uses = _uses(conn)
            memory: list[dict[str, Any]] = []
            for row in conn.execute(_MEMORY_SQL):
                item = dict(row)
                if item["proposal_tainted"] is not None:
                    item["proposal_tainted"] = bool(item["proposal_tainted"])
                item["read_by_tasks"] = uses.get(item["id"], [])
                memory.append(item)
            # Each column as stored: ``detail`` is part of the event hash, so it
            # stays the exact text that was hashed, not a parsed copy.
            events = [dict(row) for row in conn.execute("SELECT * FROM events ORDER BY id")]
            tasks = [{**dict(row), "payload": _parse(row["payload"]),
                      "result": _parse(row["result"])}
                     for row in conn.execute("SELECT * FROM tasks ORDER BY created_at, id")]
        finally:
            conn.execute("ROLLBACK")  # nothing was written, so this only ends the read
    except sqlite3.Error as exc:
        raise ExportError(f"cannot read {db}: {exc}") from exc
    finally:
        conn.close()
    return memory, events, tasks


def _md(value: object) -> str:
    """One line of Markdown that shows the value as literal text."""
    if value is None:
        return "none"
    if isinstance(value, dict | list):
        value = json.dumps(value, sort_keys=True)
    text = " ".join(clean(str(value)).splitlines()).replace("\t", " ").strip()
    return _MARKDOWN_META.sub(r"\\\1", text)


def _json_text(rows: list[dict[str, Any]]) -> str:
    return json.dumps(rows, indent=2) + "\n"


def _memory_md(items: list[dict[str, Any]]) -> str:
    lines = ["# Memory", "",
             f"{len(items)} items. The text is data from the database, escaped for display.",
             ""]
    for item in items:
        lines += [f"## Memory {_md(item['id'])}", "", _md(item["text"]) or "(no text)", ""]
        lines += [f"- {_md(key)}: {_md(value)}" for key, value in item.items()
                  if key not in ("id", "text")]
        lines.append("")
    return "\n".join(lines) + "\n"


def _tasks_md(rows: list[dict[str, Any]]) -> str:
    lines = ["# Tasks", "",
             f"{len(rows)} tasks. The title is data from the database, escaped for display.",
             ""]
    for row in rows:
        lines += [f"## {_md(row['title']) or '(untitled)'}", ""]
        lines += [f"- {_md(key)}: {_md(value)}" for key, value in row.items()
                  if key != "title"]
        lines.append("")
    return "\n".join(lines) + "\n"


def _write_file(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _write_folder(folder: Path, files: dict[str, str]) -> None:
    try:
        os.mkdir(folder, FOLDER_MODE)
    except FileExistsError as exc:
        raise ExportError(f"{folder} already exists") from exc
    except OSError as exc:
        raise ExportError(f"cannot create {folder}: {exc}") from exc
    try:
        os.chmod(folder, FOLDER_MODE)
        for name, text in files.items():
            _write_file(folder / name, text)
    except OSError as exc:
        # The folder was made by this call, so removing it cannot touch anyone else's.
        shutil.rmtree(folder, ignore_errors=True)
        raise ExportError(f"cannot write {folder}: {exc}") from exc


def export(db: Path, to: Path, *, now: datetime | None = None) -> ExportReport:
    """Write a new export folder under ``to`` and return what it holds.

    Refused, before anything is written, when ``to`` is a symbolic link or
    is not an existing directory, when the database is missing, or when the
    folder for this second already exists.
    """
    if to.is_symlink():
        raise ExportError(f"{to} is a symbolic link. Give the real directory instead.")
    if not to.is_dir():
        raise ExportError(f"{to} does not exist or is not a directory")
    memory, events, tasks = _read(db)
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    folder = to / f"lab-export-{stamp}"
    _write_folder(folder, {
        "memory.json": _json_text(memory),
        "memory.md": _memory_md(memory),
        "events.jsonl": "".join(json.dumps(event) + "\n" for event in events),
        "tasks.json": _json_text(tasks),
        "tasks.md": _tasks_md(tasks),
    })
    return ExportReport(folder, len(memory), len(events), len(tasks))
