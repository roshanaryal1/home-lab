"""``lab doctor``: one read-only health check of the lab (#347).

The owner runs it. It is not a broker or agent tool. Each check prints one
line, ``ok <name>`` or ``FAIL <name>: <how to fix it>``, and the command exits 1
when any check fails.

Doctor never writes to the database. It opens the file read-only and only if it
exists, so a missing one is reported and never created, and no migration runs.
Opening it through TaskQueue would migrate it, which is why the CLI dispatches
this command before it opens a queue. Doctor reads no secret and prints no URL,
token or chat id.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import sqlite3
import stat
import time
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import Message
from pathlib import Path
from typing import IO

from lab import audit, backup
from lab import operator as operator_keys
from lab.db import connect_readonly
from lab.migrations import latest_version
from lab.supervisor import DEPLOYED_OPERATOR_KEY

MIN_FREE_BYTES = 5 * 10**9              # 5 GB on the database's volume
BACKUP_MAX_AGE = timedelta(hours=36)
MODEL_TIMEOUT_SECONDS = 3.0             # the deadline for the GET /models answer
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
MODEL_SCHEMES = frozenset({"http", "https"})
SELFTEST_KIND = "selftest"              # the event kind lab.selftest records
_MODELS_MAX_BYTES = 1024 * 1024
_MODELS_CHUNK_BYTES = 64 * 1024
_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"        # the stamp backup.backup puts in a manifest name


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    # The fix when the check fails. An ok check says why only when it is off.
    message: str = ""

    def line(self) -> str:
        if not self.ok:
            return f"FAIL {self.name}: {self.message}"
        return f"ok {self.name}: {self.message}" if self.message else f"ok {self.name}"


def _connect(db: Path) -> sqlite3.Connection:
    """A read-only connection to a database that exists. Never creates or migrates it."""
    if not db.is_file():
        raise sqlite3.OperationalError(f"no database at {db}")
    return connect_readonly(db)


def check_database(db: Path) -> Check:
    if not db.is_file():
        return Check("database", False,
                     f"no database at {db}, so pass --db to the right file or start the lab "
                     "once to create it")
    try:
        conn = _connect(db)
        try:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return Check("database", False, f"cannot read {db} as a lab database ({exc}), so check "
                     "--db")
    latest = latest_version()
    if version == latest:
        return Check("database", True)
    if version < latest:
        return Check("database", False, f"schema version {version} is behind {latest}, so run "
                     "lab tasks once to migrate it")
    return Check("database", False, f"schema version {version} is newer than this build's "
                 f"{latest}, so run the build that wrote it")


def _operator_key_path(env: Mapping[str, str]) -> Path | None:
    """The key a decision is verified with, chosen as lab.cli chooses it: the deployed key
    when one is installed, otherwise LAB_OPERATOR_PUBKEY."""
    if DEPLOYED_OPERATOR_KEY.exists():
        return DEPLOYED_OPERATOR_KEY
    configured = env.get("LAB_OPERATOR_PUBKEY", "").strip()
    return Path(configured) if configured else None


def check_operator_key(env: Mapping[str, str]) -> Check:
    path = _operator_key_path(env)
    where = "point LAB_OPERATOR_PUBKEY at the operator.pub that lab operator init wrote"
    if path is None:
        return Check("operator_key", False, f"no operator public key is set, so {where}")
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return Check("operator_key", False, f"no operator public key at {path}, so {where}")
    if not stat.S_ISREG(mode):
        return Check("operator_key", False, f"{path} is not a file, so {where}")
    if mode & (stat.S_IWGRP | stat.S_IWOTH):
        return Check("operator_key", False,
                     f"{path} is writable by its group or others, so run chmod go-w on it")
    # The same loader the supervisor and lab.cli use, so a file that passes here loads
    # for them too.
    try:
        operator_keys.load_public(path)
    except operator_keys.OperatorKeyError:
        return Check("operator_key", False,
                     f"{path} is not a usable operator public key, so {where}")
    return Check("operator_key", True)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect could send the request off loopback, so it is an error and not followed."""

    def redirect_request(self, req: urllib.request.Request, fp: IO[bytes], code: int,
                         msg: str, headers: Message, newurl: str) -> None:
        return None


