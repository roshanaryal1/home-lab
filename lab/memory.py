"""Inspectable memory, FTS5 first (item 8.4, #85).

Memory you cannot inspect or revoke is a path for poisoned content, so
every entry here carries where it came from, a hash, when it was made,
who made it, a trust level, an expiry and an embedding version, and can
be inspected, corrected, revoked or deleted.

The write rule is ADR 0003's: **an outsider's content never becomes
curated memory by itself.**

* ``curated`` memory is a fact a person promoted. It needs a named
  promoter and a trusted source, and is refused for anything that came
  from a tainted task.
* ``evidence`` memory is a source-backed note. It always records its
  source and hash, is always untrusted, and always expires (30 days by
  default). Untrusted memory can inform a claim; it cannot be a grant, a
  destination or a policy, because retrieval hands back fixed-schema
  data and nothing else reads it as instruction.

Retrieval is SQLite FTS5 with the query rebuilt token by token so a
search string cannot use FTS syntax, over active, unexpired rows only.
There is no embedding yet (``embedding_version`` is NULL, the baseline);
when one is added it must beat this baseline on a measured task first.

A revoked memory leaves the index in the same transaction. Anything that
cited it as evidence is told: ``revoke`` can pass the evidence ledger, and
the claims that leaned on that memory lose that support and fall back.

Proposals (#253, migration 15). A task may propose a memory through the
broker tool ``memory.propose``: the text, the source it came from, and
why. A proposal lives in its own table, ``memory_proposals``, which the
search index never reads, and it has no expiry, so it cannot drift into
curated memory by waiting. The only path to active is ``accept``, which
needs the owner's decision signed with the operator private key and
verified here with the operator public key, over the proposal's id, the
hash of its exact text, its source and its taint mark. Whether the
proposing task was tainted is read from the task's own row, never from
the caller. Rejecting removes nothing trusted, so it needs no signature,
but it is audited like proposing and accepting.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from lab import operator as operator_keys
from lab.audit import append_event
from lab.untrusted import clean

if TYPE_CHECKING:
    from lab.ledger import Ledger

DEFAULT_EVIDENCE_TTL = timedelta(days=30)
MAX_MEMORY_CHARS = 4000
MAX_RESULTS = 20
EXCERPT_CHARS = 600
MAX_PROPOSAL_FIELD_CHARS = 500          # a proposal's source id and reason
MAX_PENDING_PER_TASK = 20
ACCEPT_PURPOSE = "memory-accept"
_HEX64 = re.compile(r"[0-9a-f]{64}")
_TOKEN = re.compile(r"[A-Za-z0-9]{2,}")


class MemoryRefused(ValueError):
    """The memory layer refused an operation."""


@dataclass(frozen=True)
class Recalled:
    id: int
    kind: str
    trust: str
    excerpt: str
    source_id: str
    source_sha256: str | None
    created_at: str
    expires_at: str | None


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S.") + f"{moment.microsecond // 1000:03d}"


def fts_query(text: str) -> str:
    """Rebuild a search string as quoted tokens. Operators, column filters,
    NEAR and wildcards in the input become plain words or vanish."""
    tokens = _TOKEN.findall(text)[:12]
    return " ".join(f'"{t}"' for t in tokens)


class Memory:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # -------------------------------------------------------------- writing

    def _insert(self, kind: str, text: str, source_id: str, source_sha256: str | None,
                trust: str, created_by: str, expires_at: datetime | None,
                corrected_from: int | None = None, proposal_id: int | None = None) -> int:
        text = clean(text).strip()
        if not text or len(text) > MAX_MEMORY_CHARS:
            raise MemoryRefused(f"memory text must be 1 to {MAX_MEMORY_CHARS} characters")
        if not source_id.strip() or not created_by.strip():
            raise MemoryRefused("a memory needs a source and a creator")
        cur = self._conn.execute(
            "INSERT INTO memories (kind, text, text_sha256, source_id, source_sha256, trust, "
            "created_by, expires_at, corrected_from, proposal_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, text, hashlib.sha256(text.encode()).hexdigest(), source_id.strip(),
             source_sha256, trust, created_by.strip(),
             _stamp(expires_at) if expires_at else None, corrected_from, proposal_id))
        memory_id = int(cur.lastrowid or 0)
        append_event(self._conn, None, "memory_added", detail={
            "memory": memory_id, "kind": kind, "trust": trust, "source_id": source_id,
            "by": created_by, "corrected_from": corrected_from, "proposal": proposal_id})
        return memory_id

    def add_curated(self, text: str, source_id: str, promoted_by: str, *,
                    from_tainted_task: bool = False,
                    expires_at: datetime | None = None) -> int:
        """A fact a person promoted. Refused when it came from untrusted input."""
        if from_tainted_task:
            raise MemoryRefused("curated memory cannot come from a tainted task: "
                               "store it as evidence, or have a person restate it")
        if not promoted_by.strip():
            raise MemoryRefused("curated memory needs a named promoter")
        return self._insert("curated", text, source_id, None, "trusted", promoted_by,
                            expires_at)

    def add_evidence(self, text: str, source_id: str, source_sha256: str, created_by: str, *,
                     ttl: timedelta = DEFAULT_EVIDENCE_TTL) -> int:
        """A source-backed note. Untrusted and expiring, always."""
        if len(source_sha256) != 64:
            raise MemoryRefused("evidence memory records the source's sha256")
        if ttl <= timedelta(0):
            raise MemoryRefused("evidence memory must expire in the future")
        return self._insert("evidence", text, source_id, source_sha256, "untrusted",
                            created_by, _now() + ttl)

    # ------------------------------------------------------------ retrieval

    def search(self, query: str, *, limit: int = 5, task_id: str | None = None,
               now: datetime | None = None) -> list[Recalled]:
        """Active, unexpired memories matching the words of ``query``.

        Returns fixed-schema data with provenance. If ``task_id`` is given the
        use is recorded, so revoking a memory can name what read it.
        """
        match = fts_query(query)
        if not match:
            return []
        rows = self._conn.execute(
            "SELECT m.* FROM memories_fts f JOIN memories m ON m.id = f.rowid "
            "WHERE memories_fts MATCH ? AND m.state = 'active' "
            "AND (m.expires_at IS NULL OR m.expires_at > ?) ORDER BY rank LIMIT ?",
            (match, _stamp(now or _now()), max(1, min(limit, MAX_RESULTS)))).fetchall()
        out = []
        for row in rows:
            out.append(Recalled(row["id"], row["kind"], row["trust"],
                                row["text"][:EXCERPT_CHARS], row["source_id"],
                                row["source_sha256"], row["created_at"], row["expires_at"]))
            if task_id is not None:
                self._conn.execute(
                    "INSERT INTO memory_uses (memory_id, task_id) VALUES (?, ?)",
                    (row["id"], task_id))
        return out

    # ----------------------------------------------------------- inspection

    def inspect(self, memory_id: int) -> sqlite3.Row:
        row: sqlite3.Row | None = self._conn.execute(
            "SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if row is None:
            raise MemoryRefused(f"no memory {memory_id}")
        return row

    def used_by(self, memory_id: int) -> list[str]:
        return sorted({r[0] for r in self._conn.execute(
            "SELECT task_id FROM memory_uses WHERE memory_id = ?", (memory_id,))})

    # ------------------------------------------------- correct, revoke, delete

    def _end(self, memory_id: int, state: str, by: str, reason: str) -> sqlite3.Row:
        row = self.inspect(memory_id)
        if row["state"] != "active":
            raise MemoryRefused(f"memory {memory_id} is already {row['state']}")
        if not by.strip():
            raise MemoryRefused("say who is ending this memory")
        self._conn.execute(
            "UPDATE memories SET state = ?, ended_at = ?, ended_by = ?, ended_reason = ?"
            + (", text = ''" if state == "deleted" else "") + " WHERE id = ?",
            (state, _stamp(_now()), by.strip(), reason, memory_id))
        return row

    def revoke(self, memory_id: int, by: str, reason: str, *,
               ledger: Ledger | None = None) -> dict[str, list[str] | int]:
        """Stop trusting a memory. It leaves retrieval immediately; if a ledger
        is given, evidence that cited it is withdrawn and its claims re-derived."""
        self._end(memory_id, "revoked", by, reason)
        withdrawn = ledger.invalidate_source(f"memory:{memory_id}", reason) if ledger else 0
        affected = self.used_by(memory_id)
        append_event(self._conn, None, "memory_revoked", detail={
            "memory": memory_id, "by": by, "reason": reason, "read_by_tasks": affected,
            "evidence_withdrawn": withdrawn})
        return {"read_by_tasks": affected, "evidence_withdrawn": withdrawn}

    def delete(self, memory_id: int, by: str, reason: str, *,
               ledger: Ledger | None = None) -> None:
        """Remove the text for good; the row stays as a tombstone with the hash."""
        self._end(memory_id, "deleted", by, reason)
        if ledger:
            ledger.invalidate_source(f"memory:{memory_id}", reason)
        append_event(self._conn, None, "memory_deleted", detail={
            "memory": memory_id, "by": by, "reason": reason})

    def correct(self, memory_id: int, new_text: str, by: str, reason: str, *,
                ledger: Ledger | None = None) -> int:
        """Replace a memory with a corrected one that points back at it."""
        old = self.inspect(memory_id)
        if old["state"] != "active":
            raise MemoryRefused(f"memory {memory_id} is already {old['state']}")
        expires = _now() + DEFAULT_EVIDENCE_TTL if old["kind"] == "evidence" else None
        new_id = self._insert(old["kind"], new_text, old["source_id"], old["source_sha256"],
                              old["trust"], by, expires, corrected_from=memory_id)
        self.revoke(memory_id, by, f"corrected: {reason}", ledger=ledger)
        return new_id

    def sweep_expired(self, now: datetime | None = None) -> int:
        """Retire expired memories so they leave the index rather than only
        being filtered at query time."""
        rows = self._conn.execute(
            "SELECT id FROM memories WHERE state = 'active' AND expires_at IS NOT NULL "
            "AND expires_at <= ?", (_stamp(now or _now()),)).fetchall()
        for row in rows:
            self._end(row["id"], "revoked", "expiry", "expired")
            append_event(self._conn, None, "memory_expired", detail={"memory": row["id"]})
        return len(rows)

    # ------------------------------------------------------------ proposals

    def propose(self, task_id: str, text: str, source_id: str, reason: str, *,
                source_sha256: str | None = None) -> sqlite3.Row:
        """Record a task's proposal as pending. Never active, never searched.

        The taint mark comes from the task's row, and a task the lab does not
        know counts as tainted. Proposing the same text twice from one task
        returns the first proposal, so a retried call adds nothing.
        """
        text = clean(text).strip()
        source_id = clean(source_id).strip()
        reason = clean(reason).strip()
        if not text or len(text) > MAX_MEMORY_CHARS:
            raise MemoryRefused(f"proposal text must be 1 to {MAX_MEMORY_CHARS} characters")
        for name, value in (("source", source_id), ("reason", reason)):
            if not value or len(value) > MAX_PROPOSAL_FIELD_CHARS:
                raise MemoryRefused(
                    f"a proposal's {name} must be 1 to {MAX_PROPOSAL_FIELD_CHARS} characters")
        if source_sha256 is not None and not _HEX64.fullmatch(source_sha256):
            raise MemoryRefused("a proposal's source_sha256 is 64 lowercase hex characters")
        digest = hashlib.sha256(text.encode()).hexdigest()
        existing: sqlite3.Row | None = self._conn.execute(
            "SELECT * FROM memory_proposals WHERE task_id = ? AND text_sha256 = ?",
            (task_id, digest)).fetchone()
        if existing is not None:
            return existing
        pending = self._conn.execute(
            "SELECT COUNT(*) FROM memory_proposals WHERE task_id = ? AND state = 'pending'",
            (task_id,)).fetchone()[0]
        if pending >= MAX_PENDING_PER_TASK:
            raise MemoryRefused(f"this task already has {MAX_PENDING_PER_TASK} pending "
                                "proposals")
        task = self._conn.execute("SELECT tainted FROM tasks WHERE id = ?",
                                  (task_id,)).fetchone()
        tainted = task is None or bool(task["tainted"])
        cur = self._conn.execute(
            "INSERT INTO memory_proposals (task_id, text, text_sha256, source_id, "
            "source_sha256, reason, tainted) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, text, digest, source_id, source_sha256, reason, int(tainted)))
        proposal_id = int(cur.lastrowid or 0)
        append_event(self._conn, task_id, "memory_proposed", detail={
            "proposal": proposal_id, "text_sha256": digest, "source_id": source_id,
            "tainted": tainted})
        return self.proposal(proposal_id)

    def proposal(self, proposal_id: int) -> sqlite3.Row:
        row: sqlite3.Row | None = self._conn.execute(
            "SELECT * FROM memory_proposals WHERE id = ?", (proposal_id,)).fetchone()
        if row is None:
            raise MemoryRefused(f"no proposal {proposal_id}")
        return row

    def proposals(self, state: str = "pending") -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM memory_proposals WHERE state = ? ORDER BY id", (state,)).fetchall()

    def accept(self, proposal_id: int, by: str, signature: str | None,
               operator_key: Ed25519PublicKey | None) -> int:
        """Make a pending proposal curated memory, with ``by`` as its promoter.

        Refused unless ``signature`` is the operator's, over this proposal as
        it is stored now. There is no unsigned mode: without the operator
        public key nothing can be accepted.
        """
        if operator_key is None:
            raise MemoryRefused("accepting a proposal needs the operator public key")
        if not by.strip():
            raise MemoryRefused("say who is accepting this proposal")
        row = self.proposal(proposal_id)
        if row["state"] != "pending":
            raise MemoryRefused(f"proposal {proposal_id} is already {row['state']}")
        if hashlib.sha256(row["text"].encode()).hexdigest() != row["text_sha256"]:
            raise MemoryRefused(f"proposal {proposal_id} text no longer matches its hash")
        if not operator_keys.verify_action(operator_key, signature, ACCEPT_PURPOSE,
                                           **acceptance_fields(row, by)):
            append_event(self._conn, row["task_id"], "memory_proposal_refused", detail={
                "proposal": proposal_id, "by": by, "reason": "no valid operator signature"})
            raise MemoryRefused(f"accepting proposal {proposal_id} needs a valid "
                                "operator signature")
        own_tx = not self._conn.in_transaction
        if own_tx:
            self._conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self._conn.execute(
                "UPDATE memory_proposals SET state = 'accepted', decided_by = ?, "
                "decided_at = strftime('%Y-%m-%d %H:%M:%f', 'now'), signature = ? "
                "WHERE id = ? AND state = 'pending'", (by.strip(), signature, proposal_id))
            if cur.rowcount != 1:
                raise MemoryRefused(f"proposal {proposal_id} was decided meanwhile")
            memory_id = self._insert("curated", row["text"], row["source_id"],
                                     row["source_sha256"], "trusted", by, None,
                                     proposal_id=proposal_id)
            self._conn.execute("UPDATE memory_proposals SET memory_id = ? WHERE id = ?",
                               (memory_id, proposal_id))
            append_event(self._conn, row["task_id"], "memory_proposal_accepted", detail={
                "proposal": proposal_id, "memory": memory_id, "by": by,
                "tainted": bool(row["tainted"]), "text_sha256": row["text_sha256"]})
        except BaseException:
            if own_tx:
                self._conn.execute("ROLLBACK")
            raise
        if own_tx:
            self._conn.execute("COMMIT")
        return memory_id

    def reject(self, proposal_id: int, by: str, reason: str) -> None:
        """Turn a proposal down. Nothing trusted changes, so no signature."""
        if not by.strip() or not reason.strip():
            raise MemoryRefused("rejecting a proposal needs a name and a reason")
        row = self.proposal(proposal_id)
        cur = self._conn.execute(
            "UPDATE memory_proposals SET state = 'rejected', decided_by = ?, "
            "decided_at = strftime('%Y-%m-%d %H:%M:%f', 'now'), decision_reason = ? "
            "WHERE id = ? AND state = 'pending'", (by.strip(), reason.strip(), proposal_id))
        if cur.rowcount != 1:
            raise MemoryRefused(f"proposal {proposal_id} is already {row['state']}")
        append_event(self._conn, row["task_id"], "memory_proposal_rejected", detail={
            "proposal": proposal_id, "by": by, "reason": reason})


def acceptance_fields(row: sqlite3.Row, by: str) -> dict[str, object]:
    """What an owner's acceptance signs: this proposal, exactly as stored."""
    return {"proposal": row["id"], "task_id": row["task_id"],
            "text_sha256": row["text_sha256"], "source_id": row["source_id"],
            "source_sha256": row["source_sha256"], "tainted": bool(row["tainted"]),
            "by": by.strip()}


def sign_acceptance(key: Ed25519PrivateKey, row: sqlite3.Row, by: str) -> str:
    """The owner's signature over a decision to accept ``row``."""
    return operator_keys.sign_action(key, ACCEPT_PURPOSE, **acceptance_fields(row, by))
