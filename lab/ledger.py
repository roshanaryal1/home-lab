"""Evidence ledger: claim status is not task status (item #90, merges 8.5).

A task that finished has not shown that what it concluded is true. So a
research task records its question, protocol version, data and code
identifiers, outputs and validation checks, and every conclusion it
reaches is a *claim* with a status of its own:

    unverified    stated, with no usable evidence behind it
    supported     at least one source quote backs it, and nothing contradicts it
    contradicted  at least one source quote contradicts it
    verified      supported by at least two independent sources, a contradiction
                  pass has run since the last evidence changed, and a person or
                  a named check has signed it off

Rules the code enforces, each with a test:

* Completing a task never changes a claim.
* A quote must appear in the snapshot it cites. A claim cannot cite text the
  source does not contain.
* A snapshot is the exact bytes of the source at acquisition, stored in the
  content-addressed artifact store. A reviewer opens any claim's evidence
  with ``open_snapshot`` and the bytes are re-hashed on the way out.
* A single source, or several snapshots of the same source, never verify a
  claim. Contradicting evidence anywhere blocks ``supported`` and
  ``verified``.
* A draft is reviewable only after ``run_review_pass``, which records a
  contradiction check and the list of claims with missing evidence, and only
  while no evidence has changed since.

Everything that changes state is also an event in the audit log.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from lab.artifacts import ArtifactError, ArtifactStore
from lab.audit import append_event
from lab.evals import collect_provenance

STATUSES = ("unverified", "supported", "contradicted", "verified")
MIN_INDEPENDENT_SOURCES = 2
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024


class LedgerError(ValueError):
    """The ledger refused an operation."""


KINDS = ("finding", "mechanism", "measurement")


@dataclass(frozen=True)
class ClaimView:
    id: int
    text: str
    status: str
    supports: int
    contradicts: int
    sources: int
    kind: str = "finding"


@dataclass(frozen=True)
class ReviewState:
    reviewable: bool
    reasons: list[str]
    missing_evidence: list[int]
    contradicted: list[int]


class Ledger:
    def __init__(self, conn: sqlite3.Connection, store: ArtifactStore) -> None:
        self._conn = conn
        self._store = store

    # ------------------------------------------------------------ the task

    def open_research_task(self, task_id: str, question: str, protocol_version: str, *,
                           data_ids: list[str] | None = None,
                           outputs: list[str] | None = None,
                           validation_checks: list[str] | None = None,
                           code_ids: dict[str, Any] | None = None) -> None:
        """Record what is being asked and how. ``code_ids`` defaults to the lab
        commit from the same provenance collector the evaluations use."""
        if code_ids is None:
            prov = collect_provenance()
            code_ids = {"lab_commit": prov["lab_commit"], "tree_dirty": prov["tree_dirty"]}
        try:
            self._conn.execute(
                "INSERT INTO research_tasks (task_id, question, protocol_version, data_ids, "
                "code_ids, outputs, validation_checks) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (task_id, question, protocol_version, json.dumps(data_ids or []),
                 json.dumps(code_ids), json.dumps(outputs or []),
                 json.dumps(validation_checks or [])))
        except sqlite3.IntegrityError as exc:
            raise LedgerError(f"research task {task_id!r} already exists or is malformed") \
                from exc
        append_event(self._conn, task_id, "research_task_opened",
                     detail={"protocol_version": protocol_version})

    def research_task(self, task_id: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM research_tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise LedgerError(f"no research task {task_id!r}")
        return row  # type: ignore[no-any-return]

    # ----------------------------------------------------------- snapshots

    def add_snapshot(self, task_id: str, source_id: str, source_type: str, data: bytes) -> int:
        self.research_task(task_id)
        if len(data) > MAX_SNAPSHOT_BYTES:
            raise LedgerError("snapshot larger than the cap")
        if not source_id:
            raise LedgerError("a snapshot needs a source id")
        sha, size = self._store.put_bytes(data)
        cur = self._conn.execute(
            "INSERT INTO evidence_snapshots (task_id, source_id, source_type, sha256, size) "
            "VALUES (?, ?, ?, ?, ?)", (task_id, source_id, source_type, sha, size))
        append_event(self._conn, task_id, "evidence_snapshot", detail={
            "snapshot": cur.lastrowid, "source_id": source_id, "sha256": sha})
        return int(cur.lastrowid or 0)

    def open_snapshot(self, snapshot_id: int) -> bytes:
        """The exact source bytes, re-hashed on the way out."""
        row = self._conn.execute(
            "SELECT sha256 FROM evidence_snapshots WHERE id = ?", (snapshot_id,)).fetchone()
        if row is None:
            raise LedgerError(f"no snapshot {snapshot_id}")
        try:
            return self._store.read(row["sha256"])
        except ArtifactError as exc:
            raise LedgerError(f"snapshot {snapshot_id} cannot be opened: {exc}") from None

    # -------------------------------------------------------------- claims

    def add_claim(self, task_id: str, text: str, kind: str = "finding") -> int:
        self.research_task(task_id)
        if not text.strip():
            raise LedgerError("a claim needs text")
        if kind not in KINDS:
            raise LedgerError(f"kind is one of {KINDS}")
        cur = self._conn.execute(
            "INSERT INTO claims (task_id, text, kind) VALUES (?, ?, ?)",
            (task_id, text.strip(), kind))
        claim_id = int(cur.lastrowid or 0)
        append_event(self._conn, task_id, "claim_added", detail={"claim": claim_id})
        return claim_id

    def _claim(self, claim_id: int) -> sqlite3.Row:
        row = self._conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
        if row is None:
            raise LedgerError(f"no claim {claim_id}")
        return row  # type: ignore[no-any-return]

    def link(self, claim_id: int, snapshot_id: int, relation: str, quote: str) -> None:
        """Attach a source quote to a claim. The quote must be in the snapshot."""
        claim = self._claim(claim_id)
        if relation not in ("supports", "contradicts"):
            raise LedgerError("relation is 'supports' or 'contradicts'")
        snap = self._conn.execute(
            "SELECT * FROM evidence_snapshots WHERE id = ?", (snapshot_id,)).fetchone()
        if snap is None or snap["task_id"] != claim["task_id"]:
            raise LedgerError("the snapshot belongs to a different research task")
        if not quote.strip():
            raise LedgerError("a link needs a quote")
        source = self.open_snapshot(snapshot_id).decode("utf-8", errors="replace")
        if quote not in source:
            raise LedgerError("the quote does not appear in the snapshot")
        try:
            self._conn.execute(
                "INSERT INTO claim_evidence (claim_id, snapshot_id, relation, quote) "
                "VALUES (?, ?, ?, ?)", (claim_id, snapshot_id, relation, quote))
        except sqlite3.IntegrityError:
            raise LedgerError("that exact link already exists") from None
        self._recompute(claim_id, keep_verified=False)

    def _counts(self, claim_id: int) -> tuple[int, int, int]:
        supports, contradicts = self._conn.execute(
            "SELECT COALESCE(SUM(relation = 'supports'), 0), "
            "COALESCE(SUM(relation = 'contradicts'), 0) FROM claim_evidence "
            "WHERE claim_id = ?", (claim_id,)).fetchone()
        sources = self._conn.execute(
            "SELECT COUNT(DISTINCT s.source_id) FROM claim_evidence e "
            "JOIN evidence_snapshots s ON s.id = e.snapshot_id "
            "WHERE e.claim_id = ? AND e.relation = 'supports'", (claim_id,)).fetchone()[0]
        return int(supports), int(contradicts), int(sources)

    def _set_status(self, claim: sqlite3.Row, status: str, by: str | None = None) -> None:
        if claim["status"] == status:
            return
        self._conn.execute(
            "UPDATE claims SET status = ?, status_by = ?, "
            "updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now') WHERE id = ?",
            (status, by, claim["id"]))
        append_event(self._conn, claim["task_id"], "claim_status", detail={
            "claim": claim["id"], "from": claim["status"], "to": status, "by": by})

    def _recompute(self, claim_id: int, *, keep_verified: bool) -> None:
        """Evidence moves a claim between unverified, supported and contradicted.
        It can take verification away; it never grants it. New evidence
        (``keep_verified=False``) always sends a verified claim back to be
        checked again, because the sign-off was for the evidence that existed."""
        claim = self._claim(claim_id)
        supports, contradicts, sources = self._counts(claim_id)
        if contradicts:
            new = "contradicted"
        elif (keep_verified and claim["status"] == "verified" and supports
              and sources >= MIN_INDEPENDENT_SOURCES):
            new = "verified"
        elif supports:
            new = "supported"
        else:
            new = "unverified"
        self._set_status(claim, new, claim["status_by"] if new == "verified" else None)

    def verify(self, claim_id: int, by: str) -> None:
        """A person or a named check signs a claim off. Never automatic."""
        claim = self._claim(claim_id)
        if not by.strip():
            raise LedgerError("say who is verifying")
        supports, contradicts, sources = self._counts(claim_id)
        if contradicts:
            raise LedgerError("a contradicted claim cannot be verified")
        if not supports:
            raise LedgerError("no supporting evidence")
        if sources < MIN_INDEPENDENT_SOURCES:
            raise LedgerError(
                f"needs {MIN_INDEPENDENT_SOURCES} independent sources, has {sources}")
        review = self._latest_pass(claim["task_id"])
        if review is None or not self._pass_is_current(review):
            raise LedgerError("run a review pass (contradiction check) after the last "
                              "evidence change before verifying")
        self._set_status(claim, "verified", by.strip())

    # ------------------------------------------------------------- reading

    def claims(self, task_id: str) -> list[ClaimView]:
        views = []
        for row in self._conn.execute(
                "SELECT * FROM claims WHERE task_id = ? ORDER BY id", (task_id,)).fetchall():
            supports, contradicts, sources = self._counts(row["id"])
            views.append(ClaimView(row["id"], row["text"], row["status"], supports,
                                   contradicts, sources, row["kind"]))
        return views

    def support_sources(self, claim_id: int) -> set[tuple[str, str]]:
        """(source_id, source_type) of every snapshot that supports a claim."""
        return {(r["source_id"], r["source_type"]) for r in self._conn.execute(
            "SELECT DISTINCT s.source_id, s.source_type FROM claim_evidence e "
            "JOIN evidence_snapshots s ON s.id = e.snapshot_id "
            "WHERE e.claim_id = ? AND e.relation = 'supports'", (claim_id,))}

    def evidence(self, claim_id: int) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT e.relation, e.quote, s.id AS snapshot_id, s.source_id, s.sha256 "
            "FROM claim_evidence e JOIN evidence_snapshots s ON s.id = e.snapshot_id "
            "WHERE e.claim_id = ? ORDER BY e.id", (claim_id,)).fetchall()

    # -------------------------------------------------------------- review

    def _last_change(self, task_id: str) -> int:
        return int(self._conn.execute(
            "SELECT COALESCE(MAX(e.id), 0) FROM claim_evidence e "
            "JOIN claims c ON c.id = e.claim_id WHERE c.task_id = ?", (task_id,)).fetchone()[0])

    def _latest_pass(self, task_id: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._conn.execute(
            "SELECT * FROM review_passes WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,)).fetchone()
        return row

    def _pass_is_current(self, review: sqlite3.Row) -> bool:
        claim_count = int(self._conn.execute(
            "SELECT COUNT(*) FROM claims WHERE task_id = ?", (review["task_id"],)).fetchone()[0])
        return bool(review["last_change_id"] == self._last_change(review["task_id"])
                    and review["claims_seen"] == claim_count)

    def run_review_pass(self, task_id: str, ran_by: str) -> ReviewState:
        """The contradiction pass and the missing-evidence list.

        Re-opens every cited snapshot (so a corrupted or vanished source
        counts against the claim), re-checks each quote against its bytes,
        recomputes each claim's status from what still holds, and records the
        list of claims with no usable support and the list with contradictions.
        """
        self.research_task(task_id)
        for claim in self._conn.execute(
                "SELECT id FROM claims WHERE task_id = ?", (task_id,)).fetchall():
            broken = []
            for row in self.evidence(claim["id"]):
                try:
                    text = self.open_snapshot(row["snapshot_id"]).decode("utf-8", "replace")
                except LedgerError:
                    broken.append(row)
                    continue
                if row["quote"] not in text:
                    broken.append(row)
            for row in broken:
                self._conn.execute(
                    "DELETE FROM claim_evidence WHERE claim_id = ? AND snapshot_id = ? "
                    "AND relation = ? AND quote = ?",
                    (claim["id"], row["snapshot_id"], row["relation"], row["quote"]))
                append_event(self._conn, task_id, "evidence_invalidated", detail={
                    "claim": claim["id"], "snapshot": row["snapshot_id"]})
            self._recompute(claim["id"], keep_verified=True)
        views = self.claims(task_id)
        missing = [c.id for c in views if c.supports == 0]
        contradicted = [c.id for c in views if c.contradicts > 0]
        self._conn.execute(
            "INSERT INTO review_passes (task_id, ran_by, last_change_id, claims_seen, "
            "missing_evidence, contradictions) VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, ran_by, self._last_change(task_id), len(views), json.dumps(missing),
             json.dumps(contradicted)))
        append_event(self._conn, task_id, "review_pass", detail={
            "by": ran_by, "missing_evidence": missing, "contradicted": contradicted})
        return self.review_state(task_id)

    def review_state(self, task_id: str) -> ReviewState:
        review = self._latest_pass(task_id)
        if review is None:
            return ReviewState(False, ["no review pass has been run"], [], [])
        missing = json.loads(review["missing_evidence"])
        contradicted = json.loads(review["contradictions"])
        if not self._pass_is_current(review):
            return ReviewState(False, ["evidence or claims changed after the last review pass"],
                               missing, contradicted)
        return ReviewState(True, [], missing, contradicted)
