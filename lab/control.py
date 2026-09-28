"""The operator's mode switch (item 6.3, #79).

* ``running``   normal.
* ``paused``    nothing new is leased; work in flight finishes.
* ``draining``  as paused, and the supervisor exits once idle.
* ``stopped``   emergency stop: broker authority is revoked first, then
                running handlers are cancelled and their worker processes
                killed (``Supervisor.emergency_stop``). The mode stays
                ``stopped`` until a person resumes, so a restart does not
                silently pick work back up.

The mode is one database row read by the supervisor on every lease
attempt and by a watcher on every poll. Changes are audit events.

Honest limit: until the separate lab account exists (#70) the agent's
own account can write this row too. Pausing and stopping only remove
authority, so that is the safe direction; resume is the one that should
require the operator, and does once the account boundary is real.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from lab.audit import append_event

MODES = ("running", "paused", "draining", "stopped")


class ControlError(ValueError):
    """An unknown mode or an unusable control row."""


@dataclass(frozen=True)
class ControlState:
    mode: str
    reason: str | None
    set_by: str | None
    set_at: str | None

    @property
    def leasing_allowed(self) -> bool:
        return self.mode == "running"


def get(conn: sqlite3.Connection) -> ControlState:
    row = conn.execute(
        "SELECT mode, reason, set_by, set_at FROM control WHERE id = 1").fetchone()
    if row is None:
        raise ControlError("control row missing")
    return ControlState(row[0], row[1], row[2], row[3])


def set_mode(conn: sqlite3.Connection, mode: str, *, by: str,
             reason: str | None = None) -> ControlState:
    if mode not in MODES:
        raise ControlError(f"unknown mode {mode!r}; expected one of {', '.join(MODES)}")
    before = get(conn).mode
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE control SET mode = ?, reason = ?, set_by = ?, "
            "set_at = strftime('%Y-%m-%d %H:%M:%S', 'now') WHERE id = 1",
            (mode, reason, by))
        append_event(conn, None, "control_changed", before, mode,
                     {"by": by, "reason": reason})
    except BaseException:
        if own:
            conn.execute("ROLLBACK")
        raise
    if own:
        conn.execute("COMMIT")
    return get(conn)
