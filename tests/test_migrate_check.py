"""``lab migrate --check`` (#356): the new build's migrations, tried on a copy.

The live database is only read. Each test checks that its file is byte for
byte the same afterwards and that no copy is left beside it.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from pathlib import Path

import pytest

import lab.migrations as migrations_mod
from lab.cli import main
from lab.migrations import (
    CopyCheck,
    MigrationError,
    SchemaTooNew,
    check_on_copy,
    current_version,
    discover,
    latest_version,
    migrate,
)


def _raw(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path, isolation_level=None)


def _migrations_up_to(tmp_path: Path, version: int) -> Path:
    directory = tmp_path / f"migrations_upto_{version}"
    directory.mkdir()
    for found in discover():
        if found.version <= version:
            shutil.copy(found.path, directory / found.path.name)
    return directory


def _at_version(tmp_path: Path, version: int) -> Path:
    """A WAL database migrated only up to ``version``, holding one task."""
    folder = tmp_path / "data"
    folder.mkdir()
    db = folder / "lab.db"
    conn = _raw(db)
    conn.execute("PRAGMA journal_mode = WAL")
    assert migrate(conn, _migrations_up_to(tmp_path, version)) == version
    conn.execute("INSERT INTO tasks (id, title) VALUES ('kept', 'before the check')")
    conn.close()
    return db


def _with_one_more(tmp_path: Path, sql: str) -> Path:
    """Every shipped migration plus ``sql`` as the next number."""
    directory = tmp_path / "migrations_plus_one"
    directory.mkdir()
    for found in discover():
        shutil.copy(found.path, directory / found.path.name)
    (directory / f"{latest_version() + 1:04d}_check_test.sql").write_text(sql)
    return directory


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _left_beside(db: Path) -> list[str]:
    """Anything a check could leave behind: its folder or a snapshot."""
    return sorted(p.name for p in db.parent.iterdir()
                  if ".check-" in p.name or ".pre-v" in p.name)


def test_an_old_database_migrates_on_the_copy_and_is_itself_unchanged(
        tmp_path: Path) -> None:
    db = _at_version(tmp_path, 5)
    before = _digest(db)

    assert check_on_copy(db) == CopyCheck(5, latest_version())

    assert _digest(db) == before
    conn = _raw(db)
    assert current_version(conn) == 5
    assert conn.execute("SELECT title FROM tasks WHERE id = 'kept'").fetchone() == (
        "before the check",)
    conn.close()
    assert _left_beside(db) == []


def test_a_failing_migration_is_reported_and_the_database_is_unchanged(
        tmp_path: Path) -> None:
    db = _at_version(tmp_path, latest_version())
    before = _digest(db)
    # The table already exists, so this migration fails and rolls back.
    directory = _with_one_more(tmp_path, "CREATE TABLE tasks (x);\n")

    with pytest.raises(MigrationError, match="failed and was rolled back"):
        check_on_copy(db, directory)

    assert _digest(db) == before
    assert current_version(_raw(db)) == latest_version()
    assert _left_beside(db) == []


def test_a_current_database_is_checked_and_not_changed(tmp_path: Path) -> None:
    db = _at_version(tmp_path, latest_version())
    before = _digest(db)

    assert check_on_copy(db) == CopyCheck(latest_version(), latest_version())

    assert _digest(db) == before
    assert _left_beside(db) == []


def test_a_database_from_a_newer_build_is_refused_on_the_copy(tmp_path: Path) -> None:
    db = _at_version(tmp_path, latest_version())
    conn = _raw(db)
    conn.execute(f"PRAGMA user_version = {latest_version() + 1}")
    conn.close()
    before = _digest(db)

    with pytest.raises(SchemaTooNew):
        check_on_copy(db)

    assert _digest(db) == before
    assert _left_beside(db) == []


def test_a_missing_database_is_reported_and_not_created(tmp_path: Path) -> None:
    db = tmp_path / "lab.db"

    with pytest.raises(MigrationError, match="no database file"):
        check_on_copy(db)

    assert list(tmp_path.iterdir()) == []


def test_a_file_that_is_not_a_database_leaves_nothing_behind(tmp_path: Path) -> None:
    db = tmp_path / "lab.db"
    db.write_bytes(b"not sqlite at all, " * 100)

    with pytest.raises(sqlite3.DatabaseError):
        check_on_copy(db)

    assert [p.name for p in tmp_path.iterdir()] == ["lab.db"]


def test_a_database_owned_by_another_account_is_not_opened(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _at_version(tmp_path, 5)
    files = sorted(p.name for p in db.parent.iterdir())
    owner = db.stat().st_uid
    monkeypatch.setattr(migrations_mod.os, "geteuid", lambda: owner + 1)

    with pytest.raises(MigrationError, match="another account"):
        check_on_copy(db)

    assert sorted(p.name for p in db.parent.iterdir()) == files


def test_the_check_runs_while_the_service_holds_the_database_open(
        tmp_path: Path) -> None:
    db = _at_version(tmp_path, latest_version())
    service = _raw(db)
    service.execute("INSERT INTO tasks (id, title) VALUES ('fresh', 'still in the WAL')")

    assert check_on_copy(db) == CopyCheck(latest_version(), latest_version())

    assert service.execute("SELECT count(*) FROM tasks").fetchone() == (2,)
    service.close()
    assert _left_beside(db) == []


def test_the_command_reports_an_upgrade_on_the_copy(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = _at_version(tmp_path, 5)
    before = _digest(db)

    assert main(["--db", str(db), "migrate", "--check"]) == 0

    out = capsys.readouterr().out
    assert f"from schema version 5 to {latest_version()}" in out
    assert "not changed" in out
    assert _digest(db) == before


def test_the_command_exits_1_with_the_reason_when_the_check_fails(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = _at_version(tmp_path, latest_version())
    conn = _raw(db)
    conn.execute(f"PRAGMA user_version = {latest_version() + 1}")
    conn.close()

    assert main(["--db", str(db), "migrate", "--check"]) == 1

    err = capsys.readouterr().err
    assert "migrate --check failed" in err
    assert "newer" in err or "knows up to" in err


def test_the_command_does_not_create_a_missing_database(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "lab.db"

    assert main(["--db", str(db), "migrate", "--check"]) == 1

    assert "no database file" in capsys.readouterr().err
    assert not db.exists()
