"""Versioned schema migrations (improvement plan item 3.1, #64)."""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lab.migrations import (
    MIGRATIONS_DIR,
    MigrationError,
    SchemaTooNew,
    current_version,
    discover,
    latest_version,
    migrate,
    split_statements,
)
from lab.queue import MAX_PAYLOAD_BYTES, MAX_RESULT_BYTES, PayloadTooLarge, TaskQueue

ROOT = Path(__file__).resolve().parent.parent
LEGACY = ROOT / "tests" / "fixtures" / "legacy_v0.sql"
TABLES = ("agents", "tasks", "leases", "approvals", "events", "operations")
OLD_COLUMNS = {
    "agents": ["id", "name", "kind", "capability_tier", "notes"],
    "tasks": ["id", "parent_id", "title", "payload", "state", "priority", "attempts",
              "max_attempts", "idempotent", "result", "last_error", "created_at"],
    "leases": ["id", "task_id", "owner", "holder", "expires_at", "released_at"],
    "approvals": ["id", "task_id", "reason", "state", "action_hash", "expires_at"],
    "events": ["id", "task_id", "kind", "from_state", "to_state", "detail"],
    "operations": ["id", "task_id", "tool", "params_sha256", "seq", "state"],
}


def raw(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def legacy_db(path: Path) -> None:
    conn = raw(path)
    conn.executescript(LEGACY.read_text(encoding="utf-8"))
    conn.close()


def snapshot(conn: sqlite3.Connection) -> dict[str, list[tuple[object, ...]]]:
    return {
        table: [tuple(r) for r in conn.execute(
            f"SELECT {', '.join(cols)} FROM {table} ORDER BY id")]  # nosemgrep
        for table, cols in OLD_COLUMNS.items()
    }


def shape(conn: sqlite3.Connection) -> dict[str, object]:
    """Everything about the schema that the code can observe."""
    out: dict[str, object] = {}
    for table in TABLES:
        # Sorted: ALTER ADD COLUMN appends, so an upgraded file lists the
        # late columns last. The code reads columns by name.
        out[f"{table}.columns"] = sorted(
            tuple(r)[1:] for r in conn.execute(f"PRAGMA table_info({table})"))  # nosemgrep
        out[f"{table}.fks"] = sorted(
            (r["table"], r["from"], r["to"], r["on_delete"])
            for r in conn.execute(f"PRAGMA foreign_key_list({table})"))  # nosemgrep
        out[f"{table}.indexes"] = sorted(
            r["name"] for r in conn.execute(f"PRAGMA index_list({table})")  # nosemgrep
            if not r["name"].startswith("sqlite_autoindex"))
    return out


def dir_with(tmp_path: Path, name: str, sql: str) -> Path:
    """Every shipped migration plus one more, numbered after the last.

    ``name`` is the part after the number, so adding a real migration does
    not collide with the fake one.
    """
    directory = tmp_path / "migrations"
    directory.mkdir()
    for found in discover():
        shutil.copy(found.path, directory / found.path.name)
    (directory / f"{latest_version() + 1:04d}_{name}").write_text(sql)
    return directory


# ------------------------------------------------------------ the runner


def test_fresh_database_is_at_the_latest_version(tmp_path: Path) -> None:
    with TaskQueue(tmp_path / "lab.db") as q:
        assert current_version(q._conn) == latest_version() == 16
        names = {r["name"] for r in q._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert set(TABLES) <= names
        assert "tasks_new" not in names


def test_reopening_changes_nothing(tmp_path: Path) -> None:
    db = tmp_path / "lab.db"
    with TaskQueue(db) as q:
        task_id = q.add_task("survives a reopen", {"k": "v"})
        before = shape(q._conn)
    with TaskQueue(db) as q:
        assert current_version(q._conn) == latest_version()
        assert shape(q._conn) == before
        task = q.get(task_id)
        assert task is not None and task.payload == {"k": "v"}


def test_migration_files_are_numbered_without_gaps() -> None:
    found = discover()
    assert [m.version for m in found] == list(range(1, len(found) + 1))
    assert all(m.path.parent == MIGRATIONS_DIR for m in found)
    assert latest_version() == len(found)


def test_gap_in_numbering_is_refused(tmp_path: Path) -> None:
    shutil.copy(MIGRATIONS_DIR / "0001_baseline.sql", tmp_path / "0001_baseline.sql")
    (tmp_path / "0003_skipped_two.sql").write_text("SELECT 1;\n")
    with pytest.raises(MigrationError, match="no gaps"):
        discover(tmp_path)


def test_badly_named_file_is_refused(tmp_path: Path) -> None:
    (tmp_path / "add_a_column.sql").write_text("SELECT 1;\n")
    with pytest.raises(MigrationError, match=r"NNNN_name\.sql"):
        discover(tmp_path)


@pytest.mark.safety
def test_database_from_a_newer_build_is_refused(tmp_path: Path) -> None:
    db = tmp_path / "lab.db"
    TaskQueue(db).close()
    conn = raw(db)
    conn.execute(f"PRAGMA user_version = {latest_version() + 1}")  # nosemgrep
    conn.close()
    with pytest.raises(SchemaTooNew, match="knows up to"):
        TaskQueue(db)


def test_split_statements_keeps_trigger_bodies_whole() -> None:
    script = (
        "-- a comment; with a semicolon\n"
        "CREATE TABLE t (a);\n"
        "CREATE TRIGGER g AFTER INSERT ON t BEGIN\n"
        "    SELECT 1;\n"
        "    SELECT 2;\n"
        "END;\n"
        "-- trailing comment\n"
    )
    statements = split_statements(script)
    assert len(statements) == 2
    assert "CREATE TABLE t" in statements[0]
    assert statements[1].startswith("CREATE TRIGGER")
    assert statements[1].rstrip().endswith("END;")


def test_unterminated_statement_is_refused() -> None:
    with pytest.raises(MigrationError, match="unterminated"):
        split_statements("CREATE TABLE t (a)")


# ------------------------------------------------- upgrading an old file


@pytest.mark.safety
def test_old_database_upgrades_with_its_data_intact(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    legacy_db(db)
    before = raw(db)
    assert current_version(before) == 0
    old = snapshot(before)
    before.close()

    with TaskQueue(db) as q:
        assert current_version(q._conn) == latest_version()
        assert snapshot(q._conn) == old
        # Columns added since the file was written get the value that era implies.
        assert {r[0] for r in q._conn.execute("SELECT executions FROM tasks")} == {0}
        assert {r[0] for r in q._conn.execute("SELECT generation FROM leases")} == {0}
        assert {r[0] for r in q._conn.execute("SELECT intent FROM approvals")} == {None}
        assert q._conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert q._conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_upgraded_database_has_the_same_shape_as_a_fresh_one(tmp_path: Path) -> None:
    legacy_db(tmp_path / "old.db")
    with TaskQueue(tmp_path / "old.db") as old, TaskQueue(tmp_path / "new.db") as new:
        assert shape(old._conn) == shape(new._conn)


def test_upgraded_database_still_runs_the_queue(tmp_path: Path) -> None:
    legacy_db(tmp_path / "old.db")
    with TaskQueue(tmp_path / "old.db", owner="fresh") as q:
        task = q.lease()
        assert task is not None and task.lease is not None
        q.start(task.lease)
        q.succeed(task.lease, {"upgraded": True})
        got = q.get(task.id)
        assert got is not None and got.state == "succeeded"
        q.add_task("new work after the upgrade")


def test_a_partly_upgraded_file_finishes_upgrading(tmp_path: Path) -> None:
    # A build from between items 1.7 and 3.1 has the newest column but no
    # version number.
    db = tmp_path / "half.db"
    legacy_db(db)
    conn = raw(db)
    conn.execute("ALTER TABLE tasks ADD COLUMN executions INTEGER NOT NULL DEFAULT 0")
    conn.execute("UPDATE tasks SET attempts = 1, executions = 1 WHERE id = 't-running'")
    conn.close()
    with TaskQueue(db) as q:
        assert current_version(q._conn) == latest_version()
        row = q._conn.execute("SELECT executions FROM tasks WHERE id = 't-running'").fetchone()
        assert row[0] == 1


# ------------------------------------------- an interrupted migration


@pytest.mark.safety
def test_failure_midway_rolls_the_whole_migration_back(tmp_path: Path) -> None:
    directory = dir_with(tmp_path, "half_done.sql", (
        "CREATE TABLE half_done (id INTEGER);\n"
        "INSERT INTO half_done VALUES (1);\n"
        "INSERT INTO no_such_table VALUES (1);\n"
    ))
    conn = raw(tmp_path / "lab.db")
    with pytest.raises(MigrationError, match="half_done"):
        migrate(conn, directory)
    assert current_version(conn) == latest_version()  # the shipped ones committed, the fake did not
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE name = 'half_done'").fetchone() is None
    assert not conn.in_transaction
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_a_migration_that_leaves_a_dangling_reference_is_rolled_back(tmp_path: Path) -> None:
    directory = dir_with(tmp_path, "orphan.sql", (
        "INSERT INTO approvals (id, task_id, reason, action_hash, expires_at)\n"
        "VALUES ('x', 'no-such-task', 'r', 'h', '2999-01-01');\n"
    ))
    conn = raw(tmp_path / "lab.db")
    with pytest.raises(MigrationError):
        migrate(conn, directory)
    assert current_version(conn) == latest_version()
    assert conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0


@pytest.mark.safety
def test_old_row_that_breaks_a_new_rule_rolls_back_and_keeps_the_data(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    legacy_db(db)
    conn = raw(db)
    conn.execute("UPDATE tasks SET max_attempts = 0 WHERE id = 't-failed'")
    conn.close()

    with pytest.raises(MigrationError, match="0002_check_constraints"):
        TaskQueue(db)

    conn = raw(db)
    assert current_version(conn) == 1  # the baseline committed, the rebuild did not
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 6
    assert conn.execute("SELECT max_attempts FROM tasks WHERE id = 't-failed'").fetchone()[0] == 0
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert "tasks_new" not in names
    assert "idx_tasks_runnable" in names


@pytest.mark.safety
def test_process_killed_mid_migration_leaves_the_last_good_version(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    legacy_db(db)
    # Dies after migration 2's SQL ran but before it committed.
    script = (
        "import os, signal\n"
        "from lab import migrations\n"
        "migrations.AFTER[2] = lambda conn: os.kill(os.getpid(), signal.SIGKILL)\n"
        "from lab.queue import TaskQueue\n"
        f"TaskQueue({str(db)!r})\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], cwd=str(ROOT),
        env={**os.environ, "PYTHONPATH": str(ROOT)}, capture_output=True, text=True,
        timeout=60)
    assert proc.returncode == -9, proc.stderr

    conn = raw(db)
    assert current_version(conn) == 1
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 6
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()

    with TaskQueue(db) as q:  # the next start finishes the job
        assert current_version(q._conn) == latest_version()
        assert q._conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 6


def test_processes_opening_one_fresh_file_together_all_succeed(tmp_path: Path) -> None:
    db = tmp_path / "shared.db"
    script = f"from lab.queue import TaskQueue\nTaskQueue({str(db)!r}).close()\n"
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    procs = [subprocess.Popen([sys.executable, "-c", script], cwd=str(ROOT), env=env,
                              stderr=subprocess.PIPE, text=True) for _ in range(4)]
    for p in procs:
        _, err = p.communicate(timeout=60)
        assert p.returncode == 0, err
    conn = raw(db)
    assert current_version(conn) == latest_version()
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


# ---------------------------------------------------- the CHECK rules


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("attempts", -1),
        ("max_attempts", 0),
        ("payload", "not json"),
        ("payload", '{"pad": "' + "x" * MAX_PAYLOAD_BYTES + '"}'),
        ("result", "not json"),
        ("result", '{"pad": "' + "x" * MAX_RESULT_BYTES + '"}'),
        ("executions", 1),  # more runs than claims
    ],
)
def test_schema_refuses_rows_that_break_the_budgets(
    tmp_path: Path, column: str, value: object,
) -> None:
    with TaskQueue(tmp_path / "lab.db") as q:
        task_id = q.add_task("target")
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            q._conn.execute(  # nosemgrep
                f"UPDATE tasks SET {column} = ? WHERE id = ?", (value, task_id))


def test_add_task_refuses_an_oversized_payload_with_a_clear_error(tmp_path: Path) -> None:
    with TaskQueue(tmp_path / "lab.db") as q:
        with pytest.raises(PayloadTooLarge, match="payload"):
            q.add_task("big", {"pad": "x" * MAX_PAYLOAD_BYTES})
        assert q._conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_limit_counts_bytes_not_characters(tmp_path: Path) -> None:
    # Each of these is 6 bytes once JSON-escaped, or 2 in raw UTF-8; either
    # way the encoded size, not the character count, decides.
    with TaskQueue(tmp_path / "lab.db") as q, pytest.raises(PayloadTooLarge):
        q.add_task("wide", {"pad": "é" * MAX_PAYLOAD_BYTES})


def test_payload_just_under_the_limit_is_accepted(tmp_path: Path) -> None:
    with TaskQueue(tmp_path / "lab.db") as q:
        overhead = len('{"pad": ""}')
        task_id = q.add_task("fits", {"pad": "x" * (MAX_PAYLOAD_BYTES - overhead - 1)})
        assert q.get(task_id) is not None


def test_oversized_result_is_refused_and_the_task_stays_running(tmp_path: Path) -> None:
    with TaskQueue(tmp_path / "lab.db") as q:
        q.add_task("produces too much")
        task = q.lease()
        assert task is not None and task.lease is not None
        q.start(task.lease)
        with pytest.raises(PayloadTooLarge, match="result"):
            q.succeed(task.lease, {"pad": "x" * MAX_RESULT_BYTES})
        got = q.get(task.id)
        assert got is not None and got.state == "running"
        q.succeed(task.lease, {"ok": True})


# ------------------------------------------------ migration 14: chat origin


def _at_version(tmp_path: Path, version: int) -> Path:
    """A database migrated only up to ``version``, through the real runner."""
    directory = tmp_path / f"migrations_upto_{version}"
    directory.mkdir()
    for found in discover():
        if found.version <= version:
            shutil.copy(found.path, directory / found.path.name)
    db = tmp_path / "v.db"
    conn = raw(db)
    conn.execute("PRAGMA journal_mode = WAL")
    assert migrate(conn, directory) == version
    conn.close()
    return db


@pytest.mark.safety
def test_migration_14_keeps_every_origin_and_taint_as_it_was(tmp_path: Path) -> None:
    db = _at_version(tmp_path, 13)
    conn = raw(db)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO tasks (id, title, origin_type) VALUES ('c', 't', 'chat')")
    for n, (origin, tainted, sensitivity) in enumerate(
            [("operator", 0, "internal"), ("web", 1, "public"), ("event", 1, "secret"),
             ("unknown", 1, "internal")]):
        conn.execute(
            "INSERT INTO tasks (id, title, origin_type, origin_id, tainted, sensitivity, "
            "payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"t{n}", f"title {n}", origin, f"src-{n}", tainted, sensitivity, '{"k": 1}'))
    before = [tuple(r) for r in conn.execute("SELECT * FROM tasks ORDER BY id")]
    conn.close()

    with TaskQueue(db) as q:
        assert current_version(q._conn) == latest_version()
        after = [tuple(r) for r in q._conn.execute(
            "SELECT id, parent_id, title, payload, state, priority, agent_kind, weight, "
            "capability_tier, idempotent, attempts, executions, max_attempts, last_error, "
            "result, created_at, updated_at, available_at, origin_type, origin_id, "
            "origin_sha256, acquired_at, sensitivity, delegated_by, tainted "
            "FROM tasks ORDER BY id")]
        assert after == before
        assert q._conn.execute("PRAGMA foreign_key_check").fetchall() == []
        # 'chat' is now a known origin, and it is tainted like any outside input.
        q._conn.execute("INSERT INTO tasks (id, title, origin_type) VALUES ('c', 't', 'chat')")
        assert q._conn.execute("SELECT tainted FROM tasks WHERE id = 'c'").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            q._conn.execute("INSERT INTO tasks (id, title, origin_type) VALUES ('x', 't', 'phone')")
        assert q._conn.execute("SELECT next_update_id FROM chat_state").fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError):
            q._conn.execute("INSERT INTO chat_state (id) VALUES (2)")
        with pytest.raises(sqlite3.IntegrityError):
            q._conn.execute("UPDATE chat_state SET next_update_id = -1")
        with pytest.raises(sqlite3.IntegrityError):
            q._conn.execute("INSERT INTO chat_updates (update_id, chat_id, action, task_id) "
                            "VALUES (1, 2, 'task_created', 'no-such-task')")


# --------------------------------------- migration 15: memory proposals (#253)


def test_migration_15_keeps_memories_and_adds_an_unsearchable_proposal_table(
        tmp_path: Path) -> None:
    db = _at_version(tmp_path, 14)
    conn = raw(db)
    conn.execute("INSERT INTO memories (kind, text, text_sha256, source_id, trust, created_by) "
                 "VALUES ('curated', 'kept fact', ?, 's', 'trusted', 'roshan')", ("a" * 64,))
    conn.close()
    with TaskQueue(db) as q:
        assert current_version(q._conn) == latest_version()
        row = q._conn.execute("SELECT text, proposal_id FROM memories").fetchone()
        assert tuple(row) == ("kept fact", None)
        assert q._conn.execute("PRAGMA foreign_key_check").fetchall() == []
        insert = ("INSERT INTO memory_proposals (task_id, text, text_sha256, source_id, reason, "
                  "tainted, state, signature, decided_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)")
        q._conn.execute(insert, ("t", "x", "b" * 64, "s", "r", 1, "pending", None, None))
        for bad in [("t", "", "c" * 64, "s", "r", 0, "pending", None, None),        # no text
                    ("t", "y", "short", "s", "r", 0, "pending", None, None),        # bad hash
                    ("t", "y", "d" * 64, "s", "r", 2, "pending", None, None),       # taint 0/1
                    ("t", "y", "e" * 64, "s", "r", 0, "active", None, None),        # no 'active'
                    ("t", "y", "f" * 64, "s", "r", 0, "accepted", None, "x"),       # unsigned
                    ("t", "x", "b" * 64, "s", "r", 1, "pending", None, None)]:      # duplicate
            with pytest.raises(sqlite3.IntegrityError):
                q._conn.execute(insert, bad)
        # The full-text index covers memories only, so a proposal is not in it.
        assert q._conn.execute(
            "SELECT COUNT(*) FROM memories_fts WHERE memories_fts MATCH 'x'").fetchone()[0] == 0


# ------------------------------------ migration 16: repository provenance (ADR 0008)


def test_migration_16_adds_a_provenance_table_that_refuses_a_partial_record(
        tmp_path: Path) -> None:
    db = _at_version(tmp_path, 15)
    with TaskQueue(db) as q:
        assert current_version(q._conn) == latest_version()
        insert = ("INSERT INTO workspace_acquisitions (task_id, source, source_path, "
                  "source_sha256, signed_by, revision, tree, directory, workspace) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)")
        good = ("t", "project", "/srv/project", "a" * 64, "roshan", "b" * 40, "c" * 40,
                "repo", "task-t-1")
        q._conn.execute(insert, good)
        for index, bad in ((3, "short"), (4, ""), (5, "main"), (6, "d" * 64), (7, ""),
                           (1, "x" * 65)):
            row = list(good)
            row[index] = bad
            with pytest.raises(sqlite3.IntegrityError):
                q._conn.execute(insert, tuple(row))
        assert q._conn.execute(
            "SELECT acquired_at FROM workspace_acquisitions").fetchone()[0]
