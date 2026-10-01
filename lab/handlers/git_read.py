"""Git read-only: status, log and diff of a repository in the workspace (#240).

The payload names the command and, optionally, the repository directory
relative to the workspace:

    {"command": "log", "repo": "project"}

Nothing else from the payload reaches git. The broker builds the whole
command line itself, checks the repository before git runs (it must be
inside the workspace, hold no symlinks, point nowhere else and set only
reviewed config keys) and runs git with hooks, fsmonitor, pagers,
external diff and every transport switched off. Output is capped.

Policy tier: autonomous. git.status, git.log and git.diff are read-only
and cannot leave the workspace; SECURITY.md gives the reasoning.
"""

from __future__ import annotations

from typing import Any

from lab.broker import PermanentFailure, ToolSession
from lab.policy import Tier
from lab.queue import Task

KIND = "git.read"
REF = "lab.handlers.git_read:read_repository"
TOOLS = frozenset({"git.status", "git.log", "git.diff"})
TIER = Tier.AUTONOMOUS

COMMANDS = ("status", "log", "diff")


async def read_repository(task: Task, tools: ToolSession) -> dict[str, Any]:
    payload = task.payload
    command = payload.get("command")
    repo = payload.get("repo", ".")
    if command not in COMMANDS or not isinstance(repo, str) \
            or set(payload) - {"command", "repo"}:
        raise PermanentFailure(
            f"payload must be {{'command': one of {list(COMMANDS)}, 'repo': '<directory>'}}")
    result = tools.submit(f"git.{command}", repo=repo)
    if not result.ok:
        raise PermanentFailure(f"git {command} refused or failed: {result.error}")
    return {"command": command, "repo": repo, "output": result.detail.get("output", ""),
            "truncated": bool(result.detail.get("truncated"))}
