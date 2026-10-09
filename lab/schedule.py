"""Owner-signed schedules (#361, feature 5).

A schedule starts a task on a calendar rule, so the lab can work on standing
goals while the owner is away. It may start work. It may never approve work:
the task it creates carries the schedule's capability tier like any other
task, and an approve-tier task still waits for the owner's signature
(``lab.policy``). That is the claim the owner registers before this ships:

    S1: an unsigned, altered or replayed schedule never creates a task, and a
    task a schedule creates never reaches an approve-tier tool without the
    owner's signature.

* The owner signs the whole spec (``operator.sign_action``, purpose
  ``schedule``). With an operator public key configured, ``fire_due`` checks
  the signature before every firing. A row the agent wrote, or one edited
  after signing, is refused and logged.
* Each spec carries a random nonce, unique in the table. A removed schedule
  keeps its row, so a signed spec cannot be inserted again after removal.
* Rules: ``daily HH:MM``, ``weekly DAY HH:MM`` and ``every N minutes`` with N
  from 15 to 1440. Times are in the schedule's IANA time zone, so a daily
  07:30 stays 07:30 across daylight saving.
* Missed slots (the Mac was off) fire once, not once per slot. A slot whose
  previous task is still open is skipped, so work does not pile up.
* A firing is at most once: the slot is advanced before the task is created,
  so a crash between the two loses that run rather than doubling it.

Without an operator key (tests, a laptop), schedules fire unsigned, as the
control switch does, and every firing records ``signed: false``. The
signature is the boundary, as it is for approvals.
"""

from __future__ import annotations

import contextlib
import json
import re
import secrets
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from lab.audit import append_event
from lab.operator import sign_action, verify_action
from lab.origin import Origin, SourceType, content_sha256
from lab.queue import TaskQueue

PURPOSE = "schedule"
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
KIND = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MIN_EVERY_MINUTES = 15
MAX_EVERY_MINUTES = 1440
TIERS = ("autonomous", "notify", "approve")
WEIGHTS = ("light", "heavy")
OPEN_STATES = ("queued", "leased", "running", "awaiting_approval")
STAMP = "%Y-%m-%dT%H:%M:%SZ"

_DAILY = re.compile(r"daily (\d{2}):(\d{2})")
_WEEKLY = re.compile(r"weekly (mon|tue|wed|thu|fri|sat|sun) (\d{2}):(\d{2})")
_EVERY = re.compile(r"every (\d{1,4}) minutes")


class ScheduleError(ValueError):
    """A schedule that cannot be added, or a name that cannot be found."""


@dataclass(frozen=True)
class Rule:
    kind: str                    # "daily", "weekly" or "every"
    at: time | None = None
    weekday: int | None = None   # 0 is Monday
    minutes: int | None = None


@dataclass(frozen=True)
class Fired:
    """What ``fire_due`` did with one due schedule."""
    name: str
    outcome: str                 # "fired", "refused" or "skipped"
    task_id: str | None = None
    reason: str | None = None


@contextlib.contextmanager
def _tx(conn: sqlite3.Connection) -> Iterator[None]:
    """The row and its audit event commit together, or neither does."""
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        if own:
            conn.execute("ROLLBACK")
        raise
    if own:
        conn.execute("COMMIT")


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime(STAMP)


def _parse_stamp(text: str) -> datetime:
    return datetime.strptime(text, STAMP).replace(tzinfo=UTC)


def parse_rule(text: str) -> Rule:
    text = text.strip().lower()
    if m := _DAILY.fullmatch(text):
        return Rule("daily", at=_clock(m.group(1), m.group(2)))
    if m := _WEEKLY.fullmatch(text):
        return Rule("weekly", at=_clock(m.group(2), m.group(3)),
                    weekday=DAYS.index(m.group(1)))
    if m := _EVERY.fullmatch(text):
        minutes = int(m.group(1))
        if not MIN_EVERY_MINUTES <= minutes <= MAX_EVERY_MINUTES:
            raise ScheduleError(f"every N minutes needs N from {MIN_EVERY_MINUTES} to "
                                f"{MAX_EVERY_MINUTES}, not {minutes}")
        return Rule("every", minutes=minutes)
    raise ScheduleError(f"unknown rule {text!r}: use 'daily HH:MM', 'weekly DAY HH:MM' "
                        "or 'every N minutes'")


