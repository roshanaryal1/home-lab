"""Durable commits and the SQLite version gate (improvement plan item 1.9).

Crash recovery and the retry rules both assume that a committed
transition survives a power cut. That only holds with synchronous=FULL
and, on macOS, fullfsync, and only on a SQLite without the WAL-reset
corruption bug.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lab import queue as queue_mod
from lab.queue import TaskQueue, UnsafeSQLite, sqlite_is_safe


def _pragma(q: TaskQueue, name: str) -> int:
    return q._conn.execute(f"PRAGMA {name}").fetchone()[0]


def test_every_connection_commits_durably(tmp_path: Path) -> None:
    with TaskQueue(tmp_path / "lab.db") as q:
        assert _pragma(q, "synchronous") == 2  # FULL
        assert _pragma(q, "fullfsync") == 1
        assert _pragma(q, "checkpoint_fullfsync") == 1
        assert _pragma(q, "foreign_keys") == 1
        assert _pragma(q, "busy_timeout") == queue_mod.BUSY_TIMEOUT_MS
        assert _pragma(q, "journal_mode") == "wal"


def test_second_connection_on_same_file_is_also_durable(tmp_path: Path) -> None:
    # The pragmas are per connection, so reopening must reapply them
    # rather than inherit whatever the file was last opened with.
    db = tmp_path / "lab.db"
    TaskQueue(db).close()
    with TaskQueue(db) as q:
        assert _pragma(q, "synchronous") == 2
        assert _pragma(q, "fullfsync") == 1


@pytest.mark.parametrize(
    ("version", "safe"),
    [
        ("3.7.0", False),
        ("3.44.5", False),
        ("3.44.6", True),
        ("3.45.0", False),   # no backport on this line
        ("3.50.4", False),   # shipped by some uv-managed Pythons
        ("3.50.7", True),
        ("3.51.2", False),
        ("3.51.3", True),
        ("3.53.4", True),
        ("4.0", True),
    ],
)
def test_wal_reset_version_gate(version: str, safe: bool) -> None:
    assert sqlite_is_safe(version) is safe


def test_supervisor_refuses_to_start_on_affected_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(queue_mod.sqlite3, "sqlite_version", "3.50.4")
    with pytest.raises(UnsafeSQLite, match=r"3\.50\.4"):
        TaskQueue(tmp_path / "lab.db")
    assert not (tmp_path / "lab.db").exists()


def test_recovery_event_records_the_sqlite_version(tmp_path: Path) -> None:
    with TaskQueue(tmp_path / "lab.db", owner="sup") as q:
        q.add_task("t", idempotent=True)
        q.lease()  # same owner, so recover() treats it as a past life
        q.recover()
        detail = q._conn.execute(
            "SELECT detail FROM events WHERE kind = 'recovery'"
        ).fetchone()[0]
    assert queue_mod.sqlite3.sqlite_version in detail