def _loopback_url(url: str) -> bool:
    """True for an http or https URL with no credentials whose host is loopback."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return (parts.scheme in MODEL_SCHEMES and parts.hostname in LOOPBACK_HOSTS
            and parts.username is None and parts.password is None)


class _ReplyTooLarge(Exception):
    """The GET /models reply is over _MODELS_MAX_BYTES."""


def _read_reply(reply: http.client.HTTPResponse, deadline: float) -> bytes:
    """The whole body, read a chunk at a time. Past the deadline it raises TimeoutError.

    read1 returns as soon as some bytes arrive, so a server that drips them in is caught
    at the next chunk, not after the full size is read. A read that stalls is still bounded
    by the socket timeout.
    """
    body = bytearray()
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError("the reply was still arriving at the deadline")
        chunk = reply.read1(_MODELS_CHUNK_BYTES)
        if not chunk:
            return bytes(body)
        body += chunk
        if len(body) > _MODELS_MAX_BYTES:
            raise _ReplyTooLarge()


def _model_ids(body: bytes) -> set[str] | None:
    """The ids in an OpenAI-style ``{"data": [{"id": ...}, ...]}`` list, or None if the body
    is not one."""
    try:
        data = json.loads(body)["data"]
    except (ValueError, KeyError, TypeError, RecursionError):
        return None
    if not isinstance(data, list):
        return None
    ids: set[str] = set()
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            return None
        ids.add(item["id"])
    return ids


def check_model(env: Mapping[str, str]) -> Check:
    url = env.get("LAB_MODEL_URL", "").strip()
    if not url:
        return Check("model", True, "not configured")
    if not _loopback_url(url):
        return Check("model", False, "LAB_MODEL_URL must be an http or https URL with no "
                     "credentials that points at a loopback server (127.0.0.1, ::1 or localhost)")
    # No proxy: a proxy from the environment would carry the request off loopback.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    # Counted from before the open and checked before each body read.
    deadline = time.monotonic() + MODEL_TIMEOUT_SECONDS
    try:
        with opener.open(url.rstrip("/") + "/models", timeout=MODEL_TIMEOUT_SECONDS) as reply:
            body = _read_reply(reply, deadline)
    except _ReplyTooLarge:
        return Check("model", False, "GET /models on LAB_MODEL_URL sent more than "
                     f"{_MODELS_MAX_BYTES:,} bytes, so check that LAB_MODEL_URL points at the "
                     "model server")
    except (OSError, ValueError, http.client.HTTPException):
        return Check("model", False, "GET /models on LAB_MODEL_URL gave no successful answer "
                     f"within {MODEL_TIMEOUT_SECONDS:g} seconds, so start the model server or "
                     "fix LAB_MODEL_URL")
    ids = _model_ids(body)
    if ids is None:
        return Check("model", False, "the server at LAB_MODEL_URL did not answer GET /models "
                     "with a model list, so check that LAB_MODEL_URL points at the model server")
    name = env.get("LAB_MODEL_NAME", "").strip()
    if name and name not in ids:
        return Check("model", False, f"the model server does not list {name}, so fix "
                     "LAB_MODEL_NAME or serve that model")
    return Check("model", True)


def _existing_ancestor(path: Path) -> Path:
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return path


def check_disk(db: Path) -> Check:
    try:
        free = shutil.disk_usage(_existing_ancestor(db.absolute())).free
    except OSError as exc:
        return Check("disk", False, f"cannot read free space on the database volume ({exc}), so "
                     "check that the volume is mounted")
    if free >= MIN_FREE_BYTES:
        return Check("disk", True)
    return Check("disk", False, f"only {free / 10**9:.1f} GB is free on the database volume, "
                 f"under the {MIN_FREE_BYTES / 10**9:g} GB minimum, so free some space")


def check_backup(env: Mapping[str, str]) -> Check:
    folder = env.get("LAB_BACKUP_DIR", "").strip()
    if not folder:
        return Check("backup", True, "not configured")
    if "PASTE_" in folder or not os.path.isabs(folder):
        return Check("backup", False, "LAB_BACKUP_DIR is still the placeholder or not absolute, "
                     "so set it in the installed service definition")
    try:
        newest = backup.newest_manifest(Path(folder))
    except backup.BackupError as exc:
        return Check("backup", False, f"{exc}, so run lab backup or fix LAB_BACKUP_DIR")
    stamp = newest.name.split(".", 1)[0].removeprefix("lab-")
    try:
        taken = datetime.strptime(stamp, _STAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return Check("backup", False, f"cannot read the time in {newest.name}, so run lab backup")
    age = datetime.now(UTC) - taken
    if age < timedelta(0):
        return Check("backup", False, f"the newest backup {newest.name} is dated in the future, "
                     "so check the clock")
    if age < BACKUP_MAX_AGE:
        return Check("backup", True)
    return Check("backup", False, f"the newest backup is {age.total_seconds() / 3600:.0f} hours "
                 f"old, past the {BACKUP_MAX_AGE.total_seconds() / 3600:.0f} hour limit, so run "
                 "lab backup")


def check_selftest(db: Path) -> Check:
    try:
        conn = _connect(db)
        try:
            row = conn.execute(
                "SELECT detail FROM events WHERE kind = ? ORDER BY id DESC LIMIT 1",
                (SELFTEST_KIND,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return Check("selftest", False, f"cannot read the event log ({exc}), so fix the database "
                     "check first")
    if row is None:
        return Check("selftest", False, "no selftest has been recorded yet, so run lab selftest")
    try:
        passed = json.loads(row[0] or "{}").get("ok") is True
    except (ValueError, AttributeError):
        passed = False
    if passed:
        return Check("selftest", True)
    return Check("selftest", False, "the last selftest failed, so run lab selftest and fix what "
                 "it reports")


def check_audit_chain(db: Path) -> Check:
    try:
        conn = _connect(db)
        try:
            report = audit.verify_chain(conn)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return Check("audit_chain", False, f"cannot read the audit log ({exc}), so fix the "
                     "database check first")
    if report.ok:
        return Check("audit_chain", True)
    return Check("audit_chain", False, f"the audit chain breaks at event {report.bad_id} "
                 f"({report.problem}), so keep this database for review and restore a backup "
                 "that verifies")


def run(db: Path, env: Mapping[str, str]) -> list[Check]:
    """Every check, in the order they print. Reads ``env`` and the database, writes nothing."""
    return [
        check_database(db),
        check_operator_key(env),
        check_model(env),
        check_disk(db),
        check_backup(env),
        check_selftest(db),
        check_audit_chain(db),
    ]
