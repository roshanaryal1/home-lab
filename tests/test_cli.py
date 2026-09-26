"""Tests for the approval CLI and the park/approve/run round trip.

Issues #18 and #19. The round-trip test is the important one: before
#19, granting an approval updated a row and the task stayed dead, while
everything reported success.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lab.cli import _redact, main
from lab.policy import PolicyEngine
from lab.queue import TaskQueue
from lab.supervisor import Supervisor, SupervisorConfig


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    path = tmp_path / "lab.db"
    TaskQueue(path, owner="setup").close()
    return path


def run(db: Path, *argv: str) -> int:
    return main(["--db", str(db), *argv])


# ------------------------------------------------- park, approve, run


@pytest.mark.asyncio
async def test_approved_task_actually_runs(tmp_path: Path) -> None:
    """The round trip that was broken. Issue #19.

    Before the fix the supervisor cancelled a task needing approval.
    Cancelled is terminal, so granting the approval did nothing at all
    while still reporting success.
    """
    path = tmp_path / "lab.db"
    config = SupervisorConfig(db_path=path, idle_poll_seconds=0.01,
                              owner="sup")
    ran: list[str] = []

    async def handler(task):
        ran.append(task.id)
        return {"sent": True}

    # First pass: the task parks, nothing runs.
    sup = Supervisor(config)
    sup.register("mail", handler)
    task_id = sup.queue.add_task("send invoice", agent_kind="mail",
                                 capability_tier="approve",
                                 payload={"to": "alice@example.com"})
    await sup.run(max_tasks=1)
    assert ran == [], "unapproved work must not run"
    assert sup.queue.get(task_id).state == "awaiting_approval"

    # A human approves.
    pending = sup.policy.pending()
    assert len(pending) == 1
    released = sup.policy.grant(pending[0]["id"], decided_by="roshan")
    assert released == task_id
    assert sup.queue.get(task_id).state == "queued", "must return to the queue"
    sup.close()

    # Second pass: it runs.
    sup2 = Supervisor(config)
    sup2.register("mail", handler)
    await sup2.run(max_tasks=1)
    assert ran == [task_id], "approved work must actually run"
    assert sup2.queue.get(task_id).state == "succeeded"
    sup2.close()


@pytest.mark.asyncio
async def test_denied_task_is_cancelled_and_never_runs(tmp_path: Path) -> None:
    path = tmp_path / "lab.db"
    config = SupervisorConfig(db_path=path, idle_poll_seconds=0.01,
                              owner="sup")
    ran: list[str] = []

    async def handler(task):
        ran.append(task.id)
        return {}

    sup = Supervisor(config)
    sup.register("mail", handler)
    task_id = sup.queue.add_task("send invoice", agent_kind="mail",
                                 capability_tier="approve")
    await sup.run(max_tasks=1)

    sup.policy.deny(sup.policy.pending()[0]["id"], decided_by="roshan",
                    reason="wrong recipient")
    assert sup.queue.get(task_id).state == "cancelled"
    sup.close()

    sup2 = Supervisor(config)
    sup2.register("mail", handler)
    await sup2.run(max_tasks=1)
    assert ran == [], "a denied task must never run"
    sup2.close()


def test_parking_is_not_terminal(db: Path) -> None:
    """The specific defect: awaiting_approval must have a way out."""
    from lab.queue import LEGAL_TRANSITIONS
    assert LEGAL_TRANSITIONS["awaiting_approval"], (
        "a parked task with no outgoing transition can never run"
    )
    assert "queued" in LEGAL_TRANSITIONS["awaiting_approval"]


# ------------------------------------------------------------- output


def test_approvals_lists_pending(db, capsys) -> None:
    with TaskQueue(db, owner="t") as q:
        policy = PolicyEngine(q._conn)
        task = q.get(q.add_task("send invoice", capability_tier="approve"))
        policy.request_approval(task, "sends real email")

    assert run(db, "approvals") == 0
    out = capsys.readouterr().out
    assert "1 waiting" in out
    assert "send invoice" in out
    assert "sends real email" in out


def test_approvals_when_empty(db, capsys) -> None:
    assert run(db, "approvals") == 0
    assert "Nothing waiting" in capsys.readouterr().out


def test_show_renders_the_exact_parameters(db, capsys) -> None:
    """Approving without seeing the parameters is rubber-stamping."""
    with TaskQueue(db, owner="t") as q:
        policy = PolicyEngine(q._conn)
        task = q.get(q.add_task("send invoice", capability_tier="approve",
                                payload={"to": "alice@example.com",
                                         "amount": 4200}))
        approval = policy.request_approval(task, "sends real email")

    assert run(db, "show", approval[:8]) == 0
    out = capsys.readouterr().out
    assert "alice@example.com" in out
    assert "4200" in out
    assert "Changing any parameter invalidates this approval" in out


def test_show_redacts_credentials(db, capsys) -> None:
    """Approving an action must not become a way to read a secret."""
    with TaskQueue(db, owner="t") as q:
        policy = PolicyEngine(q._conn)
        task = q.get(q.add_task("call api", capability_tier="approve",
                                payload={"url": "https://example.com",
                                         "api_key": "sk-live-do-not-print",
                                         "nested": {"password": "hunter2"}}))
        approval = policy.request_approval(task, "calls an API")

    run(db, "show", approval[:8])
    out = capsys.readouterr().out
    assert "sk-live-do-not-print" not in out
    assert "hunter2" not in out
    assert "<redacted>" in out
    assert "https://example.com" in out, "non-secrets must still be visible"


def test_redact_masks_by_key_name() -> None:
    masked = _redact({"token": "abc", "url": "u", "inner": {"secret": "s"}})
    assert masked["token"] == "<redacted>"
    assert masked["url"] == "u"
    assert masked["inner"]["secret"] == "<redacted>"


# ------------------------------------------------------------ decisions


def test_approve_records_who_decided(db, capsys) -> None:
    with TaskQueue(db, owner="t") as q:
        policy = PolicyEngine(q._conn)
        task = q.get(q.add_task("send", capability_tier="approve"))
        approval = policy.request_approval(task, "reason")

    assert run(db, "approve", approval[:8], "--by", "roshan") == 0
    with TaskQueue(db, owner="t") as q:
        row = q._conn.execute(
            "SELECT state, decided_by FROM approvals WHERE id = ?", (approval,)
        ).fetchone()
    assert row["state"] == "granted"
    assert row["decided_by"] == "roshan"


def test_deny_records_a_reason(db) -> None:
    with TaskQueue(db, owner="t") as q:
        policy = PolicyEngine(q._conn)
        task = q.get(q.add_task("send", capability_tier="approve"))
        approval = policy.request_approval(task, "reason")

    assert run(db, "deny", approval[:8], "--by", "roshan",
               "--reason", "wrong recipient") == 0
    with TaskQueue(db, owner="t") as q:
        row = q._conn.execute(
            "SELECT state FROM approvals WHERE id = ?", (approval,)
        ).fetchone()
    assert row["state"] == "denied"


def test_approving_twice_fails_clearly(db, capsys) -> None:
    with TaskQueue(db, owner="t") as q:
        policy = PolicyEngine(q._conn)
        task = q.get(q.add_task("send", capability_tier="approve"))
        approval = policy.request_approval(task, "reason")

    run(db, "approve", approval[:8], "--by", "roshan")
    capsys.readouterr()
    assert run(db, "approve", approval[:8], "--by", "roshan") == 1
    assert "already granted" in capsys.readouterr().err


def test_unknown_id_fails_clearly(db, capsys) -> None:
    assert run(db, "show", "doesnotexist") == 1
    assert "No approval matching" in capsys.readouterr().err


def test_missing_database_fails_clearly(tmp_path, capsys) -> None:
    assert main(["--db", str(tmp_path / "nope.db"), "approvals"]) == 1
    assert "No database" in capsys.readouterr().err


def test_tasks_reports_counts(db, capsys) -> None:
    with TaskQueue(db, owner="t") as q:
        q.add_task("one")
        q.add_task("two")
    assert run(db, "tasks") == 0
    assert "queued" in capsys.readouterr().out
