"""Read a repository from an operator-signed source at an exact revision (ADR 0008).

The payload names the source, the commit and the read-only git command:

    {"source": "home-lab", "revision": "<40-character commit id>", "command": "log"}

The handler makes two calls. ``workspace.acquire`` copies that commit of the
source into the task's workspace and records where it came from, then the
read-only git command runs on the copy. The handler holds those four tools and
no other. It is granted only the sources in the operator-signed sources file.

The copy is approve tier, so every task parks until the operator signs an
approval bound to the signed source entry, the revision and the directory.
The git command that follows is autonomous, as in ``git.read``.

What git prints comes from a repository someone else wrote. It is returned as
data for a person to read, marked untrusted, and never acted on here.

Policy tier: approve, the tier of workspace.acquire. Registered by
``register_all`` only when a sources file is configured and every entry in it
verifies.
"""

from __future__ import annotations

from typing import Any

from lab.broker import PermanentFailure, ToolSession
from lab.policy import Tier
from lab.queue import Task

KIND = "repo.read"
REF = "lab.handlers.repo_read:read_source"
TOOLS = frozenset({"workspace.acquire", "git.status", "git.log", "git.diff"})
TIER = Tier.APPROVE

COMMANDS = ("status", "log", "diff")
DIRECTORY = "repo"

_FIELDS = frozenset({"source", "revision", "command"})
_USAGE = ("payload must be {'source': '<name>', 'revision': '<40-character commit id>', "
          f"'command': one of {list(COMMANDS)}}}")


def check_payload(payload: dict[str, Any]) -> tuple[str, str, str]:
    """The source, revision and command of a well-formed payload, or a refusal."""
    source = payload.get("source")
    revision = payload.get("revision")
    command = payload.get("command")
    if (set(payload) - _FIELDS or not isinstance(source, str) or not source
            or not isinstance(revision, str) or not revision or command not in COMMANDS):
        raise PermanentFailure(_USAGE)
    return source, revision, str(command)


async def read_source(task: Task, tools: ToolSession) -> dict[str, Any]:
    source, revision, command = check_payload(task.payload)
    acquired = tools.submit("workspace.acquire", source=source, revision=revision,
                            dir=DIRECTORY)
    if not acquired.ok:
        # Refused, or the copy failed. The call is journaled, so a retry would
        # only replay this outcome.
        raise PermanentFailure(f"workspace.acquire failed: {acquired.error}")
    result = tools.submit(f"git.{command}", repo=DIRECTORY)
    if not result.ok:
        raise PermanentFailure(f"git {command} refused or failed: {result.error}")
    return {"source": source, "revision": revision,
            "tree": acquired.detail.get("tree"),
            "provenance": acquired.detail.get("provenance"),
            "command": command, "output": result.detail.get("output", ""),
            "truncated": bool(result.detail.get("truncated")), "untrusted": True}
