"""The Rule of Two (item 4.1, #68)."""

from __future__ import annotations

from pathlib import Path

import pytest

from lab.authority import (
    ALL_LEGS,
    TOOL_LEGS,
    AgentCapability,
    AuthorityViolation,
    Leg,
    check,
    held_legs,
)
from lab.broker import TOOL_TIERS, ToolSession
from lab.origin import Origin, SourceType
from lab.queue import Task
from lab.supervisor import Supervisor, SupervisorConfig

OPERATOR = Origin(SourceType.OPERATOR)


def test_every_broker_tool_is_classified() -> None:
    """Adding a tool without saying what it can reach must fail a test,
    not silently count as harmless."""
    assert set(TOOL_LEGS) == set(TOOL_TIERS)


@pytest.mark.safety
def test_all_three_legs_is_refused() -> None:
    legs = held_legs(True, [], AgentCapability(sensitive_data=True, external_action=True))
    assert legs == ALL_LEGS
    with pytest.raises(AuthorityViolation, match="Rule of Two"):
        check(legs)


@pytest.mark.parametrize("tainted,cap", [
    (False, AgentCapability(True, True)),        # trusted input
    (True, AgentCapability(True, False)),        # no outside effect
    (True, AgentCapability(False, True)),        # no secret
    (True, AgentCapability()),                   # neither
])
def test_two_legs_are_allowed(tainted, cap) -> None:
    check(held_legs(tainted, ["fs.read"], cap))


@pytest.mark.safety
def test_an_unclassified_tool_counts_as_external() -> None:
    legs = held_legs(True, ["net.fetch"], AgentCapability(sensitive_data=True))
    assert legs == ALL_LEGS


def test_a_task_cannot_remove_what_the_handler_declares() -> None:
    legs = held_legs(False, [], AgentCapability(sensitive_data=True, external_action=True))
    assert legs == {Leg.SENSITIVE_DATA, Leg.EXTERNAL_ACTION}


# ------------------------------------------------------------ supervisor


def make_supervisor(tmp_path: Path) -> Supervisor:
    return Supervisor(SupervisorConfig(db_path=tmp_path / "lab.db", idle_poll_seconds=0.01))


async def _run(sup: Supervisor, origin: Origin | None, **caps) -> tuple[str, list[str]]:
    ran: list[str] = []

    async def handler(task: Task, tools: ToolSession) -> dict:
        ran.append(task.id)
        return {}

    sup.register("demo", handler, **caps)
    task_id = sup.queue.add_task("t", agent_kind="demo", origin=origin)
    await sup.run(max_tasks=1)
    return task_id, ran


@pytest.mark.safety
@pytest.mark.asyncio
async def test_untrusted_input_a_secret_and_an_external_tool_never_runs(tmp_path: Path) -> None:
    sup = make_supervisor(tmp_path)
    task_id, ran = await _run(sup, None, sensitive_data=True, external_action=True)
    task = sup.queue.get(task_id)
    assert ran == [] and task.state == "cancelled"
    assert "Rule of Two" in (task.last_error or "")
    assert "authority_refused" in [r["kind"] for r in sup.queue.events(task_id)]
    assert sup.policy.pending() == [], "no approval is offered for this combination"
    sup.close()


@pytest.mark.asyncio
async def test_the_same_handler_runs_when_the_input_is_the_operators(tmp_path: Path) -> None:
    sup = make_supervisor(tmp_path)
    task_id, ran = await _run(sup, OPERATOR, sensitive_data=True, external_action=True)
    assert ran == [task_id] and sup.queue.get(task_id).state == "succeeded"
    sup.close()


@pytest.mark.asyncio
async def test_ordinary_local_tasks_are_unaffected(tmp_path: Path) -> None:
    sup = make_supervisor(tmp_path)
    task_id, ran = await _run(sup, None)
    assert ran == [task_id] and sup.queue.get(task_id).state == "succeeded"
    sup.close()
