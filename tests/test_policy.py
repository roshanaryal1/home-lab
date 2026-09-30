"""Tests for the authorization gate. Issue #9.

These are deliberately weighted toward failure cases. A policy engine
that only proves the happy path proves nothing: the whole value is in
what it refuses.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from lab.policy import Decision, PolicyEngine, Tier, action_hash
from lab.queue import TaskQueue
from lab.supervisor import Supervisor, SupervisorConfig


@pytest.fixture()
def q(tmp_path: Path) -> TaskQueue:
    with TaskQueue(tmp_path / "lab.db", owner="test") as queue:
        yield queue


@pytest.fixture()
def policy(q: TaskQueue) -> PolicyEngine:
    return PolicyEngine(q._conn)


# ------------------------------------------------------- tiers that pass


def test_autonomous_tier_proceeds(q, policy) -> None:
    task = q.get(q.add_task("read a file", capability_tier="autonomous"))
    assert policy.authorize(task).allowed


def test_notify_tier_proceeds(q, policy) -> None:
    task = q.get(q.add_task("write a draft", capability_tier="notify"))
    assert policy.authorize(task).allowed


# ------------------------------------------------------ tiers that don't


def test_never_tier_is_refused_unconditionally(q, policy) -> None:
    task = q.get(q.add_task("read the keychain", capability_tier="never"))
    result = policy.authorize(task)
    assert result.decision is Decision.DENY
    assert result.tier is Tier.NEVER


def test_never_tier_cannot_be_rescued_by_an_approval(q, policy) -> None:
    """'never' means never. An approval must not unlock it."""
    task = q.get(q.add_task("escalate privileges", capability_tier="never"))
    approval = policy.request_approval(task, "user really wants this")
    policy.grant(approval, decided_by="roshan")

    assert policy.authorize(task).decision is Decision.DENY


def test_database_refuses_to_store_an_unknown_tier(q) -> None:
    """First line of defence: an invalid tier cannot be written at all."""
    task_id = q.add_task("suspicious", capability_tier="autonomous")
    with pytest.raises(sqlite3.IntegrityError):
        q._conn.execute(
            "UPDATE tasks SET capability_tier = 'superuser' WHERE id = ?",
            (task_id,),
        )


def test_unknown_tier_fails_closed_in_the_engine(q, policy) -> None:
    """Second line of defence, in case a Task arrives from elsewhere.

    The schema CHECK already prevents storing a bad tier, so this can
    only happen via a bug or a task built outside the database. Either
    way the engine must deny rather than default to permissive.
    """
    real = q.get(q.add_task("suspicious"))
    tampered = replace(real, capability_tier="superuser")
    assert policy.authorize(tampered).decision is Decision.DENY


# ---------------------------------------------------- the approve tier


def test_approve_tier_blocks_without_an_approval(q, policy) -> None:
    task = q.get(q.add_task("send the email", capability_tier="approve"))
    result = policy.authorize(task)
    assert result.decision is Decision.NEEDS_APPROVAL
    assert not result.allowed


def test_approve_tier_proceeds_with_a_valid_approval(q, policy) -> None:
    task = q.get(q.add_task("send the email", capability_tier="approve"))
    approval = policy.request_approval(task, "sends real email")
    policy.grant(approval, decided_by="roshan")

    assert policy.authorize(task).allowed


def test_a_pending_approval_is_not_enough(q, policy) -> None:
    """Requesting is not granting."""
    task = q.get(q.add_task("send the email", capability_tier="approve"))
    policy.request_approval(task, "sends real email")

    assert policy.authorize(task).decision is Decision.NEEDS_APPROVAL


def test_a_denied_approval_blocks(q, policy) -> None:
    task = q.get(q.add_task("send the email", capability_tier="approve"))
    approval = policy.request_approval(task, "sends real email")
    policy.deny(approval, decided_by="roshan")

    assert policy.authorize(task).decision is Decision.NEEDS_APPROVAL


# ------------------------------------------------ a decision is final


def _approval_row(q, approval):
    return tuple(q._conn.execute(
        "SELECT state, decided_by, expires_at, signature FROM approvals WHERE id = ?",
        (approval,)).fetchone())


def _grant_events(q) -> int:
    return q._conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind = 'approval_granted'").fetchone()[0]


def test_granting_an_unknown_approval_does_nothing(q, policy) -> None:
    assert policy.grant("no-such-approval", decided_by="roshan") is None
    assert _grant_events(q) == 0


def test_a_denied_approval_cannot_be_granted_later(q, policy) -> None:
    task = q.get(q.add_task("send the email", capability_tier="approve"))
    approval = policy.request_approval(task, "sends real email")
    policy.deny(approval, decided_by="roshan")
    denied = _approval_row(q, approval)
    task_state = q.get(task.id).state

    assert policy.grant(approval, decided_by="someone-else") is None
    assert _approval_row(q, approval) == denied and denied[0] == "denied"
    assert q.get(task.id).state == task_state, "the cancelled task must stay cancelled"
    assert policy.authorize(task).decision is Decision.NEEDS_APPROVAL
    assert _grant_events(q) == 0


def test_a_second_grant_cannot_extend_the_window_or_change_who_decided(q, policy) -> None:
    task = q.get(q.add_task("send the email", capability_tier="approve"))
    approval = policy.request_approval(task, "sends real email")
    policy.grant(approval, decided_by="roshan", valid_for=timedelta(minutes=1))
    first = _approval_row(q, approval)

    assert policy.grant(approval, decided_by="mallory", valid_for=timedelta(days=30)) is None
    assert _approval_row(q, approval) == first
    assert _grant_events(q) == 1, "the second attempt must leave no second grant on record"


def test_an_unsigned_grant_cannot_strip_or_replace_a_signature(q, policy, tmp_path) -> None:
    from lab import operator as op
    private_path, _ = op.generate(tmp_path / "keys")
    task = q.get(q.add_task("send the email", capability_tier="approve"))
    approval = policy.request_approval(task, "sends real email")
    policy.grant(approval, decided_by="roshan", signer=op.load_private(private_path))
    signed = _approval_row(q, approval)
    assert signed[3], "the first grant carries a signature"

    assert policy.grant(approval, decided_by="roshan") is None
    assert _approval_row(q, approval) == signed


# ------------------------------------------------ replay and expiry


def test_an_approval_cannot_be_replayed(q, policy) -> None:
    """Single use. This is the one that stops a token being reused."""
    task = q.get(q.add_task("send the email", capability_tier="approve"))
    approval = policy.request_approval(task, "sends real email")
    policy.grant(approval, decided_by="roshan")

    assert policy.authorize(task).allowed, "first use should succeed"
    assert policy.authorize(task).decision is Decision.NEEDS_APPROVAL, (
        "second use of the same approval must fail"
    )


def test_an_expired_approval_blocks(q, policy) -> None:
    task = q.get(q.add_task("send the email", capability_tier="approve"))
    approval = policy.request_approval(task, "sends real email")
    policy.grant(approval, decided_by="roshan",
                 valid_for=timedelta(seconds=-1))

    assert policy.authorize(task).decision is Decision.NEEDS_APPROVAL


# --------------------------------------------- parameter binding


def test_an_approval_does_not_authorise_different_parameters(q, policy) -> None:
    """Approving 'email alice' must not permit 'email bob'.

    This is the difference between approving an action and granting a
    capability, and it is the property that makes the gate meaningful.
    """
    alice = q.get(q.add_task("send email", capability_tier="approve",
                             payload={"to": "alice@example.com"}))
    approval = policy.request_approval(alice, "emails alice")
    policy.grant(approval, decided_by="roshan")

    # Same task id, tampered payload.
    q._conn.execute(
        "UPDATE tasks SET payload = ? WHERE id = ?",
        ('{"to": "bob@example.com"}', alice.id),
    )
    bob = q.get(alice.id)

    assert action_hash(bob) != action_hash(alice)
    assert policy.authorize(bob).decision is Decision.NEEDS_APPROVAL


def test_an_approval_for_one_task_does_not_cover_another(q, policy) -> None:
    first = q.get(q.add_task("send email", capability_tier="approve",
                             payload={"to": "alice@example.com"}))
    second = q.get(q.add_task("send email", capability_tier="approve",
                              payload={"to": "alice@example.com"}))
    approval = policy.request_approval(first, "emails alice")
    policy.grant(approval, decided_by="roshan")

    assert policy.authorize(second).decision is Decision.NEEDS_APPROVAL


def test_action_hash_is_stable_across_key_order(q) -> None:
    a = q.get(q.add_task("t", payload={"x": 1, "y": 2}))
    q._conn.execute("UPDATE tasks SET payload = ? WHERE id = ?",
                    ('{"y": 2, "x": 1}', a.id))
    assert action_hash(q.get(a.id)) == action_hash(a)


# ------------------------------------------------------------- audit


def test_every_decision_is_audited(q, policy) -> None:
    allowed = q.get(q.add_task("safe", capability_tier="autonomous"))
    denied = q.get(q.add_task("forbidden", capability_tier="never"))
    policy.authorize(allowed)
    policy.authorize(denied)

    kinds = [r["kind"] for r in q.events(allowed.id)]
    assert "policy_allow" in kinds, "allows must be auditable too"
    assert "policy_deny" in [r["kind"] for r in q.events(denied.id)]


# ------------------------------------------- enforcement in the supervisor


@pytest.mark.asyncio
async def test_supervisor_runs_an_autonomous_task(tmp_path: Path) -> None:
    sup = Supervisor(SupervisorConfig(db_path=tmp_path / "s.db",
                                      idle_poll_seconds=0.01))
    ran = []

    async def handler(task, tools):
        ran.append(task.id)
        return {}

    sup.register("demo", handler)
    sup.queue.add_task("safe work", agent_kind="demo",
                       capability_tier="autonomous")
    await sup.run(max_tasks=1)
    assert len(ran) == 1
    sup.close()


@pytest.mark.asyncio
async def test_supervisor_refuses_a_never_task(tmp_path: Path) -> None:
    """The handler must never be reached."""
    sup = Supervisor(SupervisorConfig(db_path=tmp_path / "s.db",
                                      idle_poll_seconds=0.01))
    ran = []

    async def handler(task, tools):
        ran.append(task.id)
        return {}

    sup.register("demo", handler)
    task_id = sup.queue.add_task("delete everything", agent_kind="demo",
                                 capability_tier="never")
    stats = await sup.run(max_tasks=1)

    assert ran == [], "a denied task must not reach its handler"
    assert stats.denied == 1
    assert sup.queue.get(task_id).state == "cancelled"
    sup.close()


@pytest.mark.asyncio
async def test_supervisor_parks_a_task_needing_approval(tmp_path: Path) -> None:
    sup = Supervisor(SupervisorConfig(db_path=tmp_path / "s.db",
                                      idle_poll_seconds=0.01))
    ran = []

    async def handler(task, tools):
        ran.append(task.id)
        return {}

    sup.register("demo", handler)
    sup.queue.add_task("send invoice", agent_kind="demo",
                       capability_tier="approve")
    stats = await sup.run(max_tasks=1)

    assert ran == [], "an unapproved task must not reach its handler"
    assert stats.awaiting_approval == 1
    assert len(sup.policy.pending()) == 1, "a request should be waiting"
    sup.close()
