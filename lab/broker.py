"""The execution broker: the intended single path from a worker to anything real.

Every call is authorized by the policy engine against the tool's tier
from the trusted registry below (item 1.1, #43). Not yet the *only*
path: handlers run inside the supervisor process, so nothing but
convention stops one touching the filesystem directly (#48, item 1.2).

Closes issue #10. Until now, isolation existed as instructions in
`ops/mac-mini-setup.md` and nowhere in code, and a non-admin account
alone is not isolation from other processes on the same host.

The boundary this module draws is the one in the architecture diagram:
**nothing below the broker line is reachable except through a typed,
brokered request.** A worker does not open files, spawn shells or make
network calls. It submits a `ToolRequest` and gets a `ToolResult`.

Why that matters beyond tidiness: it moves the security decision out of
the place where model output lives. A handler that takes a path from a
model and opens it is one prompt injection away from reading anything.
A handler that submits a request the broker resolves against a workspace
root cannot be talked into escaping, because the confinement is applied
after the model has stopped being involved.

Process isolation (issue #17) is provided by `lab.sandbox`: anything that
executes code runs under a Seatbelt profile so that a subprocess cannot
escape the workspace by making its own syscalls. Python-level path
confinement binds only the caller; kernel enforcement binds the whole
process tree.

What is deliberately NOT here yet, and is tracked rather than pretended:

* Network egress control per host. Seatbelt denies network outright by
  default; per-host allowlisting is #14.
* Resource ceilings on CPU and memory. Needs the model adapter. #16.
* Secret injection. Needs somewhere to inject secrets into. #15.

`SECURITY.md` says this plainly rather than implying protection that
does not exist.
"""

from __future__ import annotations

import json
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from lab import sandbox
from lab.policy import Decision, PolicyEngine, Tier


class BrokerError(RuntimeError):
    """Base for every refusal the broker makes."""


class PathEscape(BrokerError):
    """A request tried to reach outside its workspace."""


class ToolNotAllowed(BrokerError):
    """The requesting worker has no grant for this tool."""


class QuotaExceeded(BrokerError):
    """The request would breach a declared ceiling."""


class PolicyUnavailable(BrokerError):
    """No policy engine, or it failed. Nothing runs without a decision."""


class PolicyDenied(BrokerError):
    """Policy refused this call outright."""


class ApprovalRequired(BrokerError):
    """This exact call needs a human approval that does not exist yet.

    Raised out of ``submit()`` rather than returned as a failed result:
    the worker cannot continue, and the supervisor must park the task
    (release its lease) until the approval named here is decided.
    """

    def __init__(self, approval_id: str, reason: str) -> None:
        super().__init__(reason)
        self.approval_id = approval_id


# Which tier each tool requires. The broker takes the tier from here,
# never from the task or the request, and passes it to the policy engine
# on every call, so a task cannot under-declare a tool's authority.
TOOL_TIERS: dict[str, Tier] = {
    "fs.read": Tier.AUTONOMOUS,
    "fs.list": Tier.AUTONOMOUS,
    "fs.write": Tier.NOTIFY,
    "fs.delete": Tier.APPROVE,
    # Runs a command. Requires OS-level isolation, never Python-level,
    # because a subprocess makes its own syscalls.
    "shell.run": Tier.APPROVE,
}


@dataclass(frozen=True)
class ToolRequest:
    """A typed ask. The only thing a worker may submit."""

    tool: str
    params: dict
    task_id: str
    worker: str


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    tool: str
    detail: dict = field(default_factory=dict)
    error: str | None = None


