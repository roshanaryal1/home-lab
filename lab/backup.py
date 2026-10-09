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
every blob the database refers to: task artifacts, evidence snapshots and
skill version files. Any of those failing is a failed restore.

Blobs are content-addressed and immutable, so a backup copies only
blobs the destination does not already hold: repeated backups to one
directory are incremental.

``rotate`` keeps a scheduled backup folder from filling the disk. It
deletes only what it can prove is its own: a manifest with the exact
name ``backup`` writes, that parses and names its own database file, that
database, and blobs that only the deleted backups referenced.
Anything else in the folder is left alone, and no symlink is followed,
not even one that has a backup's name.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lab import audit
from lab.artifacts import ArtifactError, ArtifactStore
from lab.migrations import latest_version, online_copy

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


def _check_database_path(db_path: Path) -> None:
    """Refuse a database path that is a symbolic link or not a regular file.

    Only the last part of the path is looked at (``lstat``), so a folder on
    the way that is a link still works: the deployed ``/var/homelab/lab.db``
    goes through ``/var``, which macOS links to ``/private/var``. SQLite opens
    the path itself, so this is checked just before it does, and before
    anything else is read or written.
    """
    try:
        mode = os.lstat(db_path).st_mode
    except (FileNotFoundError, NotADirectoryError):
        raise BackupError(f"no database at {db_path}") from None
    if stat.S_ISLNK(mode):
        raise BackupError(f"{db_path} is a symbolic link; give the database file itself")
    if not stat.S_ISREG(mode):
        raise BackupError(f"{db_path} is not a regular file")


def _referenced_blobs(conn: sqlite3.Connection, version: int) -> list[str]:
    """Every blob digest the database refers to, sorted and without repeats.

    Task artifacts (schema 4 and later), evidence snapshots (7 and later)
    and skill version files (11 and later), whose ``manifest`` column maps
    each path to ``[sha256, mode]``. A table is read only at a version
    that has it, so an older database is handled the same way.
    """
    selects = []
    if version >= 4:
        selects.append("SELECT sha256 AS digest FROM artifacts")
    if version >= 7:
        selects.append("SELECT sha256 AS digest FROM evidence_snapshots")
    if version >= 11:
        selects.append("SELECT json_extract(m.value, '$[0]') AS digest "
                       "FROM skill_versions, json_each(skill_versions.manifest) AS m")
    if not selects:
        return []
    rows = conn.execute(f"SELECT digest FROM ({' UNION '.join(selects)}) "
                        "WHERE digest IS NOT NULL ORDER BY digest").fetchall()
    return [str(row[0]) for row in rows]


def backup(db_path: Path, dest: Path, artifacts_dir: Path | None = None, *,
           now: datetime | None = None) -> Path:
    """Snapshot ``db_path`` and its artifacts into ``dest``; return the manifest path."""
    db_path = Path(db_path)
    _check_database_path(db_path)
    dest = Path(dest)
    dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    name = f"lab-{stamp}"
    final_db = dest / f"{name}.db"
    if final_db.exists():
        raise BackupError(f"{final_db} already exists; one backup per second per directory")
    tmp_db = dest / f".{name}.db.partial"

    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        online_copy(source, tmp_db)
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
        blob_digests = _referenced_blobs(check, version)
    except BaseException:
        tmp_db.unlink(missing_ok=True)
        raise
    finally:
        check.close()

    copied = 0
    artifacts_copied = 0
    artifact_digests = {str(sha) for (sha,) in artifact_rows}
    if artifacts_dir is not None and blob_digests:
        store_src = ArtifactStore(artifacts_dir, sqlite3.connect(":memory:"))
        store_dst = ArtifactStore(dest / "artifacts", sqlite3.connect(":memory:"))
        for sha in blob_digests:
            src = store_src.blob_path(sha)
            dst = store_dst.blob_path(sha)
            if dst.exists():
                continue
            if not src.exists():
                tmp_db.unlink(missing_ok=True)
                kind = "artifact" if sha in artifact_digests else "blob"
                raise BackupError(f"{kind} {sha[:12]} is missing from {artifacts_dir}")
            dst.parent.mkdir(mode=0o700, exist_ok=True)
            partial = dst.with_name(dst.name + ".partial")
            shutil.copyfile(src, partial)
            os.chmod(partial, 0o400)
            with open(partial, "rb") as fh:
                os.fsync(fh.fileno())
            os.replace(partial, dst)
            copied += 1
            if sha in artifact_digests:
                artifacts_copied += 1

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
        "artifact_blobs_copied": artifacts_copied,
        "blobs": len(blob_digests),
        "blobs_copied": copied,
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


