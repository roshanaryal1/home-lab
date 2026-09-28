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
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from lab.audit import append_event
from lab.ledger import Ledger
from lab.untrusted import clean

DEFAULT_EVIDENCE_TTL = timedelta(days=30)
MAX_MEMORY_CHARS = 4000
MAX_RESULTS = 20
EXCERPT_CHARS = 600
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
                corrected_from: int | None = None) -> int:
        text = clean(text).strip()
        if not text or len(text) > MAX_MEMORY_CHARS:
            raise MemoryRefused(f"memory text must be 1 to {MAX_MEMORY_CHARS} characters")
        if not source_id.strip() or not created_by.strip():
            raise MemoryRefused("a memory needs a source and a creator")
        cur = self._conn.execute(
            "INSERT INTO memories (kind, text, text_sha256, source_id, source_sha256, trust, "
            "created_by, expires_at, corrected_from) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, text, hashlib.sha256(text.encode()).hexdigest(), source_id.strip(),
             source_sha256, trust, created_by.strip(),
             _stamp(expires_at) if expires_at else None, corrected_from))
        memory_id = int(cur.lastrowid or 0)
        append_event(self._conn, None, "memory_added", detail={
            "memory": memory_id, "kind": kind, "trust": trust, "source_id": source_id,
            "by": created_by, "corrected_from": corrected_from})
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
