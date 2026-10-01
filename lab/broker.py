"""The execution broker: the intended single path from a worker to anything real.

Every call is authorized by the policy engine against the tool's tier
from typing import Any
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

import asyncio
import contextlib
import errno
import hashlib
import itertools
import json
import logging
import math
import os
import shutil
import stat
import threading
import urllib.parse
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from lab import sandbox
from lab.connectors import Connector, ConnectorError
from lab.egress import EgressDenied, EgressGateway, parse_allowlist
from lab.journal import OperationJournal, operation_id
from lab.memory import MemoryRefused
from lab.policy import Decision, PolicyEngine, Tier, canonical
from lab.queue import LeaseToken
from lab.vault import Redactor, SecretUnavailable, Vault

LOG = logging.getLogger(__name__)


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


class PermanentFailure(RuntimeError):
    """Raised by a handler when no retry can succeed (bad input, a refusal
    that will not change). The task fails without being requeued (1.7).
    Works from a worker process too: the flag crosses the channel."""


class OutcomeUnknown(BrokerError):
    """A retry reached an operation whose earlier outcome nobody knows.

    Raised out of the session like ``ApprovalRequired``: the handler must
    not continue, and the supervisor holds the task until a person
    reconciles the operation (item 1.7).
    """

    def __init__(self, operation_id: str, tool: str) -> None:
        super().__init__(f"{tool} operation {operation_id[:12]} may already have run; "
                         "holding for reconciliation")
        self.operation_id = operation_id


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
    # Outbound GET through the egress gateway (item 4.3): default-deny host
    # list per task, resolve-then-pin, audited. The result is untrusted
    # evidence, never instructions.
    "net.fetch": Tier.NOTIFY,
    # One destination, one credential, injected by the broker (item 4.4).
    # Approve tier: a person sees the exact destination, path and body.
    "connector.call": Tier.APPROVE,
    # Stores a pending proposal and nothing else (#253). Notify tier: the
    # proposal grants nothing; the owner's signed decision does, later,
    # outside the broker.
    "memory.propose": Tier.NOTIFY,
}


# What running each tool again would do (item 1.7). Read-only and
# idempotent tools are simply rerun on a retry. Non-idempotent ones go
# through the operation journal and are never blindly repeated.
READ_ONLY, IDEMPOTENT, NON_IDEMPOTENT = "read_only", "idempotent", "non_idempotent"
TOOL_EFFECTS: dict[str, str] = {
    "fs.read": READ_ONLY,
    "fs.list": READ_ONLY,
    "fs.write": IDEMPOTENT,
    "fs.delete": IDEMPOTENT,
    # A command can do anything its sandbox allows, so assume the worst.
    "shell.run": NON_IDEMPOTENT,
    "net.fetch": IDEMPOTENT,        # a GET; retried freely, and gated by the host list
    "connector.call": NON_IDEMPOTENT,   # may change the outside world: journaled
    "memory.propose": IDEMPOTENT,   # the same text from the same task is one proposal
}

# Tools that work on the database. SQLite's one connection lives on the
# event loop's thread, so these run there rather than in a worker thread.
_ON_LOOP = frozenset({"memory.propose"})

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
    "net.fetch": {"url": (_is_str, True)},
    "connector.call": {"connector": (_is_str, True), "path": (_is_str, True),
                       "method": (_is_str, False), "body": (_is_str, False)},
    "memory.propose": {"text": (_is_str, True), "source": (_is_str, True),
                       "reason": (_is_str, True), "source_sha256": (_is_str, False)},
}


class InvalidParams(BrokerError):
    """The call's parameters do not match the tool's schema."""


def validate_params(tool: str, params: dict[str, Any]) -> None:
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
    params: dict[str, Any]
    task_id: str


@dataclass
class _CallRecord:
    """What one dispatch decided and returned, for its audit event."""

    decision: str = "refused"     # allow | needs_approval | refused
    result: ToolResult | None = None
    error: str | None = None

    def finish(self, result: ToolResult) -> ToolResult:
        self.result = result
        return result


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    tool: str
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


_PROTECTED_FOLDED = frozenset(name.casefold() for name in sandbox.PROTECTED_NAMES)


def _is_protected(parts: list[str]) -> bool:
    """Git hooks and shell start-up files run outside the sandbox later.

    Compared without regard to case: on the default macOS volume ``.GIT/hooks`` and
    ``.ZSHRC`` are the very same files (#215).
    """
    folded = [part.casefold() for part in parts]
    if folded and folded[-1] in _PROTECTED_FOLDED:
        return True
    return any(a == ".git" and b == "hooks" for a, b in itertools.pairwise(folded))


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
        journal: OperationJournal | None = None,
        egress: EgressGateway | None = None,
        vault: Vault | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root)
        self.workspace_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.policy = policy
        # Usually TaskQueue.owns_lease. None means no call can prove its
        # lease, so every call is refused: fail closed.
        self._leases = leases
        # Set by revoke(): an emergency stop takes authority away from
        # every session at once, before any work is cancelled (item 1.8).
        self._revoked = False
        # Cancel flags of the shell commands running right now, by task. A command
        # runs in a thread, which cannot be cancelled from outside, so a stop has
        # to reach it through this flag (#228).
        self._shell_lock = threading.Lock()
        self._shell_cancels: dict[str, set[threading.Event]] = {}
        # None means a non-idempotent tool cannot be journaled, so it is
        # refused: fail closed, like a missing policy engine.
        self._journal = journal
        # How many identical calls each claim has made so far, so the nth
        # identical call gets the same operation id on every retry.
        self._seq: dict[tuple[str, str], int] = {}
        # None means net.fetch always refuses: no gateway, no network.
        self._egress = egress
        # None means connector.call always refuses: no vault, no credentials.
        self._vault = vault
        self._connectors: dict[str, Connector] = {}
        self._task_connectors: dict[str, frozenset[str]] = {}
        self._egress_hosts: dict[str, frozenset[str]] = {}
        self._workspaces: dict[str, Workspace] = {}
        self._calls: dict[str, int] = {}
        self._grants: dict[str, set[str]] = {}

    # ------------------------------------------------------- lifecycle

    def open_workspace(self, task_id: str, allowed_tools: set[str],
                       egress_hosts: frozenset[str] | set[str] = frozenset(),
                       connectors: frozenset[str] | set[str] = frozenset()) -> Workspace:
        """Give a task its own directory and an explicit tool allowlist.

        The allowlist is per task and default-deny: a tool not named here
        is refused even if it exists. The same holds for the network:
        ``egress_hosts`` is the only list of hosts ``net.fetch`` may reach,
        empty by default, set here by trusted code and never by the task.
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
        self._egress_hosts[task_id] = parse_allowlist(egress_hosts)
        unknown_connectors = set(connectors) - set(self._connectors)
        if unknown_connectors:
            raise ToolNotAllowed(f"unknown connectors: {sorted(unknown_connectors)}")
        self._task_connectors[task_id] = frozenset(connectors)
        self._calls[task_id] = 0
        return ws

    def add_connector(self, connector: Connector) -> None:
        """Trusted registration of a destination. Not reachable from a task."""
        self._connectors[connector.name] = connector

    def close_workspace(self, task_id: str) -> None:
        ws = self._workspaces.pop(task_id, None)
        self._grants.pop(task_id, None)
        self._egress_hosts.pop(task_id, None)
        self._task_connectors.pop(task_id, None)
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
            "net.fetch": lambda req, ws: self._run_job_sync(self._job_net_fetch(req)),
            "connector.call": lambda req, ws: self._run_job_sync(self._job_connector_call(req)),
            "memory.propose": self._tool_memory_propose,
        }

    def session(self, ctx: ExecutionContext) -> ToolSession:
        """The handle a handler gets. Bound to one context, for its life."""
        return ToolSession(self, ctx)

    def revoke(self) -> None:
        """Refuse every call from every session from now on, and end the shell
        commands that are running (they are otherwise left to their timeout)."""
        self._revoked = True
        with self._shell_lock:
            for flags in self._shell_cancels.values():
                for flag in flags:
                    flag.set()

    def cancel_running(self, task_id: str) -> None:
        """End the shell commands this task is running. Called when its work is
        interrupted: a stop, a lost lease or the wall-clock ceiling."""
        with self._shell_lock:
            for flag in self._shell_cancels.get(task_id, ()):
                flag.set()

    def _prepare(self, ctx: ExecutionContext, tool: str, params: dict[str, Any],
                 ) -> tuple[Callable[[ToolRequest, Workspace], ToolResult],
                            ToolRequest, Workspace]:
        """Every check, in order, before anything runs.

        Live lease for this context, known tool, per-task grant, open
        workspace, parameter schema, call budget, then policy for this
        exact call. Raises ``ApprovalRequired`` or a ``BrokerError``.
        """
        request = ToolRequest(tool=tool, params=dict(params), task_id=ctx.task_id)
        self._check_context(ctx)
        handler = self._registry().get(request.tool)
        if handler is None or request.tool not in TOOL_TIERS:
            raise ToolNotAllowed(f"no such tool: {request.tool}")
        self._check_grant(request)
        ws = self._workspace_for(request.task_id)
        # Before policy: a human is never asked to approve a malformed
        # call, and a runaway loop stops without touching the audit log.
        validate_params(request.tool, request.params)
        if request.tool == "connector.call":
            # Before policy, so a person is never asked to approve a call
            # to a connector the task does not hold or a path it may not use.
            self._precheck_connector(request)
        used = self._calls.get(request.task_id, 0)
        if used >= MAX_CALLS_PER_TASK:
            raise QuotaExceeded(f"tool call ceiling {MAX_CALLS_PER_TASK} reached")
        self._calls[request.task_id] = used + 1
        self._authorize(request, ws)
        return handler, request, ws

    def _journal_begin(self, ctx: ExecutionContext, request: ToolRequest,
                       ) -> tuple[str | None, ToolResult | None]:
        """Decide, for a non-idempotent call, whether to run it at all.

        Returns (operation id, None) to run it, or (id, recorded result)
        to replay a confirmed outcome without running it again. Raises
        ``OutcomeUnknown`` for an operation that may already have run.
        """
        if TOOL_EFFECTS.get(request.tool, NON_IDEMPOTENT) != NON_IDEMPOTENT:
            return None, None
        if self._journal is None:
            raise PolicyUnavailable("no operation journal; refusing a non-idempotent call")
        params_sha = hashlib.sha256(canonical(request.params).encode("utf-8")).hexdigest()
        key = (ctx.lease.lease_id, f"{request.tool}:{params_sha}")
        seq = self._seq.get(key, 0)
        self._seq[key] = seq + 1
        op_id = operation_id(ctx.task_id, request.tool, params_sha, seq)
        prior = self._journal.get(op_id)
        if prior is not None and prior.state == "confirmed":
            result = prior.result or {}
            return op_id, ToolResult(ok=bool(result.get("ok", True)), tool=request.tool,
                                     detail={**result.get("detail", {}), "replayed": True},
                                     error=result.get("error"))
        if prior is not None and prior.state in ("executing", "uncertain"):
            if prior.state == "executing":
                self._journal.uncertain(op_id, ctx.task_id,
                                        "found executing on retry: the run died during it")
            raise OutcomeUnknown(op_id, request.tool)
        self._journal.begin(op_id, ctx.task_id, request.tool, params_sha, seq)
        return op_id, None

    def _journal_end(self, op_id: str | None, task_id: str,
                     result: ToolResult | None, exc: BaseException | None) -> None:
        if op_id is None or self._journal is None:
            return
        if result is not None:
            self._journal.confirm(op_id, task_id, {
                "ok": result.ok, "detail": result.detail, "error": result.error})
        elif isinstance(exc, BrokerError):
            # Refused by the tool before it had any effect.
            self._journal.failed(op_id, task_id, f"{type(exc).__name__}: {exc}")
        else:
            self._journal.uncertain(op_id, task_id,
                                    f"{type(exc).__name__}: {exc}" if exc else "unknown")

    def _dispatch(self, ctx: ExecutionContext, tool: str, params: dict[str, Any]) -> ToolResult:
        """The single entry point, run synchronously.

        Raises ``ApprovalRequired`` when a human must decide first; every
        other refusal is a failed ``ToolResult``. Every call, whatever its
        outcome, leaves one ``broker_call`` audit event (item 3.2).
        """
        call = _CallRecord()
        try:
            handler, request, ws = self._prepare(ctx, tool, params)
            call.decision = "allow"
            op_id, replay = self._journal_begin(ctx, request)
            if replay is not None:
                return call.finish(replay)
            try:
                result = handler(request, ws)
            except BaseException as exc:
                self._journal_end(op_id, ctx.task_id, None, exc)
                raise
            self._journal_end(op_id, ctx.task_id, result, None)
            return call.finish(result)
        except ApprovalRequired:
            call.decision = "needs_approval"
            raise
        except OutcomeUnknown as exc:
            call.error = type(exc).__name__
            raise
        except BrokerError as exc:
            call.error = type(exc).__name__
            return ToolResult(ok=False, tool=tool, error=f"{type(exc).__name__}: {exc}")
        except BaseException as exc:
            call.error = type(exc).__name__
            raise
        finally:
            self._audit_call(ctx, tool, params, call)

    async def _dispatch_async(self, ctx: ExecutionContext, tool: str,
                              params: dict[str, Any]) -> ToolResult:
        """Checks on the event loop, execution in a thread (item 1.8).

        Policy and the database stay on the loop's thread, where the one
        SQLite connection lives. The tool itself, which may be a command
        running for minutes, runs in a thread and touches no database, so
        the lease heartbeat keeps beating while it runs.
        """
        call = _CallRecord()
        try:
            handler, request, ws = self._prepare(ctx, tool, params)
            call.decision = "allow"
            op_id, replay = self._journal_begin(ctx, request)
            if replay is not None:
                return call.finish(replay)
            try:
                factory = self._net_job_factory(request.tool)
                if factory is not None:
                    job = factory(request)                 # database work, on the loop
                    try:
                        outcome = await asyncio.to_thread(job.perform)   # network only
                    finally:
                        self._flush_job(job)
                    result = job.finish(outcome)           # database work, on the loop
                elif request.tool in _ON_LOOP:
                    result = handler(request, ws)
                else:
                    result = await asyncio.to_thread(handler, request, ws)
            except BaseException as exc:
                # Includes cancellation: a command cut off mid-run has an
                # unknown outcome, and is recorded as such.
                self._journal_end(op_id, ctx.task_id, None, exc)
                raise
            self._journal_end(op_id, ctx.task_id, result, None)
            return call.finish(result)
        except ApprovalRequired:
            call.decision = "needs_approval"
            raise
        except OutcomeUnknown as exc:
            call.error = type(exc).__name__
            raise
        except BrokerError as exc:
            call.error = type(exc).__name__
            return ToolResult(ok=False, tool=tool, error=f"{type(exc).__name__}: {exc}")
        except BaseException as exc:
            call.error = type(exc).__name__
            raise
        finally:
            self._audit_call(ctx, tool, params, call)

    def _net_job_factory(self, tool: str) -> Callable[[ToolRequest], NetJob] | None:
        return {"net.fetch": self._job_net_fetch,
                "connector.call": self._job_connector_call}.get(tool)

    def _flush_job(self, job: NetJob) -> None:
        """Write the gateway events the thread buffered, on the loop's thread.
        A failure here is logged, not raised: the request already happened."""
        for kind, detail in job.audit_events:
            try:
                if self.policy is not None:
                    self.policy.audit(detail.get("task_id") or None, kind, detail)
            except Exception:
                LOG.exception("could not write the %s audit event", kind)
        job.audit_events.clear()

    def _run_job_sync(self, job: NetJob) -> ToolResult:
        try:
            outcome = job.perform()
        finally:
            self._flush_job(job)
        return job.finish(outcome)

    def _audit_call(self, ctx: ExecutionContext, tool: str, params: dict[str, Any],
                    call: _CallRecord) -> None:
        """One event per call: tool, parameter hash, lease generation, decision, result.

        Written after the fact, so a failure here must not turn a tool
        that already ran into an apparent failure (a retry could repeat
        it). It is logged loudly instead; the pre-execution decision
        event from the policy engine, which does fail closed, still
        exists for every allowed call.
        """
        if self.policy is None:
            return
        try:
            params_sha = hashlib.sha256(canonical(params).encode("utf-8")).hexdigest()
        except (TypeError, ValueError):
            params_sha = "unhashable"
        try:
            self.policy.audit(ctx.task_id, "broker_call", {
                "tool": tool,
                "params_sha256": params_sha,
                "lease_id": ctx.lease.lease_id,
                "lease_generation": ctx.lease.generation,
                "decision": call.decision,
                "ok": call.result.ok if call.result is not None else None,
                "error": call.error or (call.result.error if call.result else None),
            })
        except Exception:
            LOG.exception("could not write the broker_call audit event for %s", ctx.task_id)

    def _check_context(self, ctx: ExecutionContext) -> None:
        if self._revoked:
            raise ContextRevoked("broker authority revoked by an emergency stop")
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
            if request.tool == "connector.call":
                # A send is bound to where it goes and to the exact bytes it
                # carries, not to the workspace: editing the draft by one
                # character is a different intent and needs a new approval
                # (item 8.6). The reviewer sees both in the request.
                connector = self._connectors[request.params["connector"]]
                preconditions = {
                    "destination": connector.host,
                    "body_sha256": hashlib.sha256(
                        request.params.get("body", "").encode("utf-8")).hexdigest(),
                }
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

    # ---- network tools: prepare and finish on the loop, perform in a thread
    #
    # These two tools touch the database (write-ahead, taint, receipts) and
    # the network. SQLite's one connection lives on the loop's thread, so
    # the database work happens in ``_job_*`` (before) and ``job.finish``
    # (after), and only ``job.perform`` runs in a thread. It writes
    # nothing; the gateway's audit events are buffered and flushed by the
    # caller. The same code runs on the synchronous path.

    def _job_net_fetch(self, request: ToolRequest) -> NetJob:
        if self._egress is None:
            return NetJob.refused(request.tool, "EgressDenied: no egress gateway")
        gateway = self._egress
        hosts = self._egress_hosts.get(request.task_id, frozenset())
        url = request.params["url"]
        self._note_intent(request.task_id, url)
        job = NetJob(request.tool)

        def perform() -> Any:
            try:
                return gateway.fetch(url, hosts, request.task_id, audit=job.buffer)
            except EgressDenied as exc:
                return exc

        def finish(outcome: Any) -> ToolResult:
            if isinstance(outcome, EgressDenied):
                return ToolResult(False, request.tool, error=f"EgressDenied: {outcome}")
            if self.policy is not None:
                self.policy.taint(request.task_id, "read content fetched from the network")
            return ToolResult(True, request.tool, {
                "url": outcome.url, "status": outcome.status, "hops": outcome.hops,
                "content_type": outcome.content_type,
                "evidence": outcome.evidence.as_payload(),
            })

        job.perform, job.finish = perform, finish
        return job

    def _note_intent(self, task_id: str, url: str) -> None:
        """Recorded before anything goes out, and fail-closed: if the audit
        write fails the request is never made."""
        if self.policy is None:
            return
        parts = urllib.parse.urlsplit(url)
        self.policy.audit(task_id, "egress_intent", {
            "host": (parts.hostname or "")[:255],
            "url_sha256": hashlib.sha256(url.encode("utf-8", "replace")).hexdigest()})

    def _precheck_connector(self, request: ToolRequest) -> Connector:
        params = request.params
        name = params["connector"]
        if name not in self._task_connectors.get(request.task_id, frozenset()):
            raise ToolNotAllowed(f"task {request.task_id} has no grant for connector {name!r}")
        connector = self._connectors[name]
        try:
            connector.check_call(params.get("method", "POST"), params["path"],
                                 params.get("body"))
        except ConnectorError as exc:
            raise InvalidParams(str(exc)) from None
        return connector

    def _job_connector_call(self, request: ToolRequest) -> NetJob:
        params = request.params
        name = params["connector"]
        connector = self._precheck_connector(request)
        method = params.get("method", "POST")
        body = params.get("body")
        if self._egress is None or self._vault is None:
            return NetJob.refused(request.tool, "no egress gateway or vault")
        try:
            secret = self._vault.resolve(connector.secret)
        except SecretUnavailable as exc:
            return NetJob.refused(request.tool, f"SecretUnavailable: {exc}")
        gateway = self._egress
        redactor = Redactor([secret])
        body_sha = hashlib.sha256((body or "").encode("utf-8")).hexdigest()
        params_sha = hashlib.sha256(canonical(params).encode("utf-8")).hexdigest()
        key = hashlib.sha256(
            f"{request.task_id}\n{name}\n{method}\n{params['path']}\n{body_sha}".encode()
        ).hexdigest()[:40]
        headers = {connector.header: connector.header_value(secret)}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if connector.idempotency_header:
            headers[connector.idempotency_header] = key
        url = f"https://{connector.host}{params['path']}"
        # Write-ahead: the attempt is on record before a byte leaves, so a
        # lost response or a crash can be reconciled against the provider.
        if self.policy is not None:
            self.policy.reserve_publication(
                request.task_id, name, connector.host, method, params["path"], body_sha,
                params_sha, key)
        self._note_intent(request.task_id, url)
        job = NetJob(request.tool)

        def perform() -> Any:
            try:
                return gateway.fetch(
                    url, frozenset({connector.host}), request.task_id, method=method,
                    headers=headers, body=body.encode("utf-8") if body is not None else None,
                    follow_redirects=False, audit=job.buffer)
            except EgressDenied as exc:
                return exc

        def finish(outcome: Any) -> ToolResult:
            if isinstance(outcome, EgressDenied):
                return ToolResult(False, request.tool,
                                  error=str(redactor.scrub(f"EgressDenied: {outcome}")))
            if self.policy is not None:
                self.policy.taint(request.task_id, "read a response from a connector")
                self.policy.confirm_publication(
                    key, outcome.status, _provider_id(connector, outcome),
                    outcome.evidence.sha256, "response")
            return ToolResult(True, request.tool, redactor.scrub({
                "connector": name, "status": outcome.status, "idempotency_key": key,
                "evidence": outcome.evidence.as_payload(),
            }))

        job.perform, job.finish = perform, finish
        return job

    def _tool_shell_run(self, request: ToolRequest, ws: Workspace) -> ToolResult:
        """Run a command, confined by the kernel rather than by us.

        Refuses outright if OS-level isolation is unavailable. Running a
        command unconfined because the sandbox was missing would be the
        exact failure this tool exists to prevent.
        """
        argv = request.params["argv"]

        cancel = threading.Event()
        with self._shell_lock:
            if self._revoked:
                cancel.set()             # revoked between the check and now
            self._shell_cancels.setdefault(request.task_id, set()).add(cancel)
        try:
            result = sandbox.run(
                argv, ws.root,
                timeout=float(request.params.get("timeout", 30.0)),
                cancel=cancel,
            )
        except sandbox.SandboxUnavailable as exc:
            raise BrokerError(f"refusing to run unconfined: {exc}") from exc
        finally:
            with self._shell_lock:
                flags = self._shell_cancels.get(request.task_id)
                if flags is not None:
                    flags.discard(cancel)
                    if not flags:
                        del self._shell_cancels[request.task_id]

        return ToolResult(
            ok=result.ok,
            tool=request.tool,
            detail={"stdout": result.stdout, "stderr": result.stderr,
                    "returncode": result.returncode,
                    "sandbox_denied": result.denied,
                    "timed_out": result.timed_out,
                    "cancelled": result.cancelled,
                    "truncated": result.truncated},
            error=None if result.ok else result.stderr.strip() or "failed",
        )

    def _tool_memory_propose(self, request: ToolRequest, ws: Workspace) -> ToolResult:
        """Store a pending proposal (#253). It is never searched and never
        active; only the owner's signed decision, outside the broker, can
        make it curated memory. The taint mark is read from the task's row,
        so nothing the handler says can clear it."""
        assert self.policy is not None, "_authorize refuses every call without policy"
        params = request.params
        try:
            row = self.policy.propose_memory(request.task_id, params["text"],
                                             params["source"], params["reason"],
                                             params.get("source_sha256"))
        except MemoryRefused as exc:
            raise InvalidParams(f"memory.propose: {exc}") from None
        return ToolResult(True, request.tool, {
            "proposal": row["id"], "state": row["state"], "tainted": bool(row["tainted"]),
            "text_sha256": row["text_sha256"]})

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

    def manifest(self, task_id: str) -> dict[str, Any]:
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


