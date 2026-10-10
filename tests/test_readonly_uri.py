"""Read-only opens percent-encode the database path (#386).

A ``?`` or ``#`` in a database path used to end the file name early, so a
read-only open could open, and create, a different file. Each test keeps the
database in a folder named ``a?b#c%41``. It checks that the read sees the real
data, that nothing else appears beside that folder, and that the database
bytes do not change.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lab import backup, dashboard, deadman, doctor, selftest
from lab.cli import main
from lab.db import connect_readonly
from lab.queue import TaskQueue

ODD = "a?b#c%41"


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    """A lab database with one queued task, inside ``parent/a?b#c%41``."""
    folder = tmp_path / "parent" / ODD
    folder.mkdir(parents=True)
    path = folder / "lab.db"
    with TaskQueue(path) as queue:
        queue.add_task("odd folder")
    return path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assert_nothing_else_appeared(db: Path, digest: str) -> None:
    """The odd folder is the only entry in its parent, and the database bytes are unchanged."""
    assert sorted(os.listdir(db.parent.parent)) == [ODD]
    assert _digest(db) == digest


def test_connect_readonly_reads_the_named_database(db: Path) -> None:
    digest = _digest(db)
    conn = connect_readonly(db)
    try:
        rows = conn.execute("SELECT state, COUNT(*) FROM tasks GROUP BY state").fetchall()
    finally:
        conn.close()
    assert rows == [("queued", 1)]
    _assert_nothing_else_appeared(db, digest)


def test_connect_readonly_refuses_writes(db: Path) -> None:
    digest = _digest(db)
    conn = connect_readonly(db)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("CREATE TABLE intruder (x)")
    finally:
        conn.close()
    _assert_nothing_else_appeared(db, digest)


def test_connect_readonly_never_creates_a_missing_file(db: Path) -> None:
    missing = db.parent / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        connect_readonly(missing)
    assert not missing.exists()
    assert sorted(os.listdir(db.parent.parent)) == [ODD]


def test_connect_readonly_passes_keyword_arguments_on(db: Path) -> None:
    conn = connect_readonly(db, timeout=5)
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        conn.close()


def test_status_reads_the_named_database(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    digest = _digest(db)
    assert main(["--db", str(db), "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["states"] == {"queued": 1}
    _assert_nothing_else_appeared(db, digest)


def test_audit_verify_reads_the_named_database(db: Path,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    digest = _digest(db)
    assert main(["--db", str(db), "audit", "verify"]) == 0
    assert capsys.readouterr().out.startswith("ok:")
    _assert_nothing_else_appeared(db, digest)


def test_keepawake_once_reads_the_named_database(db: Path,
                                                 capsys: pytest.CaptureFixture[str]) -> None:
    digest = _digest(db)
    assert main(["--db", str(db), "keepawake", "--once"]) == 0
    assert capsys.readouterr().out.startswith("hold:")
    _assert_nothing_else_appeared(db, digest)


def test_repo_acquired_reads_the_named_database(db: Path,
                                                capsys: pytest.CaptureFixture[str]) -> None:
    digest = _digest(db)
    assert main(["--db", str(db), "repo", "acquired"]) == 0
    assert "no repository has been acquired" in capsys.readouterr().out
    _assert_nothing_else_appeared(db, digest)


def test_dashboard_reads_the_named_database(db: Path) -> None:
    digest = _digest(db)
    assert "<td>queued</td><td class=n>1</td>" in dashboard.render_page(db)
    _assert_nothing_else_appeared(db, digest)


def test_deadman_health_reads_the_named_database(db: Path) -> None:
    digest = _digest(db)
    assert deadman.lab_health(db) == (True, "ok")
    _assert_nothing_else_appeared(db, digest)


def test_selftest_checks_read_the_named_database(db: Path) -> None:
    digest = _digest(db)
    # selftest.run() also writes its result to the log, so this calls the read-only checks.
    assert selftest._check_chain(db).ok
    assert selftest._check_health(db).ok
    assert selftest._check_backup(db).ok
    _assert_nothing_else_appeared(db, digest)


def test_doctor_checks_read_the_named_database(db: Path) -> None:
    digest = _digest(db)
    assert doctor.check_database(db).ok
    assert doctor.check_audit_chain(db).ok
    _assert_nothing_else_appeared(db, digest)


def test_backup_copies_the_named_database(db: Path, tmp_path: Path) -> None:
    digest = _digest(db)
    manifest = backup.backup(db, tmp_path / "backups")
    assert backup.restore_check(manifest, tmp_path / "restored").ok
    _assert_nothing_else_appeared(db, digest)


def test_rotate_reads_backups_in_a_named_folder(db: Path, tmp_path: Path) -> None:
    dest = tmp_path / "keep" / "b?c#d%41"
    start = datetime(2026, 1, 1, tzinfo=UTC)
    backup.backup(db, dest, now=start)
    backup.backup(db, dest, now=start + timedelta(hours=1))
    report = backup.rotate(dest, keep=1)
    assert len(report.removed) == 1
    assert report.left_alone == []
    assert sorted(os.listdir(dest.parent)) == [dest.name]
