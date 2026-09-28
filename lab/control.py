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

Resume is the direction that adds authority, so it is signed. The
operator signs ``(generation, "running")``; a supervisor that has the
operator's public key treats an unsigned, forged or replayed ``running``
as still paused (``effective``). Pausing, draining and stopping only
remove authority and need no signature. Without a configured key the
switch works unsigned, as before, and the supervisor logs that it is
unprotected. Until the separate lab account exists (#70) the private key
is only as safe as the account that holds it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from lab.audit import append_event
from lab.operator import sign_action, verify_action

MODES = ("running", "paused", "draining", "stopped")


class ControlError(ValueError):
    """An unknown mode or an unusable control row."""


@dataclass(frozen=True)
class ControlState:
    mode: str
    reason: str | None
    set_by: str | None
    set_at: str | None
    generation: int = 0
    signature: str | None = None

    @property
    def leasing_allowed(self) -> bool:
        return self.mode == "running"


def get(conn: sqlite3.Connection) -> ControlState:
    row = conn.execute("SELECT mode, reason, set_by, set_at, generation, signature "
                       "FROM control WHERE id = 1").fetchone()
    if row is None:
        raise ControlError("control row missing")
    return ControlState(row[0], row[1], row[2], row[3], row[4], row[5])


def effective(conn: sqlite3.Connection, public_key: Ed25519PublicKey | None) -> ControlState:
    """The mode a supervisor should obey.

    With an operator key, ``running`` counts only if the operator signed
    this exact generation. The initial row (generation 0) needs none.
    """
    state = get(conn)
    if public_key is None or state.mode != "running" or state.generation == 0:
        return state
    if verify_action(public_key, state.signature, "control",
                     generation=state.generation, mode="running"):
        return state
    return replace(state, mode="paused",
                   reason="resume is not signed by the operator; still paused")


def set_mode(conn: sqlite3.Connection, mode: str, *, by: str,
             reason: str | None = None, signer: Ed25519PrivateKey | None = None) -> ControlState:
    if mode not in MODES:
        raise ControlError(f"unknown mode {mode!r}; expected one of {', '.join(MODES)}")
    before = get(conn)
    generation = before.generation + 1
    signature = (sign_action(signer, "control", generation=generation, mode="running")
                 if signer is not None and mode == "running" else None)
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE control SET mode = ?, reason = ?, set_by = ?, generation = ?, "
            "signature = ?, set_at = strftime('%Y-%m-%d %H:%M:%S', 'now') WHERE id = 1",
            (mode, reason, by, generation, signature))
        append_event(conn, None, "control_changed", before.mode, mode,
                     {"by": by, "reason": reason, "generation": generation,
                      "signed": signature is not None})
    except BaseException:
        if own:
            conn.execute("ROLLBACK")
        raise
    if own:
        conn.execute("COMMIT")
    return get(conn)