@dataclass
class Workspace:
    """A per-task directory. Created with the task, destroyed with it.

    Confinement is by resolved path, not by string prefix, so a symlink
    pointing outside is caught as well as a literal `../`.
    """

    root: Path
    max_bytes: int = 64 * 1024 * 1024
    max_files: int = 2_000

    def resolve(self, relative: str) -> Path:
        """Resolve inside the workspace, or refuse.

        `strict=False` so a not-yet-existing file still resolves, and
        `is_relative_to` on the resolved path so symlink traversal is
        caught rather than only textual `..`.
        """
        candidate = (self.root / relative).resolve(strict=False)
        root = self.root.resolve(strict=False)
        if not candidate.is_relative_to(root):
            raise PathEscape(
                f"{relative!r} resolves outside the workspace"
            )
        return candidate

    def usage(self) -> tuple[int, int]:
        files = [p for p in self.root.rglob("*") if p.is_file()]
        return len(files), sum(p.stat().st_size for p in files)

    def destroy(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class ExecutionBroker:
    """Resolves tool requests against a workspace, a grant list and policy."""

    def __init__(
        self,
        workspace_root: Path,
        policy: PolicyEngine | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root)
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.policy = policy
        self._workspaces: dict[str, Workspace] = {}
        self._grants: dict[str, set[str]] = {}

    # ------------------------------------------------------- lifecycle

    def open_workspace(self, task_id: str, allowed_tools: set[str]) -> Workspace:
        """Give a task its own directory and an explicit tool allowlist.

        The allowlist is per task and default-deny: a tool not named here
        is refused even if it exists.
        """
        unknown = allowed_tools - set(TOOL_TIERS)
        if unknown:
            raise ToolNotAllowed(f"unknown tools requested: {sorted(unknown)}")

        path = self.workspace_root / f"task-{task_id}-{uuid.uuid4().hex[:8]}"
        path.mkdir(parents=True, exist_ok=False)
        ws = Workspace(root=path)
        self._workspaces[task_id] = ws
        self._grants[task_id] = set(allowed_tools)
        return ws

    def close_workspace(self, task_id: str) -> None:
        ws = self._workspaces.pop(task_id, None)
        self._grants.pop(task_id, None)
        if ws is not None:
            ws.destroy()

    # --------------------------------------------------------- dispatch

    def _registry(self) -> dict[str, Callable[[ToolRequest, Workspace], ToolResult]]:
        """The trusted dispatch table. Only tools named here can run.

        Explicit rather than ``getattr(self, f"_tool_{name}")``, so a
        request naming some other method cannot reach it, and every entry
        must also carry a tier in TOOL_TIERS.
        """
        return {
            "fs.read": self._tool_fs_read,
            "fs.list": self._tool_fs_list,
            "fs.write": self._tool_fs_write,
            "fs.delete": self._tool_fs_delete,
            "shell.run": self._tool_shell_run,
        }

    def submit(self, request: ToolRequest) -> ToolResult:
        """The single entry point. Everything is checked before anything runs.

        Order: known tool, per-task grant, open workspace, then policy for
        this exact call. Raises ``ApprovalRequired`` when a human must
        decide first; every other refusal is a failed ``ToolResult``.
        """
        try:
            handler = self._registry().get(request.tool)
            if handler is None or request.tool not in TOOL_TIERS:
                raise ToolNotAllowed(f"no such tool: {request.tool}")
            self._check_grant(request)
            ws = self._workspace_for(request.task_id)
            self._authorize(request)
            return handler(request, ws)
        except ApprovalRequired:
            raise
        except BrokerError as exc:
            return ToolResult(ok=False, tool=request.tool,
                              error=f"{type(exc).__name__}: {exc}")

    def _authorize(self, request: ToolRequest) -> None:
        if self.policy is None:
            raise PolicyUnavailable("no policy engine; refusing every call")
        try:
            verdict = self.policy.authorize_tool(
                request.task_id, request.tool, request.params,
                TOOL_TIERS[request.tool],
            )
        except Exception as exc:
            # Includes a failed audit write: an unrecorded allow is not an allow.
            raise PolicyUnavailable(f"policy check failed: {exc}") from exc
        if verdict.decision is Decision.NEEDS_APPROVAL:
            assert verdict.approval_id is not None
            raise ApprovalRequired(verdict.approval_id, verdict.reason)
        if not verdict.allowed:
            raise PolicyDenied(verdict.reason)

    def _check_grant(self, request: ToolRequest) -> None:
        granted = self._grants.get(request.task_id, set())
        if request.tool not in granted:
            raise ToolNotAllowed(
                f"task {request.task_id} has no grant for {request.tool}"
            )

    def _workspace_for(self, task_id: str) -> Workspace:
        ws = self._workspaces.get(task_id)
        if ws is None:
            raise BrokerError(f"no open workspace for task {task_id}")
        return ws

    # ------------------------------------------------------------ tools

    def _tool_fs_read(self, request: ToolRequest, ws: Workspace) -> ToolResult:
        target = ws.resolve(request.params["path"])
        if not target.is_file():
            return ToolResult(False, request.tool, error="not a file")
        return ToolResult(True, request.tool,
                          {"content": target.read_text(encoding="utf-8")})

    def _tool_fs_list(self, request: ToolRequest, ws: Workspace) -> ToolResult:
        target = ws.resolve(request.params.get("path", "."))
        if not target.is_dir():
            return ToolResult(False, request.tool, error="not a directory")
        names = sorted(p.name for p in target.iterdir())
        return ToolResult(True, request.tool, {"entries": names})

    def _tool_fs_write(self, request: ToolRequest, ws: Workspace) -> ToolResult:
        content = request.params["content"]
        target = ws.resolve(request.params["path"])

        files, used = ws.usage()
        if files + 1 > ws.max_files:
            raise QuotaExceeded(f"file count ceiling {ws.max_files} reached")
        if used + len(content.encode("utf-8")) > ws.max_bytes:
            raise QuotaExceeded(f"workspace byte ceiling {ws.max_bytes} reached")

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return ToolResult(True, request.tool, {"bytes": len(content)})

    def _tool_shell_run(self, request: ToolRequest, ws: Workspace) -> ToolResult:
        """Run a command, confined by the kernel rather than by us.

        Refuses outright if OS-level isolation is unavailable. Running a
        command unconfined because the sandbox was missing would be the
        exact failure this tool exists to prevent.
        """
        argv = request.params["argv"]
        if not isinstance(argv, list) or not argv:
            raise BrokerError("argv must be a non-empty list")

        try:
            result = sandbox.run(
                argv, ws.root,
                timeout=float(request.params.get("timeout", 30.0)),
            )
        except sandbox.SandboxUnavailable as exc:
            raise BrokerError(f"refusing to run unconfined: {exc}") from exc

        return ToolResult(
            ok=result.ok,
            tool=request.tool,
            detail={"stdout": result.stdout, "stderr": result.stderr,
                    "returncode": result.returncode,
                    "sandbox_denied": result.denied},
            error=None if result.ok else result.stderr.strip() or "failed",
        )

    def _tool_fs_delete(self, request: ToolRequest, ws: Workspace) -> ToolResult:
        target = ws.resolve(request.params["path"])
        if not target.exists():
            return ToolResult(False, request.tool, error="nothing to delete")
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        return ToolResult(True, request.tool, {"deleted": str(target.name)})

    # ------------------------------------------------------------ audit

    def manifest(self, task_id: str) -> dict:
        """What a task's workspace contained when it finished.

        Part of the evidence plane: an execution that leaves no record of
        what it produced cannot be audited afterwards.
        """
        ws = self._workspaces.get(task_id)
        if ws is None:
            return {"task_id": task_id, "workspace": None}
        files, used = ws.usage()
        return {
            "task_id": task_id,
            "workspace": str(ws.root),
            "file_count": files,
            "bytes_used": used,
            "granted_tools": sorted(self._grants.get(task_id, set())),
            "entries": sorted(
                str(p.relative_to(ws.root))
                for p in ws.root.rglob("*") if p.is_file()
            ),
        }

    def manifest_json(self, task_id: str) -> str:
        return json.dumps(self.manifest(task_id), sort_keys=True)
