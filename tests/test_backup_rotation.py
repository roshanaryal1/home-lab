"""Scheduled backups: rotation, the restore check on every new backup, and the
daily launchd job (#67).

Rotation deletes files, so it must only ever delete its own: a manifest it
wrote, the database that manifest names, and blobs only deleted backups used.
"""

from __future__ import annotations

import hashlib
import json
import plistlib
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lab import backup, service
from lab.artifacts import ArtifactStore
from lab.broker import Workspace
from lab.cli import main
from lab.queue import TaskQueue

T0 = datetime(2026, 9, 1, 2, 47, tzinfo=UTC)


@pytest.fixture()
def live(tmp_path: Path):
    db = tmp_path / "live" / "lab.db"
    db.parent.mkdir()
    q = TaskQueue(db)
    q.add_task("one")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_bytes(b"alpha")
    store = ArtifactStore(db.parent / "artifacts", q._conn)
    store.ingest_workspace(Workspace(ws), "t1", 1)
    yield db, store
    q.close()


def _series(db: Path, store: ArtifactStore, dest: Path, count: int) -> list[Path]:
    return [backup.backup(db, dest, store.root, now=T0 + timedelta(days=i))
            for i in range(count)]


def _names(dest: Path) -> set[str]:
    return {p.name for p in dest.iterdir()}


def _orphan_blob(dest: Path, manifest: Path) -> Path:
    """Make an old backup refer to a blob no newer backup has."""
    content = b"only the oldest backup had this"
    sha = hashlib.sha256(content).hexdigest()
    snapshot = dest / json.loads(manifest.read_text())["database"]
    conn = sqlite3.connect(snapshot)
    conn.execute("INSERT INTO artifacts (task_id, attempt, path, sha256, size, media_type, "
                 "tool_version) VALUES ('old', 1, 'gone.txt', ?, ?, 'text/plain', 't')",
                 (sha, len(content)))
    conn.commit()
    conn.close()
    blob = dest / "artifacts" / sha[:2] / sha
    blob.parent.mkdir(exist_ok=True)
    blob.write_bytes(content)
    blob.chmod(0o400)
    return blob


# ---------------------------------------------------------------- rotation


def test_rotation_keeps_the_newest_n(tmp_path: Path, live) -> None:
    db, store = live
    dest = tmp_path / "bk"
    manifests = _series(db, store, dest, 5)
    report = backup.rotate(dest, 2, protect=manifests[-1])
    assert sorted(report.kept) == sorted(m.name for m in manifests[-2:])
    assert sorted(report.removed) == sorted(m.name for m in manifests[:3])
    left = _names(dest)
    for m in manifests[:3]:
        assert m.name not in left and m.name.replace(".manifest.json", ".db") not in left
    for m in manifests[-2:]:
        assert m.exists() and backup.restore_check(m, tmp_path / m.stem).ok


def test_blobs_a_kept_backup_uses_stay_and_orphans_go(tmp_path: Path, live) -> None:
    db, store = live
    dest = tmp_path / "bk"
    manifests = _series(db, store, dest, 3)
    orphan = _orphan_blob(dest, manifests[0])
    shared = [p for p in (dest / "artifacts").rglob("*") if p.is_file() and p != orphan]
    report = backup.rotate(dest, 2, protect=manifests[-1])
    assert report.blobs_removed == 1 and not orphan.exists()
    assert shared and all(p.exists() for p in shared)
    assert backup.restore_check(manifests[-1], tmp_path / "r").ok


def test_the_new_backup_is_kept_even_if_older_ones_look_newer(tmp_path: Path, live) -> None:
    db, store = live
    dest = tmp_path / "bk"
    future = backup.backup(db, dest, store.root, now=T0 + timedelta(days=400))
    new = backup.backup(db, dest, store.root, now=T0)
    report = backup.rotate(dest, 1, protect=new)
    assert set(report.kept) == {future.name, new.name}



def test_a_manifest_without_its_database_never_takes_a_keep_slot(tmp_path: Path, live) -> None:
    db, store = live
    dest = tmp_path / "bk"
    manifests = _series(db, store, dest, 3)
    broken = backup.backup(db, dest, store.root, now=T0 + timedelta(days=30))
    (dest / broken.name.replace(".manifest.json", ".db")).unlink()
    report = backup.rotate(dest, 2, protect=manifests[-1])
    assert sorted(report.kept) == sorted(m.name for m in manifests[-2:])
    assert broken.name in report.removed and not broken.exists()
    assert manifests[1].exists() and backup.restore_check(manifests[1], tmp_path / "r").ok

