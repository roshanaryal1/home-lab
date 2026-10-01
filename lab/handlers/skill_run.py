"""Run one script of an active skill, in the container (#255).

The payload names the skill, the script inside it and, optionally, its
arguments:

    {"skill": "summarise", "script": "scripts/run.sh", "args": ["notes.md"]}

The handler makes one call, skill.run, and holds no other tool. The broker
does the rest: it resolves the active version only, refuses anything it
cannot run safely before a person is asked, parks the task until the
operator signs an approval bound to that version and its content hash, and
runs the script only in a disposable container with no network.

What the script printed is written by code the lab did not write. It comes
back marked untrusted and is returned as data for a person to read, never
acted on here.

Policy tier: approve, the tier of skill.run. Every call waits for the
operator's signature. Registered by ``register_all`` only when
``LAB_CONTAINER_IMAGE`` names an image pinned by digest. The owner granted
the tool to this handler on 2026-10-01.
"""

from __future__ import annotations

from typing import Any

from lab.broker import PermanentFailure, ToolSession
from lab.policy import Tier
from lab.queue import Task

KIND = "skill.run"
REF = "lab.handlers.skill_run:run_skill"
TOOLS = frozenset({"skill.run"})
TIER = Tier.APPROVE

_FIELDS = frozenset({"skill", "script", "args"})
_USAGE = "payload must be {'skill': '<name>', 'script': '<path>', 'args': ['<arg>', ...]}"

# Copied from a run that started. A refusal carries none of them.
_RUN_FIELDS = ("skill", "version", "script", "content_sha256", "stdout", "stderr",
               "returncode", "timed_out", "cancelled", "truncated")


def check_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """The broker parameters for a well-formed payload, or a refusal."""
    skill = payload.get("skill")
    script = payload.get("script")
    args = payload.get("args", [])
    if (set(payload) - _FIELDS or not isinstance(skill, str) or not skill
            or not isinstance(script, str) or not script or not isinstance(args, list)
            or not all(isinstance(a, str) for a in args)):
        raise PermanentFailure(_USAGE)
    params: dict[str, Any] = {"skill": skill, "script": script}
    if args:
        params["args"] = list(args)
    return params


async def run_skill(task: Task, tools: ToolSession) -> dict[str, Any]:
    params = check_payload(task.payload)
    result = tools.submit("skill.run", **params)
    if "returncode" not in result.detail:
        # Refused before anything started. The same call is refused again.
        raise PermanentFailure(f"skill.run refused: {result.error}")
    out = {key: result.detail.get(key) for key in _RUN_FIELDS}
    out["ok"] = result.ok
    out["untrusted"] = True
    return out
