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
* ``untrusted_input`` is the task's ``tainted`` flag (item 4.2, #69):
  derived from its recorded origin and its parent's, never from anything
  in the payload. Only a task whose source is the operator is untainted,
  and a child of a tainted task is tainted. Whoever can call ``add_task``
  with an operator origin still decides that, so the rule guards against
  mis-wiring and against untrusted text, not against someone who controls
  the queue's code path.

A model or classifier may suggest a route. It never widens the
intersection this module computes.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


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
    # Outbound requests are an outside effect (a URL can carry data out),
    # even though the host list bounds where. What comes back is written by
    # someone else, so a task that fetches also holds untrusted input
    # whatever its own origin: found by the injection harness (item 4.7),
    # where a trusted task that fetched a hostile page and held a
    # credentialed connector was stopped only by the approval gate.
    "net.fetch": frozenset({Leg.UNTRUSTED_INPUT, Leg.EXTERNAL_ACTION}),
    # A credentialed outside effect: sees a secret and acts externally, so
    # it may only run for trusted input (ADR 0006, the publish plane).
    "connector.call": frozenset({Leg.SENSITIVE_DATA, Leg.EXTERNAL_ACTION}),
    # Local and read-only: the task's own files, a repository inside them.
    "fs.search": frozenset(),
    "git.status": frozenset(),
    "git.log": frozenset(),
    "git.diff": frozenset(),
    # A fetch first, so the same legs as net.fetch; the model is on loopback.
    "net.summarize": frozenset({Leg.UNTRUSTED_INPUT, Leg.EXTERNAL_ACTION}),
    # Writes a pending row in the lab's own database that nothing reads as
    # memory until the owner signs it in (#253): no secret, nothing outside.
    "memory.propose": frozenset(),
    # Runs a skill's script in a container with no network (#255). Nothing
    # leaves the lab, but untrusted code writes its output, so the task holds
    # untrusted input whatever its own origin.
    "skill.run": frozenset({Leg.UNTRUSTED_INPUT}),
    # A tool of an operator-signed MCP server (#256). Its output and its own
    # description are written by a program the lab did not write, so a task
    # that calls one holds untrusted input whatever its origin. The server
    # runs with the task's workspace, a minimal environment and no network
    # unless the signed entry and the task's egress list both allow it.
    "mcp.call": frozenset({Leg.UNTRUSTED_INPUT}),
}


class AuthorityViolation(RuntimeError):
    """A task would hold all three properties."""


@dataclass(frozen=True)
class AgentCapability:
    """What a registered handler can reach, declared by trusted code."""

    sensitive_data: bool = False
    external_action: bool = False


def held_legs(tainted: bool, tools: Iterable[str],
              capability: AgentCapability) -> frozenset[Leg]:
    legs: set[Leg] = set()
    if tainted:
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
