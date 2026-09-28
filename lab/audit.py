"""Tamper-evident audit log (item 3.2, #65).

Every row of ``events`` written from migration 3 on carries ``prev_hash``
and ``hash``: the hash covers the row's content and the previous row's
hash, so removing, reordering or editing any row breaks every hash after
it. Triggers already refuse UPDATE and DELETE; the chain is for the case
where someone can drop a trigger.

A chain alone cannot catch a rewrite of the *whole* log, because the
rewriter can recompute every hash. That is what checkpoints are for: a
small record of (event count, last id, last hash) signed with a key the
lab's own account cannot read, exported somewhere it cannot write. Any
later log that does not contain the checkpointed row with the
checkpointed hash has been replaced.

The signature is HMAC-SHA256, from the standard library, so the verifier
needs the same key as the signer. That is deliberate: the operator both
exports and verifies, and the key never enters the lab's process. Moving
key and export directory under the operator account is item 4.5 (#70).

Rows from before the chain existed have NULL hashes and sit ahead of the
first hashed row; they are not covered.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

GENESIS = "0" * 64
CHECKPOINT_VERSION = 1
MIN_KEY_BYTES = 16


class CheckpointError(ValueError):
    """A checkpoint or its key is unusable."""


def _now() -> str:
    moment = datetime.now(UTC)
    return f"{moment.strftime('%Y-%m-%d %H:%M:%S')}.{moment.microsecond // 1000:03d}"


def event_hash(prev_hash: str, task_id: str | None, kind: str,
               from_state: str | None, to_state: str | None,
               detail: str | None, created_at: str) -> str:
    material = json.dumps(
        [prev_hash, task_id, kind, from_state, to_state, detail, created_at],
        separators=(",", ":"), ensure_ascii=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def append_event(conn: sqlite3.Connection, task_id: str | None, kind: str,
                 from_state: str | None = None, to_state: str | None = None,
                 detail: dict[str, Any] | None = None) -> None:
    """The one way to write an event.

    Reading the previous hash and inserting the next row must be one unit,
    or two writers would both chain onto the same predecessor and fork the
    log. Inside a caller's transaction that already holds. Outside one,
    this opens its own ``BEGIN IMMEDIATE``.
    """
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN IMMEDIATE")
    try:
        last = conn.execute(
            "SELECT hash FROM events WHERE hash IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        prev = last[0] if last else GENESIS
        encoded = json.dumps(detail) if detail else None
        created_at = _now()
        conn.execute(
            "INSERT INTO events (task_id, kind, from_state, to_state, detail, "
            "created_at, prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, kind, from_state, to_state, encoded, created_at, prev,
             event_hash(prev, task_id, kind, from_state, to_state, encoded, created_at)),
        )
    except BaseException:
        if own:
            conn.execute("ROLLBACK")
        raise
    if own:
        conn.execute("COMMIT")


@dataclass(frozen=True)
class ChainReport:
    ok: bool
    events: int              # hashed rows examined
    unchained: int           # legacy rows ahead of the chain
    last_id: int | None
    last_hash: str | None
    problem: str | None = None
    bad_id: int | None = None


def verify_chain(conn: sqlite3.Connection) -> ChainReport:
    """Walk the whole chain. Detects an edited, removed or reordered row."""
    prev = GENESIS
    seen = 0
    unchained = 0
    last_id: int | None = None
    last_hash: str | None = None
    for row in conn.execute(
        "SELECT id, task_id, kind, from_state, to_state, detail, created_at, "
        "prev_hash, hash FROM events ORDER BY id"
    ):
        rid, task_id, kind, frm, to, detail, created_at, row_prev, row_hash = tuple(row)
        if row_hash is None:
            if seen:
                return ChainReport(False, seen, unchained, last_id, last_hash,
                                   "unchained row after the chain began", rid)
            unchained += 1
            continue
        if row_prev != prev:
            return ChainReport(False, seen, unchained, last_id, last_hash,
                               "previous hash does not match: a row was removed or reordered", rid)
        if event_hash(prev, task_id, kind, frm, to, detail, created_at) != row_hash:
            return ChainReport(False, seen, unchained, last_id, last_hash,
                               "row content does not match its hash", rid)
        prev = row_hash
        seen += 1
        last_id, last_hash = rid, row_hash
    return ChainReport(True, seen, unchained, last_id, last_hash)


def _read_key(key_path: Path) -> bytes:
    try:
        key = key_path.read_bytes().strip()
    except OSError as exc:
        raise CheckpointError(f"cannot read key {key_path}: {exc}") from exc
    if len(key) < MIN_KEY_BYTES:
        raise CheckpointError(f"key must be at least {MIN_KEY_BYTES} bytes")
    return key


def _mac(key: bytes, body: dict[str, Any]) -> str:
    material = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(key, material, hashlib.sha256).hexdigest()


def make_checkpoint(conn: sqlite3.Connection, key_path: Path) -> dict[str, Any]:
    """Verify the chain, then sign its head. Refuses to sign a broken chain."""
    key = _read_key(key_path)
    report = verify_chain(conn)
    if not report.ok:
        raise CheckpointError(f"refusing to sign a broken chain: {report.problem} "
                              f"(event {report.bad_id})")
    body: dict[str, Any] = {
        "version": CHECKPOINT_VERSION,
        "events": report.events,
        "last_id": report.last_id,
        "last_hash": report.last_hash if report.last_hash else GENESIS,
        "created_at": _now(),
    }
    return {**body, "mac": _mac(key, body)}


def write_checkpoint(conn: sqlite3.Connection, key_path: Path, out_dir: Path) -> Path:
    checkpoint = make_checkpoint(conn, key_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"checkpoint-{int(checkpoint['last_id'] or 0):012d}-{checkpoint['last_hash'][:12]}.json"
    path = out_dir / name
    path.write_text(json.dumps(checkpoint, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def check_against_checkpoint(conn: sqlite3.Connection, checkpoint_path: Path,
                             key_path: Path) -> ChainReport:
    """Is the live log a faithful continuation of a signed checkpoint?

    Fails when the checkpoint's signature is wrong, when the chain no
    longer verifies, or when the checkpointed row is missing or carries a
    different hash, which is what a wholesale rewrite looks like.
    """
    key = _read_key(key_path)
    try:
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        mac = checkpoint.pop("mac")
    except (OSError, ValueError, KeyError) as exc:
        raise CheckpointError(f"unreadable checkpoint {checkpoint_path}: {exc}") from exc
    if not hmac.compare_digest(str(mac), _mac(key, checkpoint)):
        raise CheckpointError("checkpoint signature does not verify")
    report = verify_chain(conn)
    if not report.ok:
        return report
    last_id = checkpoint["last_id"]
    if last_id is None:
        return report
    row = conn.execute("SELECT hash FROM events WHERE id = ?", (last_id,)).fetchone()
    if row is None or row[0] != checkpoint["last_hash"]:
        return ChainReport(False, report.events, report.unchained, report.last_id,
                           report.last_hash,
                           "the log does not contain the checkpointed event: rewritten",
                           last_id)
    return report
