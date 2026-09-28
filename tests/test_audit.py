"""Audit log that cannot be quietly rewritten (item 3.2, #65)."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from lab.audit import (
    GENESIS,
    CheckpointError,
    append_event,
    check_against_checkpoint,
    event_hash,
    make_checkpoint,
    verify_chain,
    write_checkpoint,
)
from lab.broker import ExecutionBroker, ExecutionContext
from lab.journal import OperationJournal
from lab.policy import PolicyEngine
from lab.queue import TaskQueue

KEY = b"0123456789abcdef-test-key"


@pytest.fixture()
def key_file(tmp_path: Path) -> Path:
    path = tmp_path / "audit.key"
    path.write_bytes(KEY)
    return path


@pytest.fixture()
def q(tmp_path: Path) -> TaskQueue:
    with TaskQueue(tmp_path / "lab.db") as queue:
        yield queue


def populate(q: TaskQueue, n: int = 5) -> None:
    for i in range(n):
        append_event(q._conn, f"t{i}", "note", detail={"i": i})


def drop_triggers(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TRIGGER events_no_update")
    conn.execute("DROP TRIGGER events_no_delete")


# --------------------------------------------------------- append-only


@pytest.mark.safety
def test_editing_an_event_is_refused(q: TaskQueue) -> None:
    populate(q)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        q._conn.execute("UPDATE events SET kind = 'forged' WHERE id = 1")


@pytest.mark.safety
def test_deleting_an_event_is_refused(q: TaskQueue) -> None:
    populate(q)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        q._conn.execute("DELETE FROM events WHERE id = 1")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        q._conn.execute("DELETE FROM events")


@pytest.mark.safety
def test_deleting_a_task_does_not_delete_its_history(q: TaskQueue) -> None:
    task_id = q.add_task("doomed")
    assert q.events(task_id)
    q._conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    assert q.events(task_id), "history must outlive the task row"


@pytest.mark.safety
def test_a_task_with_a_lease_cannot_be_deleted(q: TaskQueue) -> None:
    q.add_task("leased")
    leased = q.lease()
    assert leased is not None
    with pytest.raises(sqlite3.IntegrityError):
        q._conn.execute("DELETE FROM tasks WHERE id = ?", (leased.id,))


# ------------------------------------------------------------- the chain


def test_normal_operation_produces_a_valid_chain(q: TaskQueue) -> None:
    q.add_task("a")
    task = q.lease()
    assert task is not None and task.lease is not None
    q.start(task.lease)
    q.succeed(task.lease, {"ok": True})
    report = verify_chain(q._conn)
    assert report.ok and report.events >= 4 and report.unchained == 0


def test_the_first_row_chains_onto_genesis(q: TaskQueue) -> None:
    append_event(q._conn, None, "first")
    row = q._conn.execute("SELECT prev_hash, hash FROM events").fetchone()
    assert row["prev_hash"] == GENESIS
    assert row["hash"] == event_hash(GENESIS, None, "first", None, None, None,
                                     q._conn.execute("SELECT created_at FROM events").fetchone()[0])


@pytest.mark.safety
def test_an_edited_row_breaks_the_chain(q: TaskQueue) -> None:
    populate(q)
    drop_triggers(q._conn)
    q._conn.execute("UPDATE events SET detail = '{\"i\": 99}' WHERE id = 3")
    report = verify_chain(q._conn)
    assert not report.ok and report.bad_id == 3 and "content" in (report.problem or "")


@pytest.mark.safety
def test_a_removed_row_breaks_the_chain(q: TaskQueue) -> None:
    populate(q)
    drop_triggers(q._conn)
    q._conn.execute("DELETE FROM events WHERE id = 3")
    report = verify_chain(q._conn)
    assert not report.ok and report.bad_id == 4 and "removed" in (report.problem or "")


def test_a_failed_transaction_leaves_no_event_and_no_fork(q: TaskQueue) -> None:
    populate(q, 2)
    q._conn.execute("BEGIN IMMEDIATE")
    append_event(q._conn, "x", "inside")
    q._conn.execute("ROLLBACK")
    populate(q, 2)
    report = verify_chain(q._conn)
    assert report.ok and report.events == 4


def test_two_writers_never_fork_the_chain(tmp_path: Path) -> None:
    db = tmp_path / "lab.db"
    with TaskQueue(db):
        pass

    def writer(name: str) -> None:
        conn = sqlite3.connect(db, isolation_level=None, timeout=30)
        conn.execute("PRAGMA busy_timeout = 30000")
        for i in range(40):
            append_event(conn, name, "note", detail={"i": i})
        conn.close()

    threads = [threading.Thread(target=writer, args=(f"w{n}",)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    conn = sqlite3.connect(db)
    report = verify_chain(conn)
    assert report.ok and report.events == 160


# ------------------------------------------------------------ checkpoints


def rewrite_everything(conn: sqlite3.Connection) -> None:
    """What an attacker with full write access does: change a row, then
    recompute every hash after it so the chain is internally consistent."""
    conn.row_factory = sqlite3.Row
    drop_triggers(conn)
    conn.execute("UPDATE events SET detail = '{\"i\": 999}' WHERE id = 2")
    prev = GENESIS
    for row in conn.execute("SELECT * FROM events ORDER BY id").fetchall():
        new = event_hash(prev, row["task_id"], row["kind"], row["from_state"],
                         row["to_state"], row["detail"], row["created_at"])
        conn.execute("UPDATE events SET prev_hash = ?, hash = ? WHERE id = ?",
                     (prev, new, row["id"]))
        prev = new


@pytest.mark.safety
def test_a_whole_chain_rewrite_is_caught_by_the_checkpoint(
        q: TaskQueue, key_file: Path, tmp_path: Path) -> None:
    populate(q)
    path = write_checkpoint(q._conn, key_file, tmp_path / "outside")
    assert check_against_checkpoint(q._conn, path, key_file).ok

    rewrite_everything(q._conn)
    assert verify_chain(q._conn).ok, "the rewrite is internally consistent, by construction"
    report = check_against_checkpoint(q._conn, path, key_file)
    assert not report.ok and "rewritten" in (report.problem or "")


def test_appending_after_a_checkpoint_is_still_valid(
        q: TaskQueue, key_file: Path, tmp_path: Path) -> None:
    populate(q)
    path = write_checkpoint(q._conn, key_file, tmp_path / "outside")
    populate(q, 3)
    report = check_against_checkpoint(q._conn, path, key_file)
    assert report.ok and report.events == 8


def test_a_forged_checkpoint_is_rejected(q: TaskQueue, key_file: Path, tmp_path: Path) -> None:
    populate(q)
    path = write_checkpoint(q._conn, key_file, tmp_path / "outside")
    body = json.loads(path.read_text())
    body["last_hash"] = "f" * 64
    path.write_text(json.dumps(body))
    with pytest.raises(CheckpointError, match="signature"):
        check_against_checkpoint(q._conn, path, key_file)


def test_the_wrong_key_is_rejected(q: TaskQueue, key_file: Path, tmp_path: Path) -> None:
    populate(q)
    path = write_checkpoint(q._conn, key_file, tmp_path / "outside")
    other = tmp_path / "other.key"
    other.write_bytes(b"a-completely-different-key")
    with pytest.raises(CheckpointError, match="signature"):
        check_against_checkpoint(q._conn, path, other)


def test_a_short_key_is_refused(q: TaskQueue, tmp_path: Path) -> None:
    short = tmp_path / "short.key"
    short.write_bytes(b"abc")
    with pytest.raises(CheckpointError, match="at least"):
        make_checkpoint(q._conn, short)


def test_a_broken_chain_is_never_signed(q: TaskQueue, key_file: Path) -> None:
    populate(q)
    drop_triggers(q._conn)
    q._conn.execute("DELETE FROM events WHERE id = 2")
    with pytest.raises(CheckpointError, match="broken chain"):
        make_checkpoint(q._conn, key_file)


# ----------------------------------------------------- broker call audit


@pytest.fixture()
def broker_setup(tmp_path: Path, q: TaskQueue):
    q._conn.execute("INSERT INTO tasks (id, title) VALUES ('t1', 't1')")
    task = q.lease()
    assert task is not None and task.lease is not None
    ctx = ExecutionContext("t1", "test", task.attempts, task.lease)
    broker = ExecutionBroker(workspace_root=tmp_path / "ws",
                             policy=PolicyEngine(q._conn), leases=q.owns_lease,
                             journal=OperationJournal(q._conn))
    broker.open_workspace("t1", {"fs.write", "fs.read"})
    return broker, ctx, q


def calls(q: TaskQueue) -> list[dict]:
    return [json.loads(r["detail"]) for r in q._conn.execute(
        "SELECT detail FROM events WHERE kind = 'broker_call' ORDER BY id")]


def test_every_broker_call_records_tool_hash_generation_decision_result(broker_setup) -> None:
    broker, ctx, q = broker_setup
    broker.session(ctx).submit("fs.write", path="a.txt", content="hi")
    (entry,) = calls(q)
    assert entry["tool"] == "fs.write"
    assert len(entry["params_sha256"]) == 64
    assert entry["lease_generation"] == ctx.lease.generation
    assert entry["lease_id"] == ctx.lease.lease_id
    assert entry["decision"] == "allow" and entry["ok"] is True and entry["error"] is None
    assert "hi" not in json.dumps(entry), "parameters are hashed, never logged"


def test_a_call_the_tool_refuses_is_recorded_too(broker_setup) -> None:
    broker, ctx, q = broker_setup
    result = broker.session(ctx).submit("fs.read", path="../../etc/passwd")
    assert not result.ok
    (entry,) = calls(q)
    # Policy allowed the call; the tool itself refused the path.
    assert entry["decision"] == "allow" and entry["ok"] is None
    assert entry["error"] == "PathEscape"


def test_a_call_with_a_dead_lease_is_recorded_as_refused(broker_setup) -> None:
    broker, ctx, q = broker_setup
    q.cancel("t1")
    result = broker.session(ctx).submit("fs.write", path="a", content="x")
    assert not result.ok
    (entry,) = calls(q)
    assert entry["decision"] == "refused" and entry["error"] == "ContextRevoked"


# ----------------------------------------------------------------- CLI


def run_cli(db: Path, *argv: str) -> int:
    from lab.cli import main
    return main(["--db", str(db), *argv])


def test_cli_verify_checkpoint_and_check(tmp_path: Path, key_file: Path, capsys) -> None:
    db = tmp_path / "lab.db"
    with TaskQueue(db) as q:
        populate(q)
    assert run_cli(db, "audit", "verify") == 0
    assert "ok: 5 chained events" in capsys.readouterr().out
    out = tmp_path / "outside"
    assert run_cli(db, "audit", "checkpoint", "--key", str(key_file), "--out", str(out)) == 0
    (cp,) = out.glob("checkpoint-*.json")
    assert run_cli(db, "audit", "check", "--key", str(key_file), "--checkpoint", str(cp)) == 0

    conn = sqlite3.connect(db, isolation_level=None)
    conn.row_factory = sqlite3.Row
    rewrite_everything(conn)
    conn.close()
    assert run_cli(db, "audit", "verify") == 0
    assert run_cli(db, "audit", "check", "--key", str(key_file), "--checkpoint", str(cp)) == 1
    assert "rewritten" in capsys.readouterr().out


def test_cli_verify_reports_a_break_and_never_writes(tmp_path: Path, capsys) -> None:
    db = tmp_path / "lab.db"
    with TaskQueue(db) as q:
        populate(q)
        drop_triggers(q._conn)
        q._conn.execute("DELETE FROM events WHERE id = 2")
    assert run_cli(db, "audit", "verify") == 1
    assert "BROKEN at event 3" in capsys.readouterr().out


# ------------------------------------------------ upgrade from before 3.2


def test_rows_from_before_the_chain_are_kept_and_unchained(tmp_path: Path) -> None:
    legacy = Path(__file__).parent / "fixtures" / "legacy_v0.sql"
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(legacy.read_text(encoding="utf-8"))
    before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    conn.close()
    with TaskQueue(db) as q:
        assert q._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] >= before
        assert verify_chain(q._conn).unchained == before
        append_event(q._conn, None, "after_upgrade")
        report = verify_chain(q._conn)
        assert report.ok and report.events == 1 and report.unchained == before
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            q._conn.execute("DELETE FROM events WHERE id = 1")