# Every field restore_check reads; a manifest without one is unreadable, not a crash.
_MANIFEST_FIELDS = ("database", "database_sha256", "database_bytes", "schema_version",
                    "audit_head", "audit_events")


def _check_restored_blob(store: ArtifactStore, sha: str, report: RestoreReport) -> None:
    """Re-hash one blob the restored database refers to. Missing or altered is a failure."""
    try:
        path = store.blob_path(sha)
    except ArtifactError:
        report.fail(f"blob {sha[:12]} is not a sha256 digest")
        return
    try:
        actual = _sha256_file(path)
    except FileNotFoundError:
        report.fail(f"blob {sha[:12]} is missing from the restored store")
        return
    except OSError as exc:
        # A directory or an unreadable file where the blob should be: a failed
        # restore to report, not an exception to crash the drill with.
        report.fail(f"blob {sha[:12]} cannot be read in the restored store "
                    f"({type(exc).__name__})")
        return
    if actual != sha:
        report.fail(f"blob {sha[:12]} does not match its name")


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
        missing = [k for k in _MANIFEST_FIELDS if k not in manifest]
        if missing:
            raise KeyError(", ".join(missing))
        source_db = manifest_path.parent / manifest["database"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
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
            flagged: set[str] = set()
            for problem in store.verify_all():
                report.fail(f"artifact {problem.sha256[:12]} ({problem.path}): {problem.problem}")
                flagged.add(problem.sha256)
            # Evidence snapshots and skill files too. A digest already reported above is not
            # reported twice.
            for sha in _referenced_blobs(conn, version):
                if sha not in flagged:
                    _check_restored_blob(store, sha, report)
    except sqlite3.DatabaseError as exc:
        # Damaged enough that SQLite cannot run its own checks: a failed
        # restore to report, not an exception to crash the drill with.
        report.fail(f"database cannot be read: {exc}")
    finally:
        conn.close()
    return report


_MANIFEST_NAME = re.compile(r"^lab-(\d{8}T\d{6}Z)\.manifest\.json$")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def newest_manifest(folder: Path) -> Path:
    """The newest manifest ``backup`` wrote in ``folder``, by the stamp in its name.

    Only names ``backup`` writes count, and only regular files: a symlink
    with a backup's name is not followed. Nothing in ``folder`` is changed.
    """
    folder = Path(folder)
    try:
        names = [entry.name for entry in os.scandir(folder)
                 if _MANIFEST_NAME.match(entry.name) and entry.is_file(follow_symlinks=False)]
    except OSError as exc:
        raise BackupError(f"cannot list {folder}: {exc.strerror}") from exc
    if not names:
        raise BackupError(f"no backup manifest (lab-*.manifest.json) in {folder}")
    return folder / max(names)


@dataclass
class RotateReport:
    kept: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    blobs_removed: int = 0
    left_alone: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Own:
    stamp: str
    manifest: str
    database: str | None        # None when the database file is already gone


def _read_manifest(dir_fd: int, name: str) -> Any:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
    with os.fdopen(fd, encoding="utf-8") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("not a regular file")
        return json.loads(stream.read(1024 * 1024))


def _own_backups(dir_fd: int, report: RotateReport) -> list[_Own]:
    own: list[_Own] = []
    for entry in os.scandir(dir_fd):
        match = _MANIFEST_NAME.match(entry.name)
        if not match:
            continue
        if not entry.is_file(follow_symlinks=False):
            report.left_alone.append(f"{entry.name}: not a regular file")
            continue
        stamp = match.group(1)
        db_name = f"lab-{stamp}.db"
        try:
            data = _read_manifest(dir_fd, entry.name)
        except (OSError, ValueError) as exc:
            report.left_alone.append(f"{entry.name}: unreadable ({type(exc).__name__})")
            continue
        if (not isinstance(data, dict) or data.get("manifest_version") != MANIFEST_VERSION
                or data.get("database") != db_name):
            report.left_alone.append(f"{entry.name}: not a manifest this tool wrote")
            continue
        try:
            info = os.lstat(db_name, dir_fd=dir_fd)
        except FileNotFoundError:
            own.append(_Own(stamp, entry.name, None))
            continue
        if not stat.S_ISREG(info.st_mode):
            report.left_alone.append(f"{entry.name}: its database is not a regular file")
            continue
        own.append(_Own(stamp, entry.name, db_name))
    return sorted(own, key=lambda b: b.stamp, reverse=True)


def _blobs_of(dest: Path, item: _Own) -> set[str]:
    """The blob digests a backup's database refers to. Raises on any doubt.

    This must name the same blobs ``backup`` copies. Otherwise rotation could
    delete a blob that a kept backup still needs.
    """
    if item.database is None:
        return set()
    conn = sqlite3.connect(f"file:{dest / item.database}?mode=ro", uri=True)
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        return set(_referenced_blobs(conn, version))
    finally:
        conn.close()


def _remove_blob(dir_fd: int, sha: str) -> bool:
    if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        return False
    try:
        store = os.open("artifacts", _DIR_FLAGS, dir_fd=dir_fd)
    except OSError:
        return False                 # missing, or a symlink: nothing of ours to remove there
    try:
        try:
            shard = os.open(sha[:2], _DIR_FLAGS, dir_fd=store)
        except OSError:
            return False
        try:
            try:
                info = os.lstat(sha, dir_fd=shard)
            except FileNotFoundError:
                return False
            if not stat.S_ISREG(info.st_mode):
                return False
            os.unlink(sha, dir_fd=shard)
            return True
        finally:
            os.close(shard)
    finally:
        os.close(store)


def rotate(dest: Path, keep: int, *, protect: Path | None = None) -> RotateReport:
    """Delete all but the newest ``keep`` backups in ``dest``.

    ``protect`` (the manifest just written) is always kept. ``dest`` itself
    must be a real directory, not a symlink. A blob is removed only when a
    deleted backup referred to it and no kept backup does; if any kept
    backup cannot be read, no blob is removed at all.
    """
    if keep < 1:
        raise BackupError("keep must be at least 1")
    dest = Path(dest)
    try:
        dir_fd = os.open(dest, _DIR_FLAGS)
    except OSError as exc:
        raise BackupError(f"cannot open {dest} as a real directory (a symlink is refused): "
                          f"{exc.strerror}") from exc
    report = RotateReport()
    try:
        own = _own_backups(dir_fd, report)
        # A manifest whose database is gone is not a backup, so it never takes
        # a keep slot from a complete one. It still goes once it falls outside.
        own = [b for b in own if b.database is not None] + \
            [b for b in own if b.database is None]
        protected = protect.name if protect is not None else None
        kept = [b for i, b in enumerate(own) if i < keep or b.manifest == protected]
        doomed = [b for b in own if b not in kept]
        report.kept = [b.manifest for b in kept]
        if not doomed:
            return report

        unused: set[str] = set()
        try:
            in_use = set().union(*(_blobs_of(dest, b) for b in kept))
            for item in doomed:
                with contextlib.suppress(sqlite3.DatabaseError, OSError):
                    unused |= _blobs_of(dest, item)
            unused -= in_use
        except (sqlite3.DatabaseError, OSError) as exc:
            report.left_alone.append(f"artifact blobs: a kept backup is unreadable ({exc})")
            unused = set()

        for item in doomed:
            os.unlink(item.manifest, dir_fd=dir_fd)      # first, so a half-removed
            if item.database is not None:                # backup is never mistaken for one
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(item.database, dir_fd=dir_fd)
            report.removed.append(item.manifest)
        report.blobs_removed = sum(_remove_blob(dir_fd, sha) for sha in sorted(unused))
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return report
