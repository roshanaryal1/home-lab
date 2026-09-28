"""Content-addressed artifacts and receipts (item 3.3, #66).

A workspace is deleted when its task ends, and a file list without hashes
cannot reproduce an experiment. Before a task may be recorded as
succeeded, every regular file it left behind is copied into a store keyed
by SHA-256 and described by a row in ``artifacts``.

What is refused, and why:

* Symlinks, hard-link tricks, devices, sockets and FIFOs are not
  artifacts. Each is found by ``lstat`` without following it, opened (if
  at all) with ``O_NOFOLLOW`` and re-checked with ``fstat`` on the open
  descriptor, so swapping a file for a link between the check and the
  open reads nothing. A refusal is audited and does not fail the task.
* A file larger than ``MAX_ARTIFACT_BYTES`` fails ingest. The task must
  not be recorded as succeeded with output that was silently dropped.

The store is immutable: blobs are written to a temporary file, flushed to
disk, renamed into place read-only, and never rewritten. Identical bytes
are stored once. ``verify`` re-hashes a blob, which is what a restore
drill (3.4) runs against every row.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import mimetypes
import os
import sqlite3
import stat
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lab.audit import append_event

if TYPE_CHECKING:
    from lab.broker import Workspace

MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_ARTIFACTS_PER_TASK = 2_000
_CHUNK = 1024 * 1024


class ArtifactError(RuntimeError):
    """An artifact could not be stored or does not verify."""


def tool_version() -> str:
    try:
        return metadata.version("home-lab")
    except metadata.PackageNotFoundError:
        return "unknown"


@dataclass(frozen=True)
class Artifact:
    task_id: str
    attempt: int
    path: str
    sha256: str
    size: int
    media_type: str
    lineage: tuple[str, ...] = ()


@dataclass(frozen=True)
class Refusal:
    path: str
    reason: str


@dataclass(frozen=True)
class Problem:
    sha256: str
    path: str
    task_id: str
    problem: str          # "missing" | "size" | "hash"


def _walk(ws: Workspace) -> Iterator[tuple[list[str], os.stat_result]]:
    """Every non-directory entry under the workspace, by descriptor.

    Directories are entered only if they are real directories (opened with
    ``O_NOFOLLOW``); anything else is yielded with its ``lstat`` result
    for the caller to classify. Order is sorted, so a run is repeatable.
    """
    stack: list[list[str]] = [[]]
    while stack:
        parts = stack.pop()
        try:
            with ws.dir_fd(parts) as fd:
                names = sorted(os.listdir(fd))
                infos = [(n, os.stat(n, dir_fd=fd, follow_symlinks=False)) for n in names]
        except OSError:
            continue
        subdirs = []
        for name, info in infos:
            if stat.S_ISDIR(info.st_mode):
                subdirs.append([*parts, name])
            else:
                yield [*parts, name], info
        stack.extend(reversed(subdirs))


class ArtifactStore:
    """Immutable blob store plus the descriptor table."""

    def __init__(self, root: str | Path, conn: sqlite3.Connection) -> None:
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._conn = conn

    # -------------------------------------------------------------- blobs

    def blob_path(self, sha256: str) -> Path:
        if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
            raise ArtifactError("not a sha256 digest")
        return self.root / sha256[:2] / sha256

    def _store_blob(self, fd: int) -> tuple[str, int]:
        """Copy an open regular file into the store; return (sha256, size)."""
        digest = hashlib.sha256()
        size = 0
        tmp_dir = self.root / ".incoming"
        tmp_dir.mkdir(mode=0o700, exist_ok=True)
        tmp_fd, tmp_name = tempfile.mkstemp(dir=tmp_dir)
        try:
            with os.fdopen(tmp_fd, "wb") as out, os.fdopen(fd, "rb", closefd=False) as src:
                while chunk := src.read(_CHUNK):
                    size += len(chunk)
                    if size > MAX_ARTIFACT_BYTES:
                        raise ArtifactError(
                            f"artifact larger than the {MAX_ARTIFACT_BYTES} byte ceiling")
                    digest.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            sha = digest.hexdigest()
            final = self.blob_path(sha)
            final.parent.mkdir(mode=0o700, exist_ok=True)
            if final.exists():
                os.unlink(tmp_name)
            else:
                os.chmod(tmp_name, 0o400)
                os.replace(tmp_name, final)
                self._fsync_dir(final.parent)
            return sha, size
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_name)
            raise

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    # ------------------------------------------------------------- ingest

    def _stage_file(self, ws: Workspace, parts: list[str], task_id: str, attempt: int,
                    lineage: tuple[str, ...]) -> Artifact:
        """Copy one workspace file into the store. Refuses anything that is
        not a plain file. Writes no descriptor row: those go in together."""
        rel = "/".join(parts)
        with ws.dir_fd(parts[:-1]) as parent:
            try:
                fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=parent)
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise ArtifactError(f"{rel}: is a symlink") from None
                raise ArtifactError(f"{rel}: cannot open: {exc.strerror}") from None
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise ArtifactError(f"{rel}: not a regular file")
                sha, size = self._store_blob(fd)
            finally:
                os.close(fd)
        media = mimetypes.guess_type(parts[-1])[0] or "application/octet-stream"
        return Artifact(task_id, attempt, rel, sha, size, media, lineage)

    def ingest_workspace(self, ws: Workspace, task_id: str, attempt: int,
                         lineage: tuple[str, ...] = ()) -> tuple[list[Artifact], list[Refusal]]:
        """Store every regular file; audit and skip links, devices, sockets, FIFOs.

        Raises ``ArtifactError`` on a real failure (oversized file, too
        many files, I/O error), in which case the caller must not record
        the task as succeeded.
        """
        stored: list[Artifact] = []
        refused: list[Refusal] = []
        for parts, info in _walk(ws):
            rel = "/".join(parts)
            if not stat.S_ISREG(info.st_mode):
                kind = ("symlink" if stat.S_ISLNK(info.st_mode)
                        else "socket" if stat.S_ISSOCK(info.st_mode)
                        else "fifo" if stat.S_ISFIFO(info.st_mode)
                        else "device" if stat.S_ISBLK(info.st_mode) or stat.S_ISCHR(info.st_mode)
                        else "special file")
                refused.append(Refusal(rel, kind))
                continue
            if len(stored) >= MAX_ARTIFACTS_PER_TASK:
                raise ArtifactError(f"more than {MAX_ARTIFACTS_PER_TASK} artifacts")
            try:
                stored.append(self._stage_file(ws, parts, task_id, attempt, lineage))
            except ArtifactError as exc:
                if "symlink" in str(exc) or "not a regular file" in str(exc):
                    refused.append(Refusal(rel, "changed to a non-file during ingest"))
                    continue
                raise
        # Blobs are on disk and flushed; now one short transaction records
        # every descriptor and the audit events together, so a reader sees
        # all of an attempt's outputs or none.
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            for a in stored:
                # Re-ingesting the same attempt (a task resumed after an
                # approval) replaces the descriptor with what is there now.
                self._conn.execute(
                    "INSERT INTO artifacts (task_id, attempt, path, sha256, size, "
                    "media_type, tool_version, lineage) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (task_id, attempt, path) DO UPDATE SET "
                    "sha256 = excluded.sha256, size = excluded.size, "
                    "media_type = excluded.media_type, lineage = excluded.lineage",
                    (a.task_id, a.attempt, a.path, a.sha256, a.size, a.media_type,
                     tool_version(), json.dumps(list(a.lineage))),
                )
            if refused:
                append_event(self._conn, task_id, "artifact_refused", detail={
                    "attempt": attempt,
                    "refused": [{"path": r.path, "reason": r.reason} for r in refused[:100]],
                    "count": len(refused),
                })
            append_event(self._conn, task_id, "artifacts_stored", detail={
                "attempt": attempt, "count": len(stored),
                "bytes": sum(a.size for a in stored),
            })
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")
        return stored, refused

    # ------------------------------------------------------ read / verify

    def read(self, sha256: str) -> bytes:
        """The bytes, after re-hashing them. A blob that no longer matches
        its name is corruption, never data."""
        path = self.blob_path(sha256)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            raise ArtifactError(f"artifact {sha256[:12]} is missing from the store") from None
        if hashlib.sha256(data).hexdigest() != sha256:
            raise ArtifactError(f"artifact {sha256[:12]} does not match its hash")
        return data

    def for_task(self, task_id: str, attempt: int | None = None) -> list[sqlite3.Row]:
        if attempt is None:
            return self._conn.execute(
                "SELECT * FROM artifacts WHERE task_id = ? ORDER BY attempt, path",
                (task_id,)).fetchall()
        return self._conn.execute(
            "SELECT * FROM artifacts WHERE task_id = ? AND attempt = ? ORDER BY path",
            (task_id, attempt)).fetchall()

    def verify_all(self) -> list[Problem]:
        """Check every descriptor against the store; empty means sound.

        Covers the guarantee that no recorded artifact points at a blob
        that is missing or altered, so it can run after a restore.
        """
        problems: list[Problem] = []
        checked: dict[str, str | None] = {}
        for row in self._conn.execute(
            "SELECT task_id, path, sha256, size FROM artifacts ORDER BY id"
        ).fetchall():
            sha = row["sha256"]
            if sha not in checked:
                checked[sha] = self._check_blob(sha, row["size"])
            if checked[sha] is not None:
                problems.append(Problem(sha, row["path"], row["task_id"], str(checked[sha])))
        return problems

    def _check_blob(self, sha: str, size: int) -> str | None:
        try:
            path = self.blob_path(sha)
        except ArtifactError:
            return "hash"
        digest = hashlib.sha256()
        total = 0
        try:
            with open(path, "rb") as fh:
                while chunk := fh.read(_CHUNK):
                    total += len(chunk)
                    digest.update(chunk)
        except FileNotFoundError:
            return "missing"
        if total != size:
            return "size"
        return None if digest.hexdigest() == sha else "hash"

    def receipt(self, task_id: str, attempt: int) -> dict[str, Any]:
        """A small, stable summary of one attempt's outputs, for a result."""
        rows = self.for_task(task_id, attempt)
        return {
            "task_id": task_id,
            "attempt": attempt,
            "artifacts": [{"path": r["path"], "sha256": r["sha256"], "size": r["size"]}
                          for r in rows],
        }
