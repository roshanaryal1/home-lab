"""Reconciling a send whose outcome is unknown (item 8.6, #86).

The broker writes each credentialed send down before it goes out, sends an
idempotency key the provider also sees, and records the provider's own id
when the response arrives. When the response is lost (a timeout after the
provider acted, a crash between the two) the send is 'reserved' but not
'confirmed', and the operation journal holds the task rather than resend.

``reconcile`` asks the provider. If the provider has a record under that
key, the receipt is stored from the provider's answer, the journal is told
the operation happened (with the receipt), and a retry replays it instead
of sending. If the provider has nothing, nothing is decided for the
person: the publication is marked ``not_found`` and the person resolves
the operation as not-happened, knowing the resend carries the same key so
a provider that honours it still cannot post twice.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from lab.connectors import Connector
from lab.egress import EgressDenied, EgressGateway
from lab.journal import OperationJournal
from lab.vault import Redactor, SecretUnavailable, Vault


class PublishError(ValueError):
    """A publication could not be looked up or reconciled."""


@dataclass(frozen=True)
class Reconciliation:
    outcome: str                  # confirmed | not_found | cannot
    detail: str
    provider_id: str | None = None
    operation: str | None = None


def find(conn: sqlite3.Connection, key_prefix: str) -> sqlite3.Row:
    if len(key_prefix) < 6:
        raise PublishError("give at least 6 characters of the idempotency key")
    rows = conn.execute(
        "SELECT * FROM publications WHERE substr(idempotency_key, 1, ?) = ?",
        (len(key_prefix), key_prefix)).fetchall()
    if not rows:
        raise PublishError(f"no publication matching {key_prefix!r}")
    if len(rows) > 1:
        raise PublishError(f"{len(rows)} publications match {key_prefix!r}; be more specific")
    return rows[0]  # type: ignore[no-any-return]


def reconcile(conn: sqlite3.Connection, key_prefix: str, *, gateway: EgressGateway,
              vault: Vault, connectors: dict[str, Connector], journal: OperationJournal,
              decided_by: str = "reconcile") -> Reconciliation:
    pub = find(conn, key_prefix)
    if pub["state"] == "confirmed":
        return Reconciliation("confirmed", "already confirmed", pub["provider_id"])
    connector = connectors.get(pub["connector"])
    if connector is None or not connector.lookup_path:
        return Reconciliation(
            "cannot", f"connector {pub['connector']!r} has no lookup, so this cannot be "
            "reconciled with the provider; check by hand and use `lab resolve`")
    try:
        secret = vault.resolve(connector.secret)
    except SecretUnavailable as exc:
        return Reconciliation("cannot", str(exc))
    redactor = Redactor([secret])
    path = connector.lookup_path.replace("{key}", pub["idempotency_key"])
    try:
        found = gateway.fetch(
            f"https://{connector.host}{path}", frozenset({connector.host}), pub["task_id"],
            method="GET", headers={connector.header: connector.header_value(secret)},
            follow_redirects=False)
    except EgressDenied as exc:
        return Reconciliation("cannot", str(redactor.scrub(f"lookup refused: {exc}")))
    if found.status == 404:
        conn.execute("UPDATE publications SET state = 'not_found', "
                     "updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now') WHERE id = ?",
                     (pub["id"],))
        return Reconciliation(
            "not_found", "the provider has no record under this key; if you are sure the send "
            "did not happen, resolve the operation as not-happened (a resend carries the same "
            "key)")
    provider_id = None
    if 200 <= found.status < 300 and not found.evidence.truncated and connector.receipt_field:
        try:
            value: Any = json.loads(found.evidence.excerpt).get(connector.receipt_field)
        except (ValueError, AttributeError):
            value = None
        if isinstance(value, str | int) and not isinstance(value, bool):
            provider_id = str(value)[:200]
    if not 200 <= found.status < 300 or provider_id is None:
        return Reconciliation(
            "cannot", f"the provider answered {found.status} without a usable record; "
            "check by hand and use `lab resolve`")

    from lab.policy import PolicyEngine  # local: policy imports this package's peers
    PolicyEngine(conn).confirm_publication(
        pub["idempotency_key"], found.status, provider_id, found.evidence.sha256,
        "reconciliation")
    op = conn.execute(
        "SELECT id FROM operations WHERE task_id = ? AND tool = 'connector.call' "
        "AND params_sha256 = ? AND state IN ('executing', 'uncertain')",
        (pub["task_id"], pub["params_sha256"])).fetchone()
    operation = None
    if op is not None:
        journal.resolve(op["id"], happened=True, decided_by=decided_by, result={
            "detail": {"provider_id": provider_id, "idempotency_key": pub["idempotency_key"],
                       "confirmed_via": "reconciliation"}})
        operation = op["id"]
    return Reconciliation("confirmed", "found on the provider's side; receipt stored",
                          provider_id, operation)
