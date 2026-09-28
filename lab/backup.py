"""Backup with SQLite's backup API, and a restore that proves itself (item 3.4, #67).

Copying a live database file while it is being written can capture a
torn state, and in WAL mode the file alone is not the database. The
online backup API produces a consistent snapshot without stopping the
supervisor.

A backup counts only once a restore has worked, so ``restore_check``
does not just copy: into a fresh directory it recovers the snapshot,
then checks the snapshot against the manifest written at backup time
(file hash, schema version, audit chain head, artifact count), runs
SQLite's integrity check, re-walks the audit hash chain, and re-hashes
every artifact blob. Any of those failing is a failed restore.

Artifacts are content-addressed and immutable, so a backup copies only
blobs the destination does not already hold: repeated backups to one
directory are incremental.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lab import audit
from lab.artifacts import ArtifactStore
from lab.migrations import latest_version

MANIFEST_VERSION = 1
_CHUNK = 1024 * 1024


class BackupError(RuntimeError):
    """A backup could not be taken, or a restore failed a check."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def backup(db_path: Path, dest: Path, artifacts_dir: Path | None = None) -> Path:
    """Snapshot ``db_path`` and its artifacts into ``dest``; return the manifest path."""
    db_path = Path(db_path)
    if not db_path.exists():
        raise BackupError(f"no database at {db_path}")
    dest = Path(dest)
    dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    name = f"lab-{stamp}"
    final_db = dest / f"{name}.db"
    if final_db.exists():
        raise BackupError(f"{final_db} already exists; one backup per second per directory")
    tmp_db = dest / f".{name}.db.partial"

    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(tmp_db)
        try:
            source.backup(target)
            target.execute("PRAGMA journal_mode = DELETE")
        finally:
            target.close()
    except BaseException:
        tmp_db.unlink(missing_ok=True)
        raise
    finally:
        source.close()

    check = sqlite3.connect(tmp_db)
    check.row_factory = sqlite3.Row
    try:
        integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise BackupError(f"snapshot failed its integrity check: {integrity}")
        version = int(check.execute("PRAGMA user_version").fetchone()[0])
        chain = audit.verify_chain(check)
        artifact_rows = check.execute(
            "SELECT DISTINCT sha256 FROM artifacts").fetchall() if version >= 4 else []
    except BaseException:
        tmp_db.unlink(missing_ok=True)
        raise
    finally:
        check.close()

    copied = 0
    if artifacts_dir is not None and artifact_rows:
        store_src = ArtifactStore(artifacts_dir, sqlite3.connect(":memory:"))
        store_dst = ArtifactStore(dest / "artifacts", sqlite3.connect(":memory:"))
        for (sha,) in artifact_rows:
            src = store_src.blob_path(sha)
            dst = store_dst.blob_path(sha)
            if dst.exists():
                continue
            if not src.exists():
                tmp_db.unlink(missing_ok=True)
                raise BackupError(f"artifact {sha[:12]} is missing from {artifacts_dir}")
            dst.parent.mkdir(mode=0o700, exist_ok=True)
            partial = dst.with_name(dst.name + ".partial")
            shutil.copyfile(src, partial)
            os.chmod(partial, 0o400)
            with open(partial, "rb") as fh:
                os.fsync(fh.fileno())
            os.replace(partial, dst)
            copied += 1

    with open(tmp_db, "rb") as fh:
        os.fsync(fh.fileno())
    os.replace(tmp_db, final_db)
    manifest: dict[str, Any] = {
        "manifest_version": MANIFEST_VERSION,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "database": final_db.name,
        "database_sha256": _sha256_file(final_db),
        "database_bytes": final_db.stat().st_size,
        "schema_version": version,
        "audit_events": chain.events,
        "audit_head": chain.last_hash,
        "audit_chain_ok": chain.ok,
        "artifact_blobs": len(artifact_rows),
        "artifact_blobs_copied": copied,
    }
    manifest_path = dest / f"{name}.manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8")
    with open(manifest_path, "rb") as fh:
        os.fsync(fh.fileno())
    _fsync_dir(dest)
    return manifest_path


@dataclass
class RestoreReport:
    ok: bool = True
    problems: list[str] = field(default_factory=list)
    database: Path | None = None
    events: int = 0
    artifacts_checked: int = 0

    def fail(self, message: str) -> None:
        self.ok = False
        self.problems.append(message)


def restore_check(manifest_path: Path, into: Path) -> RestoreReport:
    """Restore a backup into a fresh directory and verify everything.

    ``into`` must not exist or must be empty: a restore drill that
    overwrites something proves nothing and can destroy it.
    """
    manifest_path = Path(manifest_path)
    into = Path(into)
    if into.exists() and any(into.iterdir()):
        raise BackupError(f"{into} is not empty; restore into a fresh location")
    report = RestoreReport()
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        source_db = manifest_path.parent / manifest["database"]
    except (OSError, ValueError, KeyError) as exc:
        raise BackupError(f"unreadable manifest {manifest_path}: {exc}") from exc
    if not source_db.exists():
        report.fail(f"backup database {source_db.name} is missing")
        return report

    into.mkdir(mode=0o700, parents=True, exist_ok=True)
    restored = into / "lab.db"
    shutil.copyfile(source_db, restored)
    report.database = restored

    if _sha256_file(restored) != manifest["database_sha256"]:
        report.fail("database file does not match the hash in the manifest")
    if restored.stat().st_size != manifest["database_bytes"]:
        report.fail("database size does not match the manifest")

    conn = sqlite3.connect(restored)
    conn.row_factory = sqlite3.Row
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            report.fail(f"integrity check: {integrity}")
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version != manifest["schema_version"]:
            report.fail(f"schema version {version}, manifest says {manifest['schema_version']}")
        if version > latest_version():
            report.fail(f"schema version {version} is newer than this build ({latest_version()})")
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk:
            report.fail(f"{len(fk)} foreign key violation(s)")
        chain = audit.verify_chain(conn)
        report.events = chain.events
        if not chain.ok:
            report.fail(f"audit chain broken at event {chain.bad_id}: {chain.problem}")
        if chain.last_hash != manifest["audit_head"] or chain.events != manifest["audit_events"]:
            report.fail("audit chain head does not match the manifest")
        if version >= 4:
            store_src = manifest_path.parent / "artifacts"
            store_dst = into / "artifacts"
            if store_src.exists():
                shutil.copytree(store_src, store_dst)
            else:
                store_dst.mkdir(mode=0o700)
            store = ArtifactStore(store_dst, conn)
            report.artifacts_checked = int(conn.execute(
                "SELECT COUNT(DISTINCT sha256) FROM artifacts").fetchone()[0])
            for problem in store.verify_all():
                report.fail(f"artifact {problem.sha256[:12]} ({problem.path}): {problem.problem}")
    except sqlite3.DatabaseError as exc:
        # Damaged enough that SQLite cannot run its own checks: a failed
        # restore to report, not an exception to crash the drill with.
        report.fail(f"database cannot be read: {exc}")
    finally:
        conn.close()
    return report
