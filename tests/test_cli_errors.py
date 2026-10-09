"""Database and file errors print one line, not a traceback. Issue #331.

A file that is not SQLite, a database from a newer build, a locked database and a
path that cannot be opened used to end in a traceback. Now they print
`lab: <message>` to stderr and exit 1. `--debug` keeps the traceback.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import lab.queue as queue_mod
from lab.cli import COMMANDS, main
from lab.migrations import latest_version
from lab.queue import TaskQueue


def _one_line(capsys) -> str:
    err = capsys.readouterr().err
    assert "Traceback" not in err, err
    lines = err.splitlines()
    assert len(lines) == 1 and lines[0].startswith("lab: "), err
    return lines[0]


def test_a_file_that_is_not_sqlite_prints_one_line(tmp_path: Path, capsys) -> None:
    db = tmp_path / "lab.db"
    db.write_text("this is not a database\n" * 50)
    assert main(["--db", str(db), "tasks"]) == 1
    assert _one_line(capsys) == "lab: file is not a database"


def test_a_database_from_a_newer_build_prints_one_line(tmp_path: Path, capsys) -> None:
    db = tmp_path / "lab.db"
    TaskQueue(db).close()
    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute(f"PRAGMA user_version = {latest_version() + 1}")  # nosemgrep
    conn.close()
    assert main(["--db", str(db), "tasks"]) == 1
    assert "knows up to" in _one_line(capsys)


def test_a_directory_given_as_the_database_prints_one_line(tmp_path: Path, capsys) -> None:
    db = tmp_path / "lab.db"
    db.mkdir()
    assert main(["--db", str(db), "tasks"]) == 1
    assert _one_line(capsys) == "lab: unable to open database file"


def test_a_locked_database_prints_one_line(tmp_path: Path, capsys,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "lab.db"
    TaskQueue(db).close()
    # A write lock held by another connection makes the CLI's write wait out the busy
    # timeout. A short timeout keeps the test fast. The lock is held until the command returns.
    monkeypatch.setattr(queue_mod, "BUSY_TIMEOUT_MS", 200)
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        assert main(["--db", str(db), "control", "pause", "--by", "test"]) == 1
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert _one_line(capsys) == "lab: database is locked"


def test_an_os_error_from_a_command_prints_one_line(tmp_path: Path, capsys,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "lab.db"
    TaskQueue(db).close()

    def denied(queue: object, policy: object, args: object) -> int:
        raise PermissionError(13, "Permission denied", "lab.db")

    monkeypatch.setitem(COMMANDS, "tasks", denied)
    assert main(["--db", str(db), "tasks"]) == 1
    assert _one_line(capsys) == "lab: [Errno 13] Permission denied: 'lab.db'"


def test_a_network_error_keeps_its_traceback(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "lab.db"
    TaskQueue(db).close()

    def reset(queue: object, policy: object, args: object) -> int:
        raise ConnectionResetError(54, "Connection reset by peer")

    monkeypatch.setitem(COMMANDS, "tasks", reset)
    with pytest.raises(ConnectionResetError):
        main(["--db", str(db), "tasks"])


def test_other_errors_still_raise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "lab.db"
    TaskQueue(db).close()

    def broken(queue: object, policy: object, args: object) -> int:
        raise ValueError("a bug")

    monkeypatch.setitem(COMMANDS, "tasks", broken)
    with pytest.raises(ValueError, match="a bug"):
        main(["--db", str(db), "tasks"])


def test_a_command_that_handles_its_own_error_keeps_its_message(tmp_path: Path, capsys) -> None:
    db = tmp_path / "lab.db"
    db.write_text("this is not a database\n" * 50)
    assert main(["--db", str(db), "status"]) == 1
    assert capsys.readouterr().err.startswith("status: cannot read the database: ")


def test_debug_re_raises_so_the_traceback_shows(tmp_path: Path) -> None:
    db = tmp_path / "lab.db"
    db.write_text("this is not a database\n" * 50)
    with pytest.raises(sqlite3.DatabaseError, match="file is not a database"):
        main(["--debug", "--db", str(db), "tasks"])
