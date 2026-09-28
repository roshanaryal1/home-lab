"""The Rule of Two, enforced (item 4.1, #68; ADR 0006).

A session may hold at most two of three properties:

* ``untrusted_input``: it reads content an outsider could have written;
* ``sensitive_data``: it can see a secret or private data;
* ``external_action``: it can change something outside the lab or send
  something out.

All three together is the shape of a successful prompt injection: hostile
text steers a process that can see something valuable and has a way to
send it. So a task that would hold all three is refused before its
handler runs.

Where each property comes from, and why none of it is the task's say-so:

* ``sensitive_data`` and ``external_action`` are declared where a handler
  is registered (trusted code) and by the tools it is granted
  (``TOOL_LEGS``, a fixed table). A task cannot remove either.
* ``untrusted_input`` defaults to true. Only a task marked
  ``payload["origin"] == "operator"`` counts as trusted, which anyone who
  can enqueue can write; carrying real origin and lineage, so the mark
  can be checked, is item 4.2 (#69). Until then the rule is a floor
  against mistakes and against a handler wired with too much, not a
  defence against someone who controls the queue.

A model or classifier may suggest a route. It never widens the
intersection this module computes.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class Leg(StrEnum):
    UNTRUSTED_INPUT = "untrusted_input"
    SENSITIVE_DATA = "sensitive_data"
    EXTERNAL_ACTION = "external_action"


ALL_LEGS = frozenset(Leg)

# What each broker tool can do to the outside world or expose. The current
# tools are local: files inside a private workspace, and a sandboxed shell
# with no network. A tool that reaches the network or a secret must be
# added here with the matching leg before it can be registered; a tool
# missing from this table is treated as external, so forgetting fails closed.
TOOL_LEGS: dict[str, frozenset[Leg]] = {
    "fs.read": frozenset(),
    "fs.list": frozenset(),
    "fs.write": frozenset(),
    "fs.delete": frozenset(),
    "shell.run": frozenset(),
}


class AuthorityViolation(RuntimeError):
    """A task would hold all three properties."""


@dataclass(frozen=True)
class AgentCapability:
    """What a registered handler can reach, declared by trusted code."""

    sensitive_data: bool = False
    external_action: bool = False


def is_trusted_origin(payload: dict[str, Any]) -> bool:
    """Only an explicit operator mark counts. Anything else is untrusted."""
    return payload.get("origin") == "operator"


def held_legs(payload: dict[str, Any], tools: Iterable[str],
              capability: AgentCapability) -> frozenset[Leg]:
    legs: set[Leg] = set()
    if not is_trusted_origin(payload):
        legs.add(Leg.UNTRUSTED_INPUT)
    if capability.sensitive_data:
        legs.add(Leg.SENSITIVE_DATA)
    if capability.external_action:
        legs.add(Leg.EXTERNAL_ACTION)
    for tool in tools:
        legs |= TOOL_LEGS.get(tool, frozenset({Leg.EXTERNAL_ACTION}))
    return frozenset(legs)


def check(legs: frozenset[Leg]) -> None:
    if legs >= ALL_LEGS:
        raise AuthorityViolation(
            "would hold untrusted input, sensitive data and an external action "
            "together (Rule of Two, ADR 0006)"
        )
