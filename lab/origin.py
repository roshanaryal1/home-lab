"""Where a task's input came from, and the ceiling that puts on it (item 4.2, #69).

Every task records its source type, source id and content hash, when it
was acquired, its sensitivity, who delegated it, and its parent. Two
facts are derived and cannot be set by the caller:

* ``tainted``: the task's input is not the operator's own. Only a task
  whose source is ``operator`` is untainted, and a child of a tainted
  task is tainted whatever it claims, so lineage can only ever lose
  trust, never gain it. A task with no known origin is tainted.
* ``sensitivity``: the higher of what the caller asks for and what the
  parent has, so a summary of a secret is at least as sensitive as the
  secret.

Untrusted text may supply evidence. It may never supply a grant, a
destination or a policy: a tainted task's payload is refused if it
carries any of ``RESERVED_PAYLOAD_KEYS``, and tools, destinations and
policy are read from trusted registration, never from a payload
(``lab.authority``, ``lab.supervisor``).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class SourceType(StrEnum):
    OPERATOR = "operator"
    EVENT = "event"          # a GitHub event, a log line: written by someone else
    WEB = "web"
    DOCUMENT = "document"
    TASK = "task"            # produced by another task; inherits its trust
    MEMORY = "memory"
    MODEL = "model"
    UNKNOWN = "unknown"


TRUSTED_SOURCES = frozenset({SourceType.OPERATOR})

SENSITIVITY_ORDER = ("public", "internal", "secret")

# Keys that name authority. A payload that came from untrusted input may
# not carry them: even though nothing reads them today, a future reader
# must not be able to mistake text for a grant.
RESERVED_PAYLOAD_KEYS = frozenset({
    "origin", "tools", "grants", "grant", "destination", "destinations",
    "policy", "capability_tier", "approval", "approvals", "credentials",
})


class UntrustedAuthority(ValueError):
    """Untrusted input tried to carry a grant, destination or policy."""


@dataclass(frozen=True)
class Origin:
    """What the caller says about where the input came from."""

    source_type: str = SourceType.UNKNOWN
    source_id: str | None = None
    sha256: str | None = None
    acquired_at: str | None = None
    sensitivity: str = "internal"
    delegated_by: str | None = None


@dataclass(frozen=True)
class ResolvedOrigin:
    source_type: str
    source_id: str | None
    sha256: str | None
    acquired_at: str
    sensitivity: str
    delegated_by: str | None
    tainted: bool


def content_sha256(text: str | bytes) -> str:
    data = text.encode("utf-8") if isinstance(text, str) else text
    return hashlib.sha256(data).hexdigest()


def _rank(sensitivity: str) -> int:
    try:
        return SENSITIVITY_ORDER.index(sensitivity)
    except ValueError:
        raise ValueError(f"unknown sensitivity {sensitivity!r}") from None


def resolve(requested: Origin | None, parent_id: str | None,
            parent: sqlite3.Row | None) -> ResolvedOrigin:
    """Apply the ceilings. ``parent`` is the parent's row, or None."""
    if requested is None:
        requested = Origin(SourceType.TASK, parent_id) if parent else Origin()
    try:
        source = SourceType(requested.source_type)
    except ValueError:
        raise ValueError(f"unknown source type {requested.source_type!r}") from None

    parent_tainted = bool(parent["tainted"]) if parent is not None else False
    if source is SourceType.TASK:
        if parent is None:
            raise ValueError("a task-sourced task needs a parent")
        tainted = parent_tainted
        source_id = requested.source_id or parent_id
        sha = requested.sha256
        if sha is None and parent["result"]:
            sha = content_sha256(parent["result"])
    else:
        tainted = source not in TRUSTED_SOURCES or parent_tainted
        source_id, sha = requested.source_id, requested.sha256

    sensitivity = requested.sensitivity
    if parent is not None and _rank(parent["sensitivity"]) > _rank(sensitivity):
        sensitivity = parent["sensitivity"]
    _rank(sensitivity)
    return ResolvedOrigin(
        source_type=source.value, source_id=source_id, sha256=sha,
        acquired_at=requested.acquired_at or datetime.now(UTC).isoformat(timespec="seconds"),
        sensitivity=sensitivity, delegated_by=requested.delegated_by, tainted=tainted,
    )


def check_payload(payload: dict[str, Any], tainted: bool) -> None:
    if not tainted:
        return
    found = sorted(RESERVED_PAYLOAD_KEYS & set(payload))
    if found:
        raise UntrustedAuthority(
            f"untrusted input cannot carry {', '.join(found)} in a task payload")


def provenance(row: sqlite3.Row) -> dict[str, Any]:
    """What travels with a task's result so downstream code can see it."""
    return {
        "task_id": row["id"],
        "origin_type": row["origin_type"],
        "origin_id": row["origin_id"],
        "origin_sha256": row["origin_sha256"],
        "tainted": bool(row["tainted"]),
        "sensitivity": row["sensitivity"],
    }


def dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True)