def _clock(hours: str, minutes: str) -> time:
    h, m = int(hours), int(minutes)
    if h > 23 or m > 59:
        raise ScheduleError(f"{hours}:{minutes} is not a time of day")
    return time(h, m)


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ScheduleError(f"unknown time zone {name!r}") from None


def next_slot(rule: Rule, tz: ZoneInfo, after: datetime, *, anchor: datetime) -> datetime:
    """The first slot strictly after ``after``, in UTC.

    ``anchor`` is when the schedule was created. An ``every`` rule counts its
    slots from it. Daily and weekly slots are wall-clock times in ``tz``.
    """
    if rule.kind == "every":
        assert rule.minutes is not None
        step = timedelta(minutes=rule.minutes)
        if after < anchor:
            return anchor + step
        return anchor + step * ((after - anchor) // step + 1)
    assert rule.at is not None
    first: date = after.astimezone(tz).date()
    for offset in range(9):
        day = first + timedelta(days=offset)
        if rule.kind == "weekly" and day.weekday() != rule.weekday:
            continue
        candidate = datetime.combine(day, rule.at, tzinfo=tz).astimezone(UTC)
        if candidate > after:
            return candidate
    raise AssertionError("no slot in nine days")    # pragma: no cover


def _signed_fields(row: sqlite3.Row | dict[str, Any]) -> dict[str, object]:
    return {key: row[key] for key in (
        "name", "rule", "tz", "agent_kind", "title", "payload", "weight", "capability_tier",
        "created_by", "created_at", "nonce")}


def add(conn: sqlite3.Connection, name: str, rule: str, *, kind: str, title: str,
        payload: dict[str, Any] | None = None, tz: str = "UTC", weight: str = "light",
        tier: str = "autonomous", by: str, signer: Ed25519PrivateKey | None,
        now: datetime | None = None) -> int:
    """Insert a signed schedule. Returns its id."""
    if not NAME.fullmatch(name):
        raise ScheduleError("a schedule name is 1 to 64 lowercase letters, digits, "
                            "'-' or '_', starting with a letter or digit")
    parsed = parse_rule(rule)
    zone(tz)
    if not KIND.fullmatch(kind):
        raise ScheduleError("the task kind is 1 to 64 lowercase letters, digits, '.', '-' or "
                            "'_', such as git.read")
    if tier not in TIERS:
        raise ScheduleError(f"tier must be one of {', '.join(TIERS)}")
    if weight not in WEIGHTS:
        raise ScheduleError(f"weight must be one of {', '.join(WEIGHTS)}")
    if not by:
        raise ScheduleError("--by names who adds the schedule")
    created = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    spec: dict[str, Any] = {
        "name": name, "rule": rule.strip().lower(), "tz": tz, "agent_kind": kind,
        "title": title,
        "payload": json.dumps(payload or {}, sort_keys=True, separators=(",", ":")),
        "weight": weight, "capability_tier": tier, "created_by": by,
        "created_at": stamp(created), "nonce": secrets.token_hex(16),
    }
    signature = sign_action(signer, PURPOSE, **spec) if signer is not None else None
    due = next_slot(parsed, zone(tz), created, anchor=created)
    try:
        with _tx(conn):
            cur = conn.execute(
                "INSERT INTO schedules (name, rule, tz, agent_kind, title, payload, weight, "
                "capability_tier, created_by, created_at, nonce, signature, next_due_at) "
                "VALUES (:name, :rule, :tz, :agent_kind, :title, :payload, :weight, "
                ":capability_tier, :created_by, :created_at, :nonce, :signature, :next_due_at)",
                {**spec, "signature": signature, "next_due_at": stamp(due)})
            append_event(conn, None, "schedule_added", detail={
                "schedule": name, "rule": spec["rule"], "tz": tz, "kind": kind, "tier": tier,
                "by": by,
                "signed": signature is not None, "next_due_at": stamp(due)})
    except sqlite3.IntegrityError as exc:
        if "schedules.name" in str(exc):
            raise ScheduleError(f"a schedule named {name!r} already exists") from None
        raise
    return int(cur.lastrowid or 0)


def remove(conn: sqlite3.Connection, name: str, *, by: str,
           now: datetime | None = None) -> None:
    """Take a schedule out of service. Needs no signature: it only removes authority."""
    when = stamp(now or datetime.now(UTC))
    with _tx(conn):
        cur = conn.execute(
            "UPDATE schedules SET removed_at = ?, removed_by = ? "
            "WHERE name = ? AND removed_at IS NULL", (when, by, name))
        if cur.rowcount == 0:
            raise ScheduleError(f"no live schedule named {name!r}")
        append_event(conn, None, "schedule_removed", detail={"schedule": name, "by": by})


def _rows(conn: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    return list(cur.execute(sql, params))


def live(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return _rows(conn, "SELECT * FROM schedules WHERE removed_at IS NULL ORDER BY name")


def verified(row: sqlite3.Row, public_key: Ed25519PublicKey | None) -> bool:
    """True when the row is signed by the operator, or when no key is configured."""
    if public_key is None:
        return True
    return verify_action(public_key, row["signature"], PURPOSE, **_signed_fields(row))


def _advance(conn: sqlite3.Connection, row: sqlite3.Row, now: datetime) -> bool:
    """Move the schedule to its first slot after ``now``. False if another process
    already moved it, so this slot is theirs."""
    due = next_slot(parse_rule(row["rule"]), zone(row["tz"]), now,
                    anchor=_parse_stamp(row["created_at"]))
    cur = conn.execute(
        "UPDATE schedules SET next_due_at = ? WHERE id = ? AND next_due_at = ? "
        "AND removed_at IS NULL", (stamp(due), row["id"], row["next_due_at"]))
    return cur.rowcount == 1


def fire_due(queue: TaskQueue, now: datetime, public_key: Ed25519PublicKey | None) -> list[Fired]:
    """Create one task for every schedule that is due at ``now``. See the module notes."""
    conn = queue._conn
    due = _rows(conn, "SELECT * FROM schedules WHERE removed_at IS NULL AND next_due_at <= ? "
                "ORDER BY next_due_at, id", (stamp(now),))
    results: list[Fired] = []
    for row in due:
        name = row["name"]
        if not verified(row, public_key):
            if _advance(conn, row, now):
                append_event(conn, None, "schedule_refused", detail={
                    "schedule": name, "reason": "the operator's signature does not verify"})
                results.append(Fired(name, "refused", reason="signature"))
            continue
        last = row["last_task_id"]
        if last is not None:
            marks = ",".join("?" * len(OPEN_STATES))
            open_task = conn.execute(
                f"SELECT 1 FROM tasks WHERE id = ? AND state IN ({marks})",  # nosemgrep
                (last, *OPEN_STATES)).fetchone()
            if open_task is not None:
                if _advance(conn, row, now):
                    append_event(conn, last, "schedule_skipped", detail={
                        "schedule": name, "reason": "the last task is still open"})
                    results.append(Fired(name, "skipped", task_id=last, reason="open"))
                continue
        if not _advance(conn, row, now):
            continue
        spec_sha = content_sha256(json.dumps(_signed_fields(row), sort_keys=True))
        task_id = queue.add_task(
            row["title"], json.loads(row["payload"]), agent_kind=row["agent_kind"],
            weight=row["weight"],
            capability_tier=row["capability_tier"],
            origin=Origin(SourceType.OPERATOR, source_id=f"schedule:{name}", sha256=spec_sha,
                          delegated_by=row["created_by"]))
        conn.execute("UPDATE schedules SET last_fired_at = ?, last_task_id = ? WHERE id = ?",
                     (stamp(now), task_id, row["id"]))
        append_event(conn, task_id, "schedule_fired", detail={
            "schedule": name, "signed": public_key is not None})
        results.append(Fired(name, "fired", task_id=task_id))
    return results
