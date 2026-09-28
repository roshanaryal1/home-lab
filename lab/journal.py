"""Operation journal: retry only when the outcome is known (item 1.7, #56).

An ordinary failure is not proof that nothing happened. A command that
timed out after doing its work, or a request whose response was lost,
looks the same as one that never started. For operations that are not
safe to repeat, the journal records each one before it runs, so a retry
can tell the cases apart:

    executing   written and committed before the operation starts
    confirmed   it finished, with a known result (success or failure)
    failed      refused before it had any effect; safe to run again
    uncertain   it started and nobody knows how it ended

On a retry, a confirmed operation is not run again: its recorded result
is returned. An operation found ``executing`` (the process died during
it) or ``uncertain`` is not run again either; the task is held until a
person reconciles it with ``lab.cli resolve``.

The operation id is stable across retries of the same task: a hash of
the task, the tool, the exact parameters and how many identical calls
came before it in the same run.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

NOW_MS = "strftime('%Y-%m-%d %H:%M:%f', 'now')"


def operation_id(task_id: str, tool: str, params_sha256: str, seq: int) -> str:
    material = f"{task_id}\n{tool}\n{params_sha256}\n{seq}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Operation:
    id: str
    task_id: str
    tool: str
    seq: int
    state: str
    result: dict[str, Any] | None
    error: str | None


class OperationJournal:
    """Reads and writes the ``operations`` table on the queue's connection.

    Every write is a single autocommitted statement: the ``executing``
    row must be durable before the operation starts, whatever happens
    to the caller afterwards.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, op_id: str) -> Operation | None:
        row = self._conn.execute(
            "SELECT id, task_id, tool, seq, state, result, error "
            "FROM operations WHERE id = ?", (op_id,)).fetchone()
        if row is None:
            return None
        return Operation(row["id"], row["task_id"], row["tool"], row["seq"],
                         row["state"], json.loads(row["result"]) if row["result"] else None,
                         row["error"])

    def begin(self, op_id: str, task_id: str, tool: str, params_sha256: str,
              seq: int) -> None:
        """Record that the operation is about to start (new or retried)."""
        self._conn.execute(
            "INSERT INTO operations (id, task_id, tool, params_sha256, seq, state) "
            "VALUES (?, ?, ?, ?, ?, 'executing') "
            "ON CONFLICT(id) DO UPDATE SET state = 'executing', error = NULL, "
            f"started_at = {NOW_MS}, finished_at = NULL WHERE state = 'failed'",
            (op_id, task_id, tool, params_sha256, seq))
        self._event(task_id, "operation_started", op_id, tool)

    def confirm(self, op_id: str, task_id: str, result: dict[str, Any]) -> None:
        self._finish(op_id, task_id, "confirmed", result=result)

    def failed(self, op_id: str, task_id: str, error: str) -> None:
        self._finish(op_id, task_id, "failed", error=error)

    def uncertain(self, op_id: str, task_id: str, error: str) -> None:
        self._finish(op_id, task_id, "uncertain", error=error)

    def _finish(self, op_id: str, task_id: str, state: str, *,
                result: dict[str, Any] | None = None, error: str | None = None) -> None:
        self._conn.execute(
            f"UPDATE operations SET state = ?, result = ?, error = ?, finished_at = {NOW_MS} "
            "WHERE id = ?",
            (state, json.dumps(result) if result is not None else None, error, op_id))
        self._event(task_id, f"operation_{state}", op_id, None, error)

    def unresolved(self, task_id: str | None = None) -> list[sqlite3.Row]:
        """Operations nobody knows the outcome of: started and never finished,
        or finished as uncertain."""
        return self._conn.execute(
            "SELECT * FROM operations WHERE state IN ('executing', 'uncertain') "
            "AND (? IS NULL OR task_id = ?) ORDER BY started_at",
            (task_id, task_id)).fetchall()

    def resolve(self, op_id: str, *, happened: bool, decided_by: str) -> str | None:
        """A person's reconciliation: the operation did, or did not, take effect.

        ``happened`` becomes ``confirmed`` (never run again; a retry gets a
        result saying it was reconciled) and not-happened becomes
        ``failed`` (the retry runs it). Only unresolved operations change.
        Returns the task id, or None if nothing was unresolved under that id.
        """
        state = "confirmed" if happened else "failed"
        result = {"reconciled": True, "happened": True} if happened else None
        cur = self._conn.execute(
            "UPDATE operations SET state = ?, result = ?, resolved_by = ?, "
            f"finished_at = {NOW_MS} "
            "WHERE id = ? AND state IN ('executing', 'uncertain')",
            (state, json.dumps(result) if result else None, decided_by, op_id))
        if cur.rowcount != 1:
            return None
        row = self._conn.execute("SELECT task_id FROM operations WHERE id = ?",
                                 (op_id,)).fetchone()
        self._event(row["task_id"], "operation_resolved", op_id, None,
                    f"{state} by {decided_by}")
        return str(row["task_id"])

    def _event(self, task_id: str, kind: str, op_id: str, tool: str | None,
               error: str | None = None) -> None:
        detail = {"operation": op_id}
        if tool:
            detail["tool"] = tool
        if error:
            detail["error"] = error
        self._conn.execute("INSERT INTO events (task_id, kind, detail) VALUES (?, ?, ?)",
                           (task_id, kind, json.dumps(detail)))
