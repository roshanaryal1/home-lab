"""Workspace files: read, list, write and search the task's own files (#240).

The payload is a short list of steps, run in order:

    {"steps": [{"op": "write", "path": "notes/a.md", "content": "..."},
               {"op": "search", "pattern": "TODO"},
               {"op": "read", "path": "notes/a.md"},
               {"op": "list", "path": "notes"}]}

The handler checks only the shape of each step. Every path is resolved by
the broker, inside this task's workspace and nowhere else: absolute paths,
``..``, symlinks and anything but regular files are refused there, so a
payload or a model cannot talk this handler into reaching outside.

Policy tier: notify. fs.read, fs.list and fs.search are autonomous and
fs.write is notify (``lab.broker.TOOL_TIERS``). It holds no delete.
"""

from __future__ import annotations

import json
from typing import Any

from lab.broker import PermanentFailure, ToolSession
from lab.policy import Tier
from lab.queue import Task

KIND = "workspace.files"
REF = "lab.handlers.workspace:run_steps"
TOOLS = frozenset({"fs.read", "fs.list", "fs.write", "fs.search"})
TIER = Tier.NOTIFY

MAX_STEPS = 16
MAX_READ_CHARS = 32 * 1024          # of one file's content, in the task result
MAX_RESULT_CHARS = 512 * 1024       # of the whole result; the queue caps it at 1 MB

# op -> (broker tool, fields the step may carry besides "op")
_OPS: dict[str, tuple[str, frozenset[str]]] = {
    "read": ("fs.read", frozenset({"path"})),
    "list": ("fs.list", frozenset({"path"})),
    "write": ("fs.write", frozenset({"path", "content"})),
    "search": ("fs.search", frozenset({"pattern", "path"})),
}


def _step_result(op: str, detail: dict[str, Any]) -> dict[str, Any]:
    out = {"op": op, **detail}
    if op == "read":
        content = str(detail.get("content", ""))
        out["content"] = content[:MAX_READ_CHARS]
        out["truncated"] = len(content) > MAX_READ_CHARS
    return out


async def run_steps(task: Task, tools: ToolSession) -> dict[str, Any]:
    steps = task.payload.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise PermanentFailure(f"payload needs 'steps': a list of 1 to {MAX_STEPS} steps")
    results: list[dict[str, Any]] = []
    used = 0
    for number, step in enumerate(steps):
        if not isinstance(step, dict) or step.get("op") not in _OPS:
            raise PermanentFailure(f"step {number}: 'op' must be one of {sorted(_OPS)}")
        tool, fields = _OPS[step["op"]]
        params = {k: v for k, v in step.items() if k != "op"}
        unknown = set(params) - fields
        if unknown:
            raise PermanentFailure(f"step {number}: unknown fields {sorted(unknown)}")
        result = tools.submit(tool, **params)
        if not result.ok:
            # A refusal (a path outside, a symlink, a cap) will not change on
            # a retry, so the task fails here rather than running on.
            raise PermanentFailure(f"step {number} ({step['op']}) refused: {result.error}")
        entry = _step_result(step["op"], result.detail)
        used += len(json.dumps(entry))
        if used > MAX_RESULT_CHARS:
            raise PermanentFailure(f"step {number}: results larger than {MAX_RESULT_CHARS} "
                                   "characters; split the task")
        results.append(entry)
    return {"steps": results}
