"""Run a reviewed handler in its own process (item 1.2, #48).

A handler running inside the supervisor shares its memory: it can reach
the database connection, the policy engine and every other session. A
worker process cannot. It is started with a minimal environment, in the
task's workspace, with no database path, no lease token and no broker.
Its only way to act is the line-oriented JSON channel on its stdin and
stdout, and every request on that channel is resolved by the broker in
the supervisor, against the context the supervisor built.

What this does not do yet: the worker runs as the same OS user, so a
hostile handler could still open the database file if it found the
path. Running workers under the separate lab account (4.5, #70) closes
that, and needs the Mac mini. Until then the boundary is the process,
plus the rule that only reviewed code under ``lab.handlers`` is loaded.

Protocol, one JSON object per line:

    supervisor -> worker  {"type": "start", "handler": ..., "task": {...}, "context": {...}}
    worker -> supervisor  {"type": "call", "tool": ..., "params": {...}}
    supervisor -> worker  {"type": "result", "ok": ..., "detail": {...}, "error": ...}
    worker -> supervisor  {"type": "done", "result": {...}}
                          {"type": "error", "error": "..."}
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import os
import signal
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from lab.broker import PermanentFailure, ToolResult, ToolSession
from lab.queue import Task

# Only code in this package may be loaded into a worker. A task or a
# model may choose which reviewed handler runs; it never supplies code.
REVIEWED_PREFIX = "lab.handlers."

# Largest single protocol line accepted from a worker, in bytes.
MAX_LINE = 1024 * 1024

# How much of the worker's stderr to keep for an error report.
STDERR_TAIL = 8192


class WorkerError(RuntimeError):
    """The worker process failed, crashed or broke the protocol."""


def check_reference(ref: str) -> tuple[str, str]:
    """Split ``module:function`` and refuse anything outside reviewed code."""
    module, sep, func = ref.partition(":")
    if not sep or not module.startswith(REVIEWED_PREFIX) or not func.isidentifier():
        raise ValueError(
            f"handler {ref!r} is not a reviewed handler; expected "
            f"'{REVIEWED_PREFIX}<module>:<function>'"
        )
    return module, func


def worker_environment(workspace: Path) -> dict[str, str]:
    """The whole environment a worker gets. Nothing from the supervisor."""
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(workspace),
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


# ------------------------------------------------------ supervisor side


async def run_in_worker(ref: str, task: Task, tools: ToolSession, *,
                        workspace: Path) -> dict[str, Any]:
    """Run ``ref`` for ``task`` in a fresh process; broker its tool calls.

    Raises ``ApprovalRequired`` when a call needs a human (after ending
    the worker, since the task will park), and ``WorkerError`` when the
    worker fails, crashes or misbehaves. The worker's whole process group
    is killed on the way out, whatever happened.
    """
    check_reference(ref)
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-I", "-m", "lab.worker",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=worker_environment(workspace),
        cwd=str(workspace),
        start_new_session=True,
        limit=MAX_LINE,
    )
    assert proc.stdin and proc.stdout and proc.stderr
    stdin, stdout, stderr = proc.stdin, proc.stdout, proc.stderr
    stderr_tail = bytearray()

    async def drain_stderr() -> None:
        while chunk := await stderr.read(4096):
            stderr_tail.extend(chunk)
            del stderr_tail[:-STDERR_TAIL]

    drainer = asyncio.create_task(drain_stderr())

    async def send(message: dict[str, Any]) -> None:
        stdin.write(json.dumps(message).encode() + b"\n")
        await stdin.drain()

    ctx = tools.context
    try:
        await send({
            "type": "start",
            "handler": ref,
            "task": {"id": task.id, "title": task.title, "state": task.state,
                     "priority": task.priority, "attempts": task.attempts,
                     "max_attempts": task.max_attempts,
                     "idempotent": task.idempotent, "payload": task.payload,
                     "weight": task.weight, "capability_tier": task.capability_tier,
                     "agent_kind": task.agent_kind},
            # Never the lease token: that is the supervisor's credential.
            "context": {"task_id": ctx.task_id, "agent_kind": ctx.agent_kind,
                        "attempt": ctx.attempt},
        })
        while True:
            try:
                line = await stdout.readline()
            except ValueError as exc:
                raise WorkerError(f"worker sent an oversized message: {exc}") from exc
            if not line:
                await proc.wait()
                tail = bytes(stderr_tail).decode(errors="replace").strip()[-500:]
                raise WorkerError(
                    f"worker exited ({proc.returncode}) without a result: {tail}"
                )
            try:
                message = json.loads(line)
                kind = message["type"]
            except (ValueError, KeyError, TypeError) as exc:
                raise WorkerError(f"worker broke the protocol: {line[:200]!r}") from exc

            if kind == "call":
                params = message.get("params")
                tool = message.get("tool")
                if not isinstance(tool, str) or not isinstance(params, dict):
                    raise WorkerError("worker sent a malformed call")
                # Off the event loop, so the heartbeat keeps beating (1.8).
                result = await tools.submit_async(tool, **params)  # may raise ApprovalRequired
                await send({"type": "result", **asdict(result)})
            elif kind == "done":
                result_obj = message.get("result")
                if not isinstance(result_obj, dict):
                    raise WorkerError("worker returned a non-object result")
                return result_obj
            elif kind == "error":
                if message.get("permanent") is True:
                    raise PermanentFailure(str(message.get("error")))
                raise WorkerError(f"handler failed in worker: {message.get('error')}")
            else:
                raise WorkerError(f"unknown message type {kind!r}")
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
        drainer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await drainer


# ---------------------------------------------------------- worker side


@dataclass(frozen=True)
class WorkerContext:
    """What a handler in a worker may know about who it is acting for."""

    task_id: str
    agent_kind: str
    attempt: int


class RemoteTools:
    """The worker's ToolSession stand-in: every call goes to the supervisor."""

    def __init__(self, context: WorkerContext, reader: Any, writer: Any) -> None:
        self.context = context
        self._reader = reader
        self._writer = writer

    def submit(self, tool: str, **params: object) -> ToolResult:
        self._writer.write(json.dumps({"type": "call", "tool": tool,
                                       "params": params}) + "\n")
        self._writer.flush()
        line = self._reader.readline()
        if not line:
            raise SystemExit("supervisor closed the channel")
        reply = json.loads(line)
        return ToolResult(ok=reply["ok"], tool=reply["tool"],
                          detail=reply.get("detail") or {}, error=reply.get("error"))


def main() -> None:
    # The protocol owns the real stdout. Anything a handler prints goes to
    # stderr, so it can never be mistaken for a protocol message.
    proto_out = os.fdopen(os.dup(1), "w", buffering=1)
    sys.stdout = sys.stderr
    reader = sys.stdin

    def emit(message: dict[str, Any]) -> None:
        proto_out.write(json.dumps(message) + "\n")
        proto_out.flush()

    try:
        start = json.loads(reader.readline())
        module_name, func_name = check_reference(start["handler"])
        handler = getattr(importlib.import_module(module_name), func_name)
        task = Task(**start["task"])
        tools = RemoteTools(WorkerContext(**start["context"]), reader, proto_out)
        result = asyncio.run(handler(task, tools))
        emit({"type": "done", "result": result})
    except BaseException as exc:
        emit({"type": "error", "error": f"{type(exc).__name__}: {exc}",
              "permanent": isinstance(exc, PermanentFailure)})
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
