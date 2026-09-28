"""Backup, restore drill and recovery drills (item 3.4, #67; drills #91)."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from lab import backup, drills
from lab.artifacts import ArtifactStore
from lab.audit import append_event
from lab.broker import Workspace
from lab.cli import main
from lab.queue import TaskQueue


@pytest.fixture()
def live(tmp_path: Path):
    """A live database in WAL mode with tasks, events and artifacts, still open."""
    db = tmp_path / "live" / "lab.db"
    db.parent.mkdir()
    q = TaskQueue(db)
    for i in range(3):
        q.add_task(f"task {i}")
    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    (ws_root / "a.txt").write_bytes(b"alpha")
    (ws_root / "b.txt").write_bytes(b"beta")
    store = ArtifactStore(db.parent / "artifacts", q._conn)
    store.ingest_workspace(Workspace(ws_root), "t1", 1)
    yield q, db, store
    q.close()


def test_backup_then_restore_verifies_everything(tmp_path: Path, live) -> None:
    _q, db, store = live
    manifest = backup.backup(db, tmp_path / "bk", store.root)
    data = json.loads(manifest.read_text())
    assert data["audit_chain_ok"] and data["schema_version"] >= 4
    assert data["artifact_blobs"] == 2 and data["artifact_blobs_copied"] == 2

    report = backup.restore_check(manifest, tmp_path / "restored")
    assert report.ok, report.problems
    assert report.artifacts_checked == 2 and report.events >= 3
    with TaskQueue(tmp_path / "restored" / "lab.db") as restored:
        assert restored.counts() == {"queued": 3}


def test_the_source_is_untouched_and_stays_writable(tmp_path: Path, live) -> None:
    q, db, store = live
    before = q.counts()
    backup.backup(db, tmp_path / "bk", store.root)
    assert q.counts() == before
    q.add_task("after the backup")
    assert q.counts()["queued"] == 4


def test_a_backup_taken_during_writes_is_consistent(tmp_path: Path, live) -> None:
    _q, db, store = live
    writer = sqlite3.connect(db, isolation_level=None, timeout=30)
    writer.execute("BEGIN IMMEDIATE")
    append_event(writer, None, "uncommitted")     # inside an open transaction
    manifest = backup.backup(db, tmp_path / "bk", store.root)
    writer.execute("ROLLBACK")
    writer.close()
    report = backup.restore_check(manifest, tmp_path / "restored")
    assert report.ok, report.problems


def test_second_backup_copies_no_blobs(tmp_path: Path, live) -> None:
    _q, db, store = live
    backup.backup(db, tmp_path / "bk", store.root)
    import time
    time.sleep(1.1)   # names carry a one-second stamp
    second = json.loads(backup.backup(db, tmp_path / "bk", store.root).read_text())
    assert second["artifact_blobs"] == 2 and second["artifact_blobs_copied"] == 0


@pytest.mark.safety
def test_a_corrupted_backup_fails_the_restore(tmp_path: Path, live) -> None:
    _q, db, store = live
    manifest = backup.backup(db, tmp_path / "bk", store.root)
    snapshot = manifest.parent / json.loads(manifest.read_text())["database"]
    raw = bytearray(snapshot.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    snapshot.write_bytes(bytes(raw))
    report = backup.restore_check(manifest, tmp_path / "restored")
    assert not report.ok and any("hash" in p for p in report.problems)


@pytest.mark.safety
def test_a_tampered_audit_log_in_the_backup_fails_the_restore(tmp_path: Path, live) -> None:
    _q, db, store = live
    manifest = backup.backup(db, tmp_path / "bk", store.root)
    snapshot = manifest.parent / json.loads(manifest.read_text())["database"]
    conn = sqlite3.connect(snapshot, isolation_level=None)
    conn.execute("DROP TRIGGER events_no_update")
    conn.execute("UPDATE events SET kind = 'forged' WHERE id = (SELECT MAX(id) FROM events)")
    conn.close()
    # Keep the file hash honest so the chain check is what catches it.
    data = json.loads(manifest.read_text())
    data["database_sha256"] = backup._sha256_file(snapshot)
    data["database_bytes"] = snapshot.stat().st_size
    manifest.write_text(json.dumps(data))
    report = backup.restore_check(manifest, tmp_path / "restored")
    assert not report.ok and any("audit chain" in p for p in report.problems)


@pytest.mark.safety
def test_an_altered_artifact_blob_fails_the_restore(tmp_path: Path, live) -> None:
    _q, db, store = live
    manifest = backup.backup(db, tmp_path / "bk", store.root)
    blob = next(p for p in (manifest.parent / "artifacts").rglob("*") if p.is_file())
    os.chmod(blob, 0o600)
    blob.write_bytes(b"replaced")
    report = backup.restore_check(manifest, tmp_path / "restored")
    assert not report.ok and any("artifact" in p for p in report.problems)


def test_a_missing_source_blob_fails_the_backup(tmp_path: Path, live) -> None:
    _q, db, store = live
    victim = store.blob_path(next(iter(store.for_task("t1")))["sha256"])
    os.chmod(victim, 0o600)
    victim.unlink()
    with pytest.raises(backup.BackupError, match="missing"):
        backup.backup(db, tmp_path / "bk", store.root)
    assert not list((tmp_path / "bk").glob("*.db")), "no half backup left behind"


def test_restore_refuses_a_non_empty_target(tmp_path: Path, live) -> None:
    _q, db, store = live
    manifest = backup.backup(db, tmp_path / "bk", store.root)
    target = tmp_path / "occupied"
    target.mkdir()
    (target / "precious").write_text("do not overwrite")
    with pytest.raises(backup.BackupError, match="not empty"):
        backup.restore_check(manifest, target)
    assert (target / "precious").read_text() == "do not overwrite"


def test_backup_of_a_missing_database_fails_clearly(tmp_path: Path) -> None:
    with pytest.raises(backup.BackupError, match="no database"):
        backup.backup(tmp_path / "nope.db", tmp_path / "bk")


# ------------------------------------------------------------------ CLI


def test_cli_backup_and_restore_check(tmp_path: Path, live, capsys) -> None:
    _q, db, _store = live
    assert main(["--db", str(db), "backup", "--to", str(tmp_path / "bk")]) == 0
    (manifest,) = (tmp_path / "bk").glob("*.manifest.json")
    assert main(["--db", str(db), "restore-check", str(manifest),
                 "--into", str(tmp_path / "r")]) == 0
    assert "ok:" in capsys.readouterr().out
    assert main(["--db", str(db), "restore-check", str(manifest),
                 "--into", str(tmp_path / "r")]) == 1


# --------------------------------------------------------------- drills


def test_crash_drill_passes_and_is_logged_as_a_rehearsal(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("LAB_TARGET", raising=False)
    results = drills.drill_crash(tmp_path)
    assert [r.name for r in results] == ["crash-idempotent", "crash-non-idempotent"]
    assert all(r.passed for r in results), [r.actual for r in results]
    path = drills.record(results[0], tmp_path / "log")
    text = path.read_text()
    assert "counts as demonstrated: no" in text and "rehearsal" in text
    assert "## Failure injected" in text and "## Follow-up" in text


def test_a_drill_counts_only_on_the_target(monkeypatch) -> None:
    import platform
    monkeypatch.setenv("LAB_TARGET", "mac-mini")
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    assert not drills.on_target()
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    assert drills.on_target()
    monkeypatch.setenv("LAB_TARGET", "laptop")
    assert not drills.on_target()


def test_restore_drill_against_a_live_database(tmp_path: Path, live) -> None:
    _q, db, store = live
    result = drills.drill_restore(db, store.root, tmp_path)
    assert result.passed, result.actual
    failed = drills.drill_restore(tmp_path / "missing.db", None, tmp_path)
    assert not failed.passed


def test_cli_drill_restore_writes_a_record(tmp_path: Path, live, capsys) -> None:
    _q, db, _store = live
    assert main(["--db", str(db), "drill", "restore", "--log", str(tmp_path / "log")]) == 0
    (record,) = (tmp_path / "log").glob("*-restore.md")
    assert "result: PASS" in record.read_text()
