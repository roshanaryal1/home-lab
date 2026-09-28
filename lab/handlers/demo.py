"""Small handlers that exercise the worker boundary in tests and smoke runs."""

from __future__ import annotations

import os
from typing import Any

from lab.broker import ToolSession
from lab.queue import Task


async def write_note(task: Task, tools: ToolSession) -> dict[str, Any]:
    """Write the payload's note into the workspace and read it back."""
    note = str(task.payload.get("note", ""))
    tools.submit("fs.write", path="note.txt", content=note)
    back = tools.submit("fs.read", path="note.txt")
    return {"read_back": back.detail.get("content")}


async def delete_note(task: Task, tools: ToolSession) -> dict[str, Any]:
    """Needs an approval: fs.delete is approve tier."""
    if "note.txt" not in tools.submit("fs.list").detail.get("entries", []):
        tools.submit("fs.write", path="note.txt", content="x")
    return {"deleted": tools.submit("fs.delete", path="note.txt").ok}


async def describe_process(task: Task, tools: ToolSession) -> dict[str, Any]:
    """Report what the worker process can see of its own environment."""
    return {"pid": os.getpid(), "env": sorted(os.environ), "cwd": os.getcwd()}


async def explode(task: Task, tools: ToolSession) -> dict[str, Any]:
    raise ValueError("handler exploded in the worker")


async def sleep_for(task: Task, tools: ToolSession) -> dict[str, Any]:
    """Sleep for payload["seconds"]; used to test the wall-clock ceiling."""
    import asyncio
    await asyncio.sleep(float(task.payload.get("seconds", 60)))
    return {"slept": True}


async def record_pid_then_sleep(task: Task, tools: ToolSession) -> dict[str, Any]:
    """Leave this worker's pid in the workspace, then sleep. Lets a test
    check that stopping the task really ended the process."""
    import asyncio
    tools.submit("fs.write", path="pid", content=str(os.getpid()))
    await asyncio.sleep(float(task.payload.get("seconds", 60)))
    return {"slept": True}


async def reject_input(task: Task, tools: ToolSession) -> dict[str, Any]:
    """A permanent failure: no retry will make this input valid."""
    from lab.broker import PermanentFailure
    raise PermanentFailure("payload has no 'note'")