@pytest.mark.safety
def test_rotation_never_touches_foreign_files(tmp_path: Path, live) -> None:
    db, store = live
    dest = tmp_path / "bk"
    manifests = _series(db, store, dest, 3)
    foreign = {
        "notes.txt": "mine",
        "lab-old.db": "not a stamped name",
        "lab-20200101T000000Z.db": "a database with no manifest",
        "lab-20200102T000000Z.manifest.json": "not json",
        "lab-20200103T000000Z.manifest.json": json.dumps(
            {"manifest_version": 1, "database": "something-else.db"}),
        "something-else.db": "named by a foreign manifest",
        "lab-20200104T000000Z.manifest.json.bak": "a copy",
    }
    for name, text in foreign.items():
        (dest / name).write_text(text)
    stray = dest / "artifacts" / "zz"
    stray.mkdir()
    (stray / "readme").write_text("not a blob")
    backup.rotate(dest, 1, protect=manifests[-1])
    for name, text in foreign.items():
        assert (dest / name).read_text() == text, name
    assert (stray / "readme").read_text() == "not a blob"
    assert not manifests[0].exists() and manifests[-1].exists()


@pytest.mark.safety
def test_rotation_never_follows_a_symlink(tmp_path: Path, live) -> None:
    db, store = live
    elsewhere = tmp_path / "elsewhere"
    outside = _series(db, store, elsewhere, 1)[0]
    outside_db = elsewhere / json.loads(outside.read_text())["database"]
    dest = tmp_path / "bk"
    manifests = _series(db, store, dest, 3)
    # An old-looking manifest name that is a link to a real backup elsewhere.
    link = dest / "lab-20200101T000000Z.manifest.json"
    link.symlink_to(outside)
    # One of ours whose database has been swapped for a link to a file outside.
    victim = elsewhere / "precious.db"
    victim.write_text("do not delete")
    swapped = dest / json.loads(manifests[0].read_text())["database"]
    swapped.unlink()
    swapped.symlink_to(victim)
    report = backup.rotate(dest, 1, protect=manifests[-1])
    assert link.is_symlink() and outside.exists() and outside_db.exists()
    assert victim.read_text() == "do not delete"
    assert manifests[0].exists() and swapped.is_symlink(), "left alone, not half-deleted"
    assert any("not a regular file" in n for n in report.left_alone)


@pytest.mark.safety
def test_a_symlinked_artifact_store_is_not_entered(tmp_path: Path, live) -> None:
    db, store = live
    dest = tmp_path / "bk"
    manifests = _series(db, store, dest, 2)
    orphan = _orphan_blob(dest, manifests[0])
    real = tmp_path / "real-artifacts"
    (dest / "artifacts").rename(real)
    (dest / "artifacts").symlink_to(real)
    report = backup.rotate(dest, 1, protect=manifests[-1])
    assert report.blobs_removed == 0
    assert (real / orphan.parent.name / orphan.name).exists()


@pytest.mark.safety
def test_rotation_refuses_a_symlinked_folder_and_a_keep_below_one(tmp_path: Path, live) -> None:
    db, store = live
    real = tmp_path / "real"
    manifests = _series(db, store, real, 2)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(backup.BackupError, match="symlink"):
        backup.rotate(link, 1)
    with pytest.raises(backup.BackupError, match="at least 1"):
        backup.rotate(real, 0)
    assert all(m.exists() for m in manifests)


def test_an_unreadable_kept_backup_means_no_blob_is_removed(tmp_path: Path, live) -> None:
    db, store = live
    dest = tmp_path / "bk"
    manifests = _series(db, store, dest, 2)
    orphan = _orphan_blob(dest, manifests[0])
    kept_db = dest / json.loads(manifests[-1].read_text())["database"]
    kept_db.write_bytes(b"not a database" * 100)
    report = backup.rotate(dest, 1, protect=manifests[-1])
    assert report.blobs_removed == 0 and orphan.exists()
    assert any("unreadable" in n for n in report.left_alone)


# --------------------------------------------------------------------- CLI


