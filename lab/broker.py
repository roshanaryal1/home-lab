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
network calls. It holds a `ToolSession` and gets a `ToolResult`.

Identity (item 1.2, #48). The supervisor, which is trusted code, builds
an `ExecutionContext` (task, agent kind, attempt, lease token) and hands
the handler a `ToolSession` bound to it. A handler never names a task,
so it cannot reach another task's workspace, and every call re-checks
that the context's lease is still live, so a worker that lost its lease
can no longer act.

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

import contextlib
import errno
import hashlib
import itertools
import json
import math
import os
import shutil
import stat
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from lab import sandbox
from lab.policy import Decision, PolicyEngine, Tier
from lab.queue import LeaseToken


class BrokerError(RuntimeError):
    """Base for every refusal the broker makes."""


class PathEscape(BrokerError):
    """A request tried to reach outside its workspace."""


class ContextRevoked(BrokerError):
    """The session's lease is no longer live, or it was never valid."""


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


# Per-task and per-call ceilings (item 1.10). A request can lower these
# where a parameter allows it, never raise them.
MAX_CALLS_PER_TASK = 1000
MAX_READ_BYTES = 1024 * 1024
MAX_LIST_ENTRIES = 1000

# What each tool accepts: field -> (validator, required). Unknown fields
# are refused, so a model cannot pass options the broker never reviewed.
_Validator = Callable[[object], bool]


def _is_str(v: object) -> bool:
    return isinstance(v, str)


def _is_argv(v: object) -> bool:
    return isinstance(v, list) and bool(v) and all(isinstance(a, str) and "\0" not in a
                                                   for a in v)


def _is_timeout(v: object) -> bool:
    return (isinstance(v, int | float) and not isinstance(v, bool)
            and math.isfinite(v) and v > 0)


TOOL_SCHEMAS: dict[str, dict[str, tuple[_Validator, bool]]] = {
    "fs.read": {"path": (_is_str, True)},
    "fs.list": {"path": (_is_str, False)},
    "fs.write": {"path": (_is_str, True), "content": (_is_str, True)},
    "fs.delete": {"path": (_is_str, True)},
    "shell.run": {"argv": (_is_argv, True), "timeout": (_is_timeout, False)},
}


class InvalidParams(BrokerError):
    """The call's parameters do not match the tool's schema."""


def validate_params(tool: str, params: dict) -> None:
    schema = TOOL_SCHEMAS.get(tool)
    if schema is None:
        raise ToolNotAllowed(f"no schema for tool {tool}")
    unknown = set(params) - set(schema)
    if unknown:
        raise InvalidParams(f"{tool}: unknown parameters {sorted(unknown)}")
    for name, (valid, required) in schema.items():
        if name not in params:
            if required:
                raise InvalidParams(f"{tool}: missing parameter {name!r}")
            continue
        if not valid(params[name]):
            raise InvalidParams(f"{tool}: invalid value for {name!r}")


@dataclass(frozen=True)
class ExecutionContext:
    """Who is acting, built only by trusted code (the supervisor).

    Never taken from a request or from model output. ``lease`` is the
    token from ``TaskQueue.lease()``; the broker checks it on every call.
    """

    task_id: str
    agent_kind: str
    attempt: int
    lease: LeaseToken


@dataclass(frozen=True)
class ToolRequest:
    """One call, as the broker builds it from a session. Internal: there
    is no public entry point that accepts a caller-built request."""

    tool: str
    params: dict
    task_id: str


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    tool: str
    detail: dict = field(default_factory=dict)
    error: str | None = None


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _is_protected(parts: list[str]) -> bool:
    """Git hooks and shell start-up files run outside the sandbox later."""
    if parts and parts[-1] in sandbox.PROTECTED_NAMES:
        return True
    return any(a == ".git" and b == "hooks" for a, b in itertools.pairwise(parts))


@dataclass
class Workspace:
    """A per-task directory. Created with the task, destroyed with it.

    File tools act through descriptors opened one path component at a
    time with O_NOFOLLOW (item 1.5). Checking a resolved path and then
    opening it by name left a window in which a directory could be
    swapped for a symlink (R09); walking by descriptor refuses a symlink
    in any component at the moment of use, so there is no window.
    ``resolve()`` remains as an early, readable refusal, not the control.
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

    @staticmethod
    def parts(relative: str) -> list[str]:
        """Split a workspace-relative path, refusing absolute and ``..``."""
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts or "\0" in relative:
            raise PathEscape(f"{relative!r} is not a plain workspace-relative path")
        return [p for p in path.parts if p not in ("", ".")]

    @contextlib.contextmanager
    def dir_fd(self, parts: list[str], *, create: bool = False) -> Iterator[int]:
        """A descriptor for the directory ``parts`` names, walked safely."""
        fd = os.open(self.root, _DIR_FLAGS)
        try:
            for name in parts:
                if create:
                    with contextlib.suppress(FileExistsError):
                        os.mkdir(name, 0o700, dir_fd=fd)
                try:
                    child = os.open(name, _DIR_FLAGS, dir_fd=fd)
                except OSError as exc:
                    if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
                        raise PathEscape(
                            f"{name!r} is a symlink or not a directory") from None
                    raise
                os.close(fd)
                fd = child
            yield fd
        finally:
            os.close(fd)

    def state_hash(self) -> str:
        """Hash of every name and file content under the root.

        Content, not timestamps, so a rerun that rewrites a file with the
        same bytes keeps the same hash. Symlinks are hashed by their
        target text and never followed.
        """
        digest = hashlib.sha256()
        for path in sorted(self.root.rglob("*")):
            rel = str(path.relative_to(self.root))
            if path.is_symlink():
                digest.update(f"L {rel} -> {path.readlink()}\n".encode())
            elif path.is_file():
                digest.update(f"F {rel} ".encode())
                digest.update(hashlib.sha256(path.read_bytes()).digest())
                digest.update(b"\n")
            elif path.is_dir():
                digest.update(f"D {rel}\n".encode())
        return digest.hexdigest()

    def regular_files(self) -> list[Path]:
        """Regular files only, by lstat: a symlink to something outside is
        neither counted nor followed."""
        out = []
        for dirpath, _dirs, files in os.walk(self.root, followlinks=False):
            for name in files:
                path = Path(dirpath) / name
                if stat.S_ISREG(path.lstat().st_mode):
                    out.append(path)
        return out

    def usage(self) -> tuple[int, int]:
        files = self.regular_files()
        return len(files), sum(p.lstat().st_size for p in files)

    def destroy(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class ExecutionBroker:
    """Resolves tool requests against a workspace, a grant list and policy."""

    def __init__(
        self,
        workspace_root: Path,
        policy: PolicyEngine | None = None,
        leases: Callable[[LeaseToken], bool] | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root)
        self.workspace_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.policy = policy
        # Usually TaskQueue.owns_lease. None means no call can prove its
        # lease, so every call is refused: fail closed.
        self._leases = leases
        self._workspaces: dict[str, Workspace] = {}
        self._calls: dict[str, int] = {}
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

        if task_id in self._workspaces:
            raise BrokerError(f"task {task_id} already has an open workspace")
        # Named here, never by the task; private to the lab user.
        path = self.workspace_root / f"task-{task_id}-{uuid.uuid4().hex[:8]}"
        path.mkdir(mode=0o700, parents=False, exist_ok=False)
        ws = Workspace(root=path)
        self._workspaces[task_id] = ws
        self._grants[task_id] = set(allowed_tools)
        self._calls[task_id] = 0
        return ws

    def close_workspace(self, task_id: str) -> None:
        ws = self._workspaces.pop(task_id, None)
        self._grants.pop(task_id, None)
        self._calls.pop(task_id, None)
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

    def session(self, ctx: ExecutionContext) -> ToolSession:
        """The handle a handler gets. Bound to one context, for its life."""
        return ToolSession(self, ctx)

    def _dispatch(self, ctx: ExecutionContext, tool: str, params: dict) -> ToolResult:
        """The single entry point. Everything is checked before anything runs.

        Order: live lease for this context, known tool, per-task grant,
        open workspace, then policy for this exact call. Raises
        ``ApprovalRequired`` when a human must decide first; every other
        refusal is a failed ``ToolResult``.
        """
        request = ToolRequest(tool=tool, params=dict(params), task_id=ctx.task_id)
        try:
            self._check_context(ctx)
            handler = self._registry().get(request.tool)
            if handler is None or request.tool not in TOOL_TIERS:
                raise ToolNotAllowed(f"no such tool: {request.tool}")
            self._check_grant(request)
            ws = self._workspace_for(request.task_id)
            # Before policy: a human is never asked to approve a malformed
            # call, and a runaway loop stops without touching the audit log.
            validate_params(request.tool, request.params)
            used = self._calls.get(request.task_id, 0)
            if used >= MAX_CALLS_PER_TASK:
                raise QuotaExceeded(f"tool call ceiling {MAX_CALLS_PER_TASK} reached")
            self._calls[request.task_id] = used + 1
            self._authorize(request, ws)
            return handler(request, ws)
        except ApprovalRequired:
            raise
        except BrokerError as exc:
            return ToolResult(ok=False, tool=request.tool,
                              error=f"{type(exc).__name__}: {exc}")

    def _check_context(self, ctx: ExecutionContext) -> None:
        if self._leases is None:
            raise ContextRevoked("no lease checker configured; refusing every call")
        if ctx.lease.task_id != ctx.task_id or not self._leases(ctx.lease):
            raise ContextRevoked(
                f"lease {ctx.lease.lease_id} on {ctx.task_id} is not live; "
                "this session can no longer act"
            )

    def _authorize(self, request: ToolRequest, ws: Workspace) -> None:
        if self.policy is None:
            raise PolicyUnavailable("no policy engine; refusing every call")
        tier = TOOL_TIERS[request.tool]
        try:
            # An approve-tier intent names the workspace state it will act
            # on, so a file changed after review voids the grant (1.4).
            preconditions = ({"workspace_sha256": ws.state_hash()}
                             if tier is Tier.APPROVE else None)
            verdict = self.policy.authorize_tool(
                request.task_id, request.tool, request.params, tier, preconditions,
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
        ws.resolve(request.params["path"])
        parts = ws.parts(request.params["path"])
        if not parts:
            return ToolResult(False, request.tool, error="not a file")
        with ws.dir_fd(parts[:-1]) as parent:
            try:
                fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            except FileNotFoundError:
                return ToolResult(False, request.tool, error="not a file")
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise PathEscape(f"{parts[-1]!r} is a symlink") from None
                return ToolResult(False, request.tool, error="not a file")
            with os.fdopen(fd, "rb") as fh:
                info = os.fstat(fh.fileno())
                if not stat.S_ISREG(info.st_mode):
                    return ToolResult(False, request.tool, error="not a file")
                if info.st_size > MAX_READ_BYTES:
                    raise QuotaExceeded(f"file larger than the {MAX_READ_BYTES} byte read cap")
                content = fh.read(MAX_READ_BYTES).decode("utf-8", errors="replace")
        return ToolResult(True, request.tool, {"content": content})

    def _tool_fs_list(self, request: ToolRequest, ws: Workspace) -> ToolResult:
        ws.resolve(request.params.get("path", "."))
        parts = ws.parts(request.params.get("path", "."))
        try:
            with ws.dir_fd(parts) as fd:
                names = sorted(os.listdir(fd))
            truncated = len(names) > MAX_LIST_ENTRIES
            names = names[:MAX_LIST_ENTRIES]
        except FileNotFoundError:
            return ToolResult(False, request.tool, error="not a directory")
        except PathEscape:
            raise
        except OSError:
            return ToolResult(False, request.tool, error="not a directory")
        return ToolResult(True, request.tool, {"entries": names, "truncated": truncated})

    def _tool_fs_write(self, request: ToolRequest, ws: Workspace) -> ToolResult:
        content = request.params["content"]
        ws.resolve(request.params["path"])
        parts = ws.parts(request.params["path"])
        if not parts:
            raise PathEscape("cannot write to the workspace root")
        if _is_protected(parts):
            raise PathEscape(f"{request.params['path']!r} is a protected path")

        files, used = ws.usage()
        if files + 1 > ws.max_files:
            raise QuotaExceeded(f"file count ceiling {ws.max_files} reached")
        if used + len(content.encode("utf-8")) > ws.max_bytes:
            raise QuotaExceeded(f"workspace byte ceiling {ws.max_bytes} reached")

        with ws.dir_fd(parts[:-1], create=True) as parent:
            try:
                fd = os.open(parts[-1],
                             os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                             0o600, dir_fd=parent)
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise PathEscape(f"{parts[-1]!r} is a symlink") from None
                raise BrokerError(f"cannot write {parts[-1]!r}: {exc.strerror}") from None
            with os.fdopen(fd, "wb") as fh:
                if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                    raise BrokerError("target is not a regular file")
                fh.write(content.encode("utf-8"))
        return ToolResult(True, request.tool, {"bytes": len(content)})

    def _tool_shell_run(self, request: ToolRequest, ws: Workspace) -> ToolResult:
        """Run a command, confined by the kernel rather than by us.

        Refuses outright if OS-level isolation is unavailable. Running a
        command unconfined because the sandbox was missing would be the
        exact failure this tool exists to prevent.
        """
        argv = request.params["argv"]

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
                    "sandbox_denied": result.denied,
                    "timed_out": result.timed_out,
                    "truncated": result.truncated},
            error=None if result.ok else result.stderr.strip() or "failed",
        )

    def _tool_fs_delete(self, request: ToolRequest, ws: Workspace) -> ToolResult:
        # No resolve() here: it follows a final symlink, which would refuse
        # to remove a planted link. The descriptor walk is the check.
        parts = ws.parts(request.params["path"])
        if not parts:
            raise PathEscape("refusing to delete the workspace root")
        if _is_protected(parts):
            raise PathEscape(f"{request.params['path']!r} is a protected path")
        with ws.dir_fd(parts[:-1]) as parent:
            try:
                st = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return ToolResult(False, request.tool, error="nothing to delete")
            if stat.S_ISDIR(st.st_mode):
                # fd-based and symlink-attack resistant on this platform.
                shutil.rmtree(parts[-1], dir_fd=parent)
            else:
                # Removes a symlink itself, never its target.
                os.unlink(parts[-1], dir_fd=parent)
        return ToolResult(True, request.tool, {"deleted": parts[-1]})

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
                str(p.relative_to(ws.root)) for p in ws.regular_files()
            ),
        }

    def manifest_json(self, task_id: str) -> str:
        return json.dumps(self.manifest(task_id), sort_keys=True)


class ToolSession:
    """A handler's only way to call a tool. Cannot name a task.

    Holds its broker privately. In-process that is a convention, not a
    boundary: handler code could still reach ``_broker``. For reviewed
    handlers run by ``lab.worker`` the session stays in the supervisor
    and the worker process only sees a JSON channel to it.
    """

    def __init__(self, broker: ExecutionBroker, ctx: ExecutionContext) -> None:
        self._broker = broker
        self.context = ctx

    def submit(self, tool: str, **params: object) -> ToolResult:
        return self._broker._dispatch(self.context, tool, params)