@dataclass
class NetJob:
    """A network tool call split at the thread boundary.

    ``perform`` runs in a thread and must not touch the database. ``finish``
    and the buffered audit events are handled on the loop's thread.
    """

    tool: str
    perform: Callable[[], Any] = field(default=lambda: None)
    finish: Callable[[Any], ToolResult] = field(default=lambda outcome: outcome)
    audit_events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def buffer(self, kind: str, detail: dict[str, Any]) -> None:
        self.audit_events.append((kind, detail))

    @classmethod
    def refused(cls, tool: str, error: str) -> NetJob:
        return cls(tool, perform=lambda: None,
                   finish=lambda _outcome: ToolResult(False, tool, error=error))


def _provider_id(connector: Connector, fetched: Any) -> str | None:
    """The provider's own id for what was created, if the response says."""
    if not connector.receipt_field or fetched.evidence.truncated \
            or not 200 <= fetched.status < 300:
        return None
    try:
        value = json.loads(fetched.evidence.excerpt).get(connector.receipt_field)
    except (ValueError, AttributeError):
        return None
    return str(value)[:200] if isinstance(value, str | int) and not isinstance(value, bool) \
        else None


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

    async def submit_async(self, tool: str, **params: object) -> ToolResult:
        """Same checks, but the tool runs off the event loop."""
        return await self._broker._dispatch_async(self.context, tool, params)