def _recorder(tmp_path: Path) -> tuple[Path, Path]:
    out = tmp_path / "alerts.txt"
    script = tmp_path / "notify.py"
    script.write_text(f"import sys\nopen({str(out)!r}, 'a').write(sys.stdin.read())\n")
    cfg = tmp_path / "alert.json"
    cfg.write_text(json.dumps({"command": [sys.executable, str(script)]}))
    cfg.chmod(0o600)
    return cfg, out


def test_cli_backup_keep_checks_then_rotates(tmp_path: Path, live, capsys) -> None:
    db, store = live
    dest = tmp_path / "bk"
    _series(db, store, dest, 3)
    cfg, out = _recorder(tmp_path)
    assert main(["--db", str(db), "backup", "--to", str(dest), "--keep", "2",
                 "--alert-config", str(cfg)]) == 0
    printed = capsys.readouterr().out
    assert "restore check ok" in printed and "removed 2 backups" in printed
    assert len(list(dest.glob("*.manifest.json"))) == 2
    assert not out.exists(), "no alert when the backup is good"


@pytest.mark.safety
def test_a_new_backup_that_fails_its_restore_check_fails_and_rotates_nothing(
        tmp_path: Path, live, monkeypatch, capsys) -> None:
    db, store = live
    dest = tmp_path / "bk"
    old = _series(db, store, dest, 3)
    real_backup = backup.backup

    def damaged(*a: object, **k: object) -> Path:
        manifest = real_backup(*a, **k)  # type: ignore[arg-type]
        snapshot = manifest.parent / json.loads(manifest.read_text())["database"]
        raw = bytearray(snapshot.read_bytes())
        raw[len(raw) // 2] ^= 0xFF
        snapshot.write_bytes(bytes(raw))
        return manifest

    monkeypatch.setattr(backup, "backup", damaged)
    cfg, out = _recorder(tmp_path)
    assert main(["--db", str(db), "backup", "--to", str(dest), "--keep", "1",
                 "--alert-config", str(cfg)]) == 1
    assert "failed its restore check" in capsys.readouterr().err
    assert all(m.exists() for m in old), "the good old backups must survive a bad new one"
    assert out.exists() and out.read_text().startswith("backup: ")


def test_the_folder_comes_from_lab_backup_dir_and_the_placeholder_is_refused(
        tmp_path: Path, live, monkeypatch, capsys) -> None:
    db, _store = live
    cfg, out = _recorder(tmp_path)
    monkeypatch.setenv("LAB_BACKUP_DIR", service.BACKUP_DIR_PLACEHOLDER)
    assert main(["--db", str(db), "backup", "--keep", "3", "--alert-config", str(cfg)]) == 1
    assert "placeholder" in capsys.readouterr().err and out.exists()
    monkeypatch.delenv("LAB_BACKUP_DIR")
    assert main(["--db", str(db), "backup"]) == 1
    assert "LAB_BACKUP_DIR" in capsys.readouterr().err
    monkeypatch.setenv("LAB_BACKUP_DIR", str(tmp_path / "from-env"))
    assert main(["--db", str(db), "backup", "--keep", "3"]) == 0
    assert len(list((tmp_path / "from-env").glob("*.manifest.json"))) == 1


def test_cli_refuses_a_symlinked_destination_and_a_bad_keep(tmp_path: Path, live,
                                                            capsys) -> None:
    db, _store = live
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert main(["--db", str(db), "backup", "--to", str(link), "--keep", "2"]) == 1
    assert main(["--db", str(db), "backup", "--to", str(real), "--keep", "0"]) == 1
    assert not list(real.iterdir())


# ------------------------------------------------------------------ launchd


def test_backup_plist_runs_daily_as_lab_and_matches_the_committed_copy() -> None:
    root = Path(__file__).resolve().parent.parent / "ops" / "launchd"
    generated = service.backup_plist(user="lab", workdir="/opt/homelab")
    assert (root / "com.homelab.backup.plist").read_bytes() == generated
    data = plistlib.loads(generated)
    assert data["Label"] == service.BACKUP_LABEL and data["UserName"] == "lab"
    assert set(data["StartCalendarInterval"]) == {"Hour", "Minute"}
    # Only the launcher holds Full Disk Access, so the job runs it, with no
    # arguments; its fixed command is checked in test_backup_launcher.py (#287).
    assert data["ProgramArguments"] == [service.BACKUP_LAUNCHER] and "Program" not in data
    assert data["EnvironmentVariables"] == {"LAB_BACKUP_DIR": service.BACKUP_DIR_PLACEHOLDER}
