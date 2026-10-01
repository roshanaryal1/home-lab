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
import re
import shutil
import stat
import threading
import urllib.parse
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from lab import container as containers
from lab import sandbox, skills
from lab.connectors import Connector, ConnectorError
from lab.container import ContainerExecutor, ContainerResult, ContainerUnavailable
from lab.egress import EgressDenied, EgressGateway, parse_allowlist
from lab.journal import OperationJournal, operation_id
from lab.mcp import MAX_ARGUMENT_BYTES as MCP_MAX_ARGUMENT_BYTES
from lab.mcp import MAX_CALLS_PER_TASK as MCP_MAX_CALLS_PER_TASK
from lab.mcp import (
    CallOutcome,
    McpCancelled,
    McpError,
    McpRefused,
    McpRegistry,
    McpTimeout,
    ServerSpec,
    argument_bytes,
)
from lab.memory import MemoryRefused
from lab.policy import Decision, PolicyEngine, Tier, canonical
from lab.queue import LeaseToken
from lab.skillstore import SkillStore, SkillStoreError
from lab.untrusted import MAX_LIMIT, Evidence, clean, extract_evidence
from lab.vault import Redactor, SecretUnavailable, Vault

LOG = logging.getLogger(__name__)


class BrokerError(RuntimeError):
    """Base for every refusal the broker makes."""


class PathEscape(BrokerError):
    """A request tried to reach outside its workspace."""


class UnsafeRepository(BrokerError):
    """A repository that git must not be run on: it reaches outside the
    workspace, or its configuration could make git run something."""


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


class SkillRefused(BrokerError):
    """skill.run cannot run this script safely, so it does not run at all (#255)."""


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
    # Read-only search of the task's own files. Autonomous like fs.read: it
    # walks by descriptor, never follows a symlink and never opens anything
    # but a regular file, so it cannot see outside the workspace.
    "fs.search": Tier.AUTONOMOUS,
    # Read-only git on a repository inside the workspace (#240). Autonomous
    # because nothing a repository holds can make these run code or reach
    # outside: the repository is checked before git runs (no symlinks, no
    # git directory or object store outside, only reviewed config keys), the
    # command line is fixed, hooks, fsmonitor, pagers and every transport
    # are off, and output is capped. SECURITY.md gives the reasoning.
    "git.status": Tier.AUTONOMOUS,
    "git.log": Tier.AUTONOMOUS,
    "git.diff": Tier.AUTONOMOUS,
    # net.fetch, then the bounded model summarizes the fetched Evidence in
    # the broker. Same tier as net.fetch: the same request, the same gateway.
    "net.summarize": Tier.NOTIFY,
    # Stores a pending proposal and nothing else (#253). Notify tier: the
    # proposal grants nothing; the owner's signed decision does, later,
    # outside the broker.
    "memory.propose": Tier.NOTIFY,
    # Runs a script from the active version of a skill, only inside a
    # disposable container (#255, ADR 0007). Approve tier: the code is not
    # ours, and a person sees the skill, the version and the exact script.
    "skill.run": Tier.APPROVE,
    # One tool of an operator-signed MCP server (#256). Approve tier: the
    # server is a program the lab did not write, and a person sees the exact
    # server, tool and arguments. The approval is also bound to the signed
    # entry and the tool's signed fingerprint, so a re-signed server needs a
    # new approval.
    "mcp.call": Tier.APPROVE,
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
    "fs.search": READ_ONLY,
    "git.status": READ_ONLY,     # GIT_OPTIONAL_LOCKS=0, so status does not rewrite the index
    "git.log": READ_ONLY,
    "git.diff": READ_ONLY,
    "net.summarize": IDEMPOTENT,    # a GET and a local model call
    "memory.propose": IDEMPOTENT,   # the same text from the same task is one proposal
    "skill.run": NON_IDEMPOTENT,    # untrusted code can do anything to the workspace
    "mcp.call": NON_IDEMPOTENT,     # a server tool can do anything: journaled
}

# Tools that work on the database. SQLite's one connection lives on the
# event loop's thread, so these run there rather than in a worker thread.
_ON_LOOP = frozenset({"memory.propose"})

# Per-task and per-call ceilings (item 1.10). A request can lower these
# where a parameter allows it, never raise them.
MAX_CALLS_PER_TASK = 1000
MAX_READ_BYTES = 1024 * 1024
MAX_LIST_ENTRIES = 1000
MAX_SEARCH_MATCHES = 200
MAX_SEARCH_BYTES = 16 * 1024 * 1024     # read in total by one search
MAX_PATTERN_CHARS = 200
MAX_MATCH_CHARS = 300                   # of each matching line returned

# Read-only git (#240).
GIT_MAX_OUTPUT = 256 * 1024             # per stream
# The index listing is checked inside the broker and never returned, so it
# may be larger than what the model sees; this bounds memory only.
GIT_MAX_INDEX_BYTES = 16 * 1024 * 1024
GIT_TIMEOUT_SECONDS = 30.0
GIT_LOG_ENTRIES = 50
GIT_MAX_ENTRIES = 50_000                # files and directories checked in one git directory
GIT_MAX_CONFIG_BYTES = 64 * 1024
GIT_PATH = "/usr/bin:/bin"

# skill.run (#255). The timeout and output ceilings are the container tier's.
MAX_SKILL_ARGS = 32
MAX_SKILL_ARG_CHARS = 1024
SKILL_DEFAULT_TIMEOUT = 30.0
MAX_SKILL_OUTPUT_CHARS = containers.MAX_OUTPUT_BYTES
SKILL_RUN_PREFIX = "skill-run-"

# What each tool accepts: field -> (validator, required). Unknown fields
# are refused, so a model cannot pass options the broker never reviewed.
_Validator = Callable[[object], bool]


def _is_str(v: object) -> bool:
    return isinstance(v, str)


def _is_argv(v: object) -> bool:
    return isinstance(v, list) and bool(v) and all(isinstance(a, str) and "\0" not in a
                                                   for a in v)


def _is_str_list(v: object) -> bool:
    """A list of strings, possibly empty, none holding a NUL byte."""
    return isinstance(v, list) and all(isinstance(a, str) and "\0" not in a for a in v)


def _is_timeout(v: object) -> bool:
    return (isinstance(v, int | float) and not isinstance(v, bool)
            and math.isfinite(v) and v > 0)


def _is_json_object(v: object) -> bool:
    """A plain JSON object of bounded size: what an MCP tool takes as arguments."""
    if not isinstance(v, dict):
        return False
    size = argument_bytes(v)
    return size is not None and size <= MCP_MAX_ARGUMENT_BYTES


TOOL_SCHEMAS: dict[str, dict[str, tuple[_Validator, bool]]] = {
    "fs.read": {"path": (_is_str, True)},
    "fs.list": {"path": (_is_str, False)},
    "fs.write": {"path": (_is_str, True), "content": (_is_str, True)},
    "fs.delete": {"path": (_is_str, True)},
    "shell.run": {"argv": (_is_argv, True), "timeout": (_is_timeout, False)},
    "net.fetch": {"url": (_is_str, True)},
    "connector.call": {"connector": (_is_str, True), "path": (_is_str, True),
                       "method": (_is_str, False), "body": (_is_str, False)},
    "fs.search": {"pattern": (_is_str, True), "path": (_is_str, False)},
    "git.status": {"repo": (_is_str, False)},
    "git.log": {"repo": (_is_str, False)},
    "git.diff": {"repo": (_is_str, False)},
    "net.summarize": {"url": (_is_str, True)},
    "memory.propose": {"text": (_is_str, True), "source": (_is_str, True),
                       "reason": (_is_str, True), "source_sha256": (_is_str, False)},
    "skill.run": {"skill": (_is_str, True), "script": (_is_str, True),
                  "args": (_is_str_list, False), "timeout": (_is_timeout, False)},
    # ``name`` is the server's tool, as in MCP's own tools/call. It cannot be
    # called ``tool``: that is the session's own argument (``submit(tool, ...)``).
    "mcp.call": {"server": (_is_str, True), "name": (_is_str, True),
                 "arguments": (_is_json_object, False)},
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
# O_NONBLOCK so a FIFO swapped in after the type check cannot hang the call.
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


@contextlib.contextmanager
def _walk(dir_fd: int) -> Iterator[Iterator[tuple[str, list[str], list[str], int]]]:
    """Walk below ``dir_fd`` by descriptor without following symlinks.

    Closed on the way out, so stopping early leaves no directory open.
    """
    walker = os.fwalk(".", dir_fd=dir_fd, follow_symlinks=False)
    try:
        yield walker
    finally:
        close = getattr(walker, "close", None)
        if close is not None:
            close()


def _open_regular(parent: int, name: str) -> int | None:
    """Open ``name`` in ``parent`` for reading if it is a regular file.

    Returns None for a missing file or anything that is not a regular
    file. The type is checked before opening, so a device or a FIFO is
    never opened at all, and again by descriptor after, so a swap in
    between is caught. A symlink is a ``PathEscape``.
    """
    try:
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(before.st_mode):
        raise PathEscape(f"{name!r} is a symlink")
    if not stat.S_ISREG(before.st_mode):
        return None
    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=parent)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PathEscape(f"{name!r} is a symlink") from None
        return None
    after = os.fstat(fd)
    if not stat.S_ISREG(after.st_mode) or (after.st_dev, after.st_ino) != (
            before.st_dev, before.st_ino):
        os.close(fd)
        return None
    return fd


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
        skills: SkillStore | None = None,
        container: ContainerExecutor | None = None,
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
        # None means net.summarize always refuses: no model, no summary.
        self._summarizer: Callable[[Evidence], str] | None = None
        # Both or nothing: skill.run refuses unless a skill store and a
        # container executor with a digest-pinned image are configured.
        self._skills = skills
        self._container = container
        self._connectors: dict[str, Connector] = {}
        self._task_connectors: dict[str, frozenset[str]] = {}
        self._egress_hosts: dict[str, frozenset[str]] = {}
        # None means mcp.call always refuses: no signed servers (#256).
        self._mcp: McpRegistry | None = None
        self._task_mcp: dict[str, frozenset[str]] = {}
        self._mcp_calls: dict[str, int] = {}
        self._workspaces: dict[str, Workspace] = {}
        self._calls: dict[str, int] = {}
        self._grants: dict[str, set[str]] = {}

    # ------------------------------------------------------- lifecycle

    def open_workspace(self, task_id: str, allowed_tools: set[str],
                       egress_hosts: frozenset[str] | set[str] = frozenset(),
                       connectors: frozenset[str] | set[str] = frozenset(),
                       mcp_servers: frozenset[str] | set[str] = frozenset()) -> Workspace:
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
        known_mcp = set(self._mcp.names) if self._mcp is not None else set()
        unknown_mcp = set(mcp_servers) - known_mcp
        if unknown_mcp:
            raise ToolNotAllowed(f"unknown MCP servers: {sorted(unknown_mcp)}")
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
        self._task_mcp[task_id] = frozenset(mcp_servers)
        self._mcp_calls[task_id] = 0
        self._calls[task_id] = 0
        return ws

    def add_connector(self, connector: Connector) -> None:
        """Trusted registration of a destination. Not reachable from a task."""
        self._connectors[connector.name] = connector

    def set_mcp(self, registry: McpRegistry) -> None:
        """Trusted registration of the operator's signed MCP servers (#256)."""
        self._mcp = registry

    @property
    def mcp_registry(self) -> McpRegistry | None:
        """The registry ``set_mcp`` installed, for trusted registration code."""
        return self._mcp

    def set_summarizer(self, summarizer: Callable[[Evidence], str]) -> None:
        """Trusted registration of the bounded model behind net.summarize.

        It is given fixed-schema ``Evidence`` and returns one summary
        string, or raises. A ``PermanentFailure`` (a reply that is not
        exactly one summary) is final; anything else may be retried.
        """
        self._summarizer = summarizer

    def set_skill_runner(self, skills: SkillStore, container: ContainerExecutor) -> None:
        """Trusted registration of what skill.run needs (#255): the skill
        store it reads the active version from, and the container executor
        it runs in. Never reachable from a task."""
        self._skills = skills
        self._container = container

    def close_workspace(self, task_id: str) -> None:
        ws = self._workspaces.pop(task_id, None)
        self._grants.pop(task_id, None)
        self._egress_hosts.pop(task_id, None)
        self._task_connectors.pop(task_id, None)
        self._task_mcp.pop(task_id, None)
        self._mcp_calls.pop(task_id, None)
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
            "fs.search": self._tool_fs_search,
            "git.status": self._tool_git,
            "git.log": self._tool_git,
            "git.diff": self._tool_git,
            "net.summarize": lambda req, ws: self._run_job_sync(self._job_net_summarize(req)),
            "memory.propose": self._tool_memory_propose,
            "skill.run": lambda req, ws: self._run_job_sync(self._job_skill_run(req)),
            "mcp.call": lambda req, ws: self._run_job_sync(self._job_mcp_call(req)),
        }

    def session(self, ctx: ExecutionContext) -> ToolSession:
        """The handle a handler gets. Bound to one context, for its life."""
        return ToolSession(self, ctx)

    def revoke(self) -> None:
        """Refuse every call from every session from now on, and end the shell
        commands and MCP servers that are running (they are otherwise left to
        their timeout)."""
        self._revoked = True
        with self._shell_lock:
            for flags in self._shell_cancels.values():
                for flag in flags:
                    flag.set()

    def cancel_running(self, task_id: str) -> None:
        """End the shell commands and MCP servers this task is running. Called
        when its work is interrupted: a stop, a lost lease or the wall-clock
        ceiling."""
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
        if request.tool == "skill.run":
            # Before policy, like a connector: a person is never asked to
            # approve a script that would be refused anyway.
            self._precheck_skill(request)
        if request.tool == "mcp.call":
            # The same for MCP: a server the task does not hold, an unsigned
            # server or a tool off the signed allowlist never reaches a person.
            self._precheck_mcp(request)
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
                "net.summarize": self._job_net_summarize,
                "connector.call": self._job_connector_call,
                "skill.run": self._job_skill_run,
                "mcp.call": self._job_mcp_call}.get(tool)

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
            active = (self._skills.active(request.params["skill"])
                      if request.tool == "skill.run" and self._skills is not None else None)
            if preconditions is not None and active is not None:
                # The grant names the exact version and content, so a promotion
                # or a rollback after review voids it (#255).
                preconditions.update({"skill_version": str(active["version"]),
                                      "skill_sha256": active["content_sha256"]})
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
            if request.tool == "mcp.call":
                # Bound to the signed entry and to the tool as signed, so a
                # server the operator re-signs needs a new approval.
                spec = self._precheck_mcp(request)
                preconditions = {**(preconditions or {}), "server_sha256": spec.digest(),
                                 "tool_sha256": spec.tools[request.params["name"]]}
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
            fd = _open_regular(parent, parts[-1])
            if fd is None:
                return ToolResult(False, request.tool, error="not a file")
            with os.fdopen(fd, "rb") as fh:
                info = os.fstat(fh.fileno())
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
            # Checked before opening: O_TRUNC on a device, or a write to a
            # FIFO, must never happen. O_NONBLOCK covers a swap after this.
            with contextlib.suppress(FileNotFoundError):
                existing = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                if stat.S_ISLNK(existing.st_mode):
                    raise PathEscape(f"{parts[-1]!r} is a symlink")
                if not stat.S_ISREG(existing.st_mode):
                    raise BrokerError("target is not a regular file")
            try:
                fd = os.open(parts[-1],
                             os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
                             | os.O_NONBLOCK, 0o600, dir_fd=parent)
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise PathEscape(f"{parts[-1]!r} is a symlink") from None
                raise BrokerError(f"cannot write {parts[-1]!r}: {exc.strerror}") from None
            with os.fdopen(fd, "wb") as fh:
                if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                    raise BrokerError("target is not a regular file")
                fh.write(content.encode("utf-8"))
        return ToolResult(True, request.tool, {"bytes": len(content)})

    def _tool_fs_search(self, request: ToolRequest, ws: Workspace) -> ToolResult:
        """Find lines containing a literal string, under a workspace directory.

        A literal, not a regular expression, so a pattern cannot be made to
        run for ever. The walk is by descriptor and never follows a symlink;
        only regular files are opened, as in fs.read. ``.git`` directories
        are skipped. Every limit is reported in the result, not hidden.
        """
        pattern = request.params["pattern"]
        if not pattern or len(pattern) > MAX_PATTERN_CHARS or "\n" in pattern:
            raise InvalidParams(
                f"fs.search: pattern must be 1 to {MAX_PATTERN_CHARS} characters on one line")
        start = request.params.get("path", ".")
        ws.resolve(start)
        parts = ws.parts(start)
        prefix = PurePosixPath(*parts) if parts else PurePosixPath()
        matches: list[dict[str, Any]] = []
        skipped: dict[str, int] = {"symlink": 0, "not_regular": 0, "too_large": 0, "binary": 0}
        scanned = files = 0
        truncated = False
        with contextlib.ExitStack() as stack:
            try:
                top = stack.enter_context(ws.dir_fd(parts))
            except FileNotFoundError:
                return ToolResult(False, request.tool, error="not a directory")
            walker = stack.enter_context(_walk(top))
            for dirpath, dirnames, filenames, dfd in walker:
                dirnames[:] = sorted(d for d in dirnames if d.casefold() != ".git")
                for name in sorted(filenames):
                    if truncated:
                        break
                    files += 1
                    if files > ws.max_files:
                        truncated = True
                        break
                    try:
                        fd = _open_regular(dfd, name)
                    except PathEscape:
                        skipped["symlink"] += 1
                        continue
                    if fd is None:
                        skipped["not_regular"] += 1
                        continue
                    with os.fdopen(fd, "rb") as fh:
                        size = os.fstat(fh.fileno()).st_size
                        if size > MAX_READ_BYTES:
                            skipped["too_large"] += 1
                            continue
                        if scanned + size > MAX_SEARCH_BYTES:
                            truncated = True
                            break
                        data = fh.read(MAX_READ_BYTES)
                    scanned += len(data)
                    if b"\0" in data:
                        skipped["binary"] += 1
                        continue
                    rel = str(prefix / PurePosixPath(dirpath) / name)
                    text = data.decode("utf-8", errors="replace")
                    for number, line in enumerate(text.splitlines(), start=1):
                        if pattern in line:
                            if len(matches) >= MAX_SEARCH_MATCHES:
                                truncated = True
                                break
                            matches.append({"path": rel, "line": number,
                                            "text": clean(line)[:MAX_MATCH_CHARS]})
                if truncated:
                    break
        return ToolResult(True, request.tool, {
            "matches": matches, "truncated": truncated, "files_scanned": min(files, ws.max_files),
            "bytes_scanned": scanned, "skipped": skipped})

    # ---- read-only git (#240)
    #
    # git is not run in the sandbox: on Linux there is none, and the tool
    # must not depend on one to be safe. Instead nothing the repository
    # holds can choose what runs. The repository is checked first
    # (``_git_repository``), the command line is fixed and never takes a
    # value from the request, configuration from the system and the home
    # directory is switched off, and every config key that can name a
    # program is either refused in the repository or overridden here.

    def _tool_git(self, request: ToolRequest, ws: Workspace) -> ToolResult:
        git = shutil.which("git", path=GIT_PATH)
        if git is None:
            raise BrokerError("git is not installed")
        worktree, gitdir = _git_repository(ws, request.params.get("repo", "."))
        env = _git_environment(ws.root, worktree, gitdir)
        base = [git, *_GIT_SAFETY]
        commands = {
            "git.status": ["status", "--porcelain=v1", "--branch",
                           "--untracked-files=normal", "--ignore-submodules=all"],
            "git.log": ["log", "--no-show-signature", "--no-color", "--no-decorate",
                        "--no-mailmap", f"--max-count={GIT_LOG_ENTRIES}",
                        "--format=%H%x09%an%x09%aI%x09%s"],
            "git.diff": ["diff", "--no-ext-diff", "--no-textconv", "--no-color",
                         "--ignore-submodules=all", "HEAD", "--"],
        }
        with self._cancel_flag(request.task_id) as cancel:
            if request.tool in ("git.status", "git.diff"):
                # These two look at the work tree through the index, so the
                # index must name nothing outside it.
                listed = sandbox._execute([*base, "ls-files", "-z", "--cached"],
                                          cwd=str(worktree), env=env,
                                          timeout=GIT_TIMEOUT_SECONDS, cap=GIT_MAX_INDEX_BYTES,
                                          cancel=cancel)
                if listed.truncated or listed.timed_out:
                    raise UnsafeRepository("the index is too large to check")
                if listed.returncode != 0:
                    return _git_result(request.tool, listed)
                _check_index_paths(listed.stdout)
            done = sandbox._execute([*base, *commands[request.tool]], cwd=str(worktree),
                                    env=env, timeout=GIT_TIMEOUT_SECONDS, cap=GIT_MAX_OUTPUT,
                                    cancel=cancel)
        return _git_result(request.tool, done)

    # ---- network tools: prepare and finish on the loop, perform in a thread
    #
    # These two tools touch the database (write-ahead, taint, receipts) and
    # the network. SQLite's one connection lives on the loop's thread, so
    # the database work happens in ``_job_*`` (before) and ``job.finish``
    # (after), and only ``job.perform`` runs in a thread. It writes
    # nothing; the gateway's audit events are buffered and flushed by the
    # caller. The same code runs on the synchronous path.

    def _job_net_fetch(self, request: ToolRequest, *, summarize: bool = False) -> NetJob:
        if self._egress is None:
            return NetJob.refused(request.tool, "EgressDenied: no egress gateway")
        summarizer = self._summarizer
        if summarize and summarizer is None:
            return NetJob.refused(request.tool, "no summarizer model is configured")
        gateway = self._egress
        hosts = self._egress_hosts.get(request.task_id, frozenset())
        url = request.params["url"]
        self._note_intent(request.task_id, url)
        job = NetJob(request.tool)

        def perform() -> Any:
            try:
                fetched = gateway.fetch(url, hosts, request.task_id, audit=job.buffer)
            except EgressDenied as exc:
                return exc
            if summarizer is None or not summarize:
                return fetched
            # The model sees the fixed-schema Evidence only, never the raw
            # body. Its failure is reported, not raised: the fetch happened.
            try:
                return fetched, summarizer(fetched.evidence)
            except Exception as exc:
                return fetched, exc

        def finish(outcome: Any) -> ToolResult:
            if isinstance(outcome, EgressDenied):
                return ToolResult(False, request.tool, error=f"EgressDenied: {outcome}")
            if self.policy is not None:
                self.policy.taint(request.task_id, "read content fetched from the network")
            fetched, summary = outcome if summarize else (outcome, None)
            detail = {
                "url": fetched.url, "status": fetched.status, "hops": fetched.hops,
                "content_type": fetched.content_type,
                "evidence": fetched.evidence.as_payload(),
            }
            if isinstance(summary, Exception):
                return ToolResult(False, request.tool,
                                  {**detail, "permanent": isinstance(summary, PermanentFailure)},
                                  error=f"{type(summary).__name__}: {summary}")
            if summarize:
                detail["summary"] = summary
            return ToolResult(True, request.tool, detail)

        job.perform, job.finish = perform, finish
        return job

    def _job_net_summarize(self, request: ToolRequest) -> NetJob:
        return self._job_net_fetch(request, summarize=True)

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

    def _precheck_mcp(self, request: ToolRequest) -> ServerSpec:
        params = request.params
        server = params["server"]
        if self._mcp is None:
            raise ToolNotAllowed("no MCP servers are configured")
        if server not in self._task_mcp.get(request.task_id, frozenset()):
            raise ToolNotAllowed(
                f"task {request.task_id} has no grant for MCP server {server!r}")
        if self._mcp_calls.get(request.task_id, 0) >= MCP_MAX_CALLS_PER_TASK:
            raise QuotaExceeded(f"MCP call ceiling {MCP_MAX_CALLS_PER_TASK} reached")
        try:
            return self._mcp.check_call(server, params["name"], params.get("arguments", {}),
                                        self._egress_hosts.get(request.task_id, frozenset()))
        except McpRefused as exc:
            raise ToolNotAllowed(str(exc)) from None

    def _job_mcp_call(self, request: ToolRequest) -> NetJob:
        """One call to one MCP server tool (#256).

        The server runs in a thread like a shell command, under the same cancel
        flag, so a stop or a revoke kills its process group. Whatever it returns,
        and the tool's own description, reach the caller only as Evidence, and
        the task is tainted, because a server's words are someone else's.
        """
        registry = self._mcp
        if registry is None:
            return NetJob.refused(request.tool, "no MCP servers are configured")
        self._precheck_mcp(request)
        task_id = request.task_id
        ws = self._workspace_for(task_id)
        hosts = self._egress_hosts.get(task_id, frozenset())
        server, tool = request.params["server"], request.params["name"]
        arguments = request.params.get("arguments", {})
        self._mcp_calls[task_id] = self._mcp_calls.get(task_id, 0) + 1
        job = NetJob(request.tool)

        def perform() -> Any:
            with self._cancel_flag(task_id) as cancel:
                try:
                    return registry.call(server, tool, arguments, ws.root,
                                         egress_hosts=hosts, cancel=cancel)
                except McpError as exc:
                    return exc

        def finish(outcome: Any) -> ToolResult:
            if self.policy is not None:
                self.policy.taint(task_id, "ran an MCP server tool")
            source = f"mcp:{server}/{tool}"
            if isinstance(outcome, McpError):
                return ToolResult(False, request.tool, {
                    "server": server, "tool": tool,
                    "refused": isinstance(outcome, McpRefused),
                    "timed_out": isinstance(outcome, McpTimeout),
                    "cancelled": isinstance(outcome, McpCancelled),
                }, error=f"{type(outcome).__name__}: {clean(str(outcome))[:500]}")
            assert isinstance(outcome, CallOutcome)
            return ToolResult(not outcome.is_error, request.tool, {
                "server": server, "tool": tool, "is_error": outcome.is_error,
                "evidence": extract_evidence(outcome.text, source_type="document",
                                             source_id=source, limit=MAX_LIMIT).as_payload(),
                "description": extract_evidence(outcome.description, source_type="document",
                                                source_id=f"{source}#description").as_payload(),
            }, error="the MCP tool reported an error" if outcome.is_error else None)

        job.perform, job.finish = perform, finish
        return job

    @contextlib.contextmanager
    def _cancel_flag(self, task_id: str) -> Iterator[threading.Event]:
        """A flag a stop or a revoke sets to end this task's running command."""
        cancel = threading.Event()
        with self._shell_lock:
            if self._revoked:
                cancel.set()             # revoked between the check and now
            self._shell_cancels.setdefault(task_id, set()).add(cancel)
        try:
            yield cancel
        finally:
            with self._shell_lock:
                flags = self._shell_cancels.get(task_id)
                if flags is not None:
                    flags.discard(cancel)
                    if not flags:
                        del self._shell_cancels[task_id]

    def _tool_shell_run(self, request: ToolRequest, ws: Workspace) -> ToolResult:
        """Run a command, confined by the kernel rather than by us.

        Refuses outright if OS-level isolation is unavailable. Running a
        command unconfined because the sandbox was missing would be the
        exact failure this tool exists to prevent.
        """
        argv = request.params["argv"]

        with self._cancel_flag(request.task_id) as cancel:
            try:
                result = sandbox.run(
                    argv, ws.root,
                    timeout=float(request.params.get("timeout", 30.0)),
                    cancel=cancel,
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
                    "cancelled": result.cancelled,
                    "truncated": result.truncated},
            error=None if result.ok else result.stderr.strip() or "failed",
        )

    # ---- skill.run (#255): an active skill's script, only in a container
    #
    # Every check runs on the loop's thread before anything starts: in
    # ``_prepare`` (so a person is never asked to approve a refusal) and again
    # right before the install. Only the container run itself is in a thread.

    def _precheck_skill(self, request: ToolRequest) -> tuple[Any, str]:
        """The active version and the script's manifest path, or a refusal."""
        if self._skills is None or self._container is None:
            raise SkillRefused("skill.run is off: no container image is configured "
                               "(set LAB_CONTAINER_IMAGE to an image pinned by digest)")
        params = request.params
        name = params["skill"]
        args = params.get("args", [])
        if len(args) > MAX_SKILL_ARGS or any(len(a) > MAX_SKILL_ARG_CHARS for a in args):
            raise InvalidParams(f"skill.run: at most {MAX_SKILL_ARGS} arguments of at most "
                                f"{MAX_SKILL_ARG_CHARS} characters each")
        if len(name) > skills.MAX_NAME or not skills.NAME_RE.match(name):
            raise SkillRefused(f"{name[:80]!r} is not a skill name")
        script = _skill_script(params["script"])
        row = self._skills.active(name)
        if row is None:
            raise SkillRefused(f"{name!r} has no active version, and only an active "
                               "version runs")
        label = f"{name} v{row['version']}"
        if row["tier"] == "never":
            raise SkillRefused(f"{label} has tier never")
        entry = json.loads(row["manifest"]).get(script)
        if entry is None:
            raise SkillRefused(f"{script!r} is not a file of {label}")
        if not int(entry[1]) & stat.S_IXUSR:
            raise SkillRefused(f"{script!r} is not executable in {label}")
        problems = self._skills.verify_version(int(row["id"]))
        if problems:
            raise SkillRefused(f"the stored files of {label} no longer verify: "
                               + "; ".join(problems[:3]))
        try:
            containers.check_image(self._container.config.image)
            containers.check_limits(self._container.config)
        except ContainerUnavailable as exc:
            raise SkillRefused(str(exc)) from None
        if self._container.runtime.cli() is None:
            raise SkillRefused("no container runtime on this host, and a skill script never "
                               "runs without one")
        return row, script

    def _job_skill_run(self, request: ToolRequest) -> NetJob:
        row, script = self._precheck_skill(request)
        store, executor = self._skills, self._container
        assert store is not None and executor is not None, "checked by _precheck_skill"
        ws = self._workspace_for(request.task_id)
        name = row["name"]
        # A fresh directory per run, named here, inside the workspace, so the
        # guest sees it under /work and nothing else of the host.
        run_dir = f"{SKILL_RUN_PREFIX}{uuid.uuid4().hex[:12]}"
        (ws.root / run_dir).mkdir(mode=0o700)
        try:
            installed = store.install(name, ws.root / run_dir)
        except SkillStoreError as exc:
            _remove_entry(ws, run_dir)
            raise SkillRefused(f"the active version of {name!r} did not install: {exc}") \
                from None
        command = [f"{containers.GUEST_WORKDIR}/{run_dir}/{name}/{script}",
                   *request.params.get("args", [])]
        timeout = min(float(request.params.get("timeout", SKILL_DEFAULT_TIMEOUT)),
                      containers.MAX_TIMEOUT_SECONDS)
        job = NetJob(request.tool)

        def perform() -> ContainerResult:
            try:
                # The same flag shell.run uses, so revoke() and cancel_running()
                # reach a running script (#228).
                with self._cancel_flag(request.task_id) as cancel:
                    try:
                        return executor.run(ws.root, command, task_id=request.task_id,
                                            timeout=timeout, cancel=cancel)
                    except ContainerUnavailable as exc:
                        raise SkillRefused(f"refusing to run without a container: {exc}") \
                            from None
            finally:
                _remove_entry(ws, run_dir)

        def finish(result: ContainerResult) -> ToolResult:
            # Untrusted code wrote this. The task holds untrusted input from now on.
            if self.policy is not None:
                self.policy.taint(request.task_id, f"read the output of skill {name}")
            stdout, cut_out = _untrusted_text(result.stdout)
            stderr, cut_err = _untrusted_text(result.stderr)
            return ToolResult(result.ok, request.tool, {
                "skill": name, "version": installed.version, "script": script,
                "content_sha256": row["content_sha256"], "stdout": stdout, "stderr": stderr,
                "returncode": result.returncode, "timed_out": result.timed_out,
                "cancelled": result.cancelled,
                "truncated": result.truncated or cut_out or cut_err,
                "removed": result.removed, "container": result.name, "untrusted": True,
            }, error=None if result.ok else (stderr.strip()[-2000:] or "failed"))

        job.perform, job.finish = perform, finish
        return job

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
    """A tool call split at the thread boundary (the network tools, skill.run).

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


def _skill_script(script: str) -> str:
    """A script path as the manifest writes it, or a refusal.

    Relative, inside the skill, with no ``..``: the path names a manifest
    entry, never a place on disk.
    """
    path = PurePosixPath(script)
    if (not script or "\0" in script or "\\" in script or path.is_absolute()
            or ".." in path.parts):
        raise PathEscape(f"{script[:200]!r} is not a plain path inside the skill")
    parts = [p for p in path.parts if p not in ("", ".")]
    if not parts:
        raise PathEscape("the script path is empty")
    return "/".join(parts)


def _untrusted_text(text: str) -> tuple[str, bool]:
    """Output written by untrusted code: control characters dropped, capped."""
    cleaned = clean(text)
    return cleaned[:MAX_SKILL_OUTPUT_CHARS], len(cleaned) > MAX_SKILL_OUTPUT_CHARS


def _remove_entry(ws: Workspace, name: str) -> None:
    """Remove one top-level entry of the workspace without following a link.

    The guest could have replaced the directory with a symlink. That removes
    the link, never its target. Best effort: the container is already gone.
    """
    with contextlib.suppress(OSError), ws.dir_fd([]) as parent:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            shutil.rmtree(name, dir_fd=parent)
        else:
            os.unlink(name, dir_fd=parent)


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


# ------------------------------------------------------- read-only git (#240)

# Before every git command. Each switches off a way the repository's files
# or the environment could make git run a program, write, or reach out.
_GIT_SAFETY = (
    "--no-pager", "--no-replace-objects", "--literal-pathspecs",
    "-c", "core.fsmonitor=false",
    "-c", "core.hooksPath=/dev/null",
    "-c", "protocol.allow=never",
    "-c", "core.pager=cat",
    "-c", "core.attributesFile=/dev/null",
    "-c", "core.excludesFile=/dev/null",
    "-c", "core.untrackedCache=false",
    "-c", "submodule.recurse=false",
    "-c", "log.showSignature=false",
    "-c", "color.ui=false",
    "-c", "gc.auto=0",
    "-c", "maintenance.auto=false",
    "-c", "core.quotePath=true",
)

# The only keys a repository's own config may hold. Every key git reads to
# name a program (core.fsmonitor, core.pager, diff and filter drivers,
# gpg.program, credential helpers, aliases, include paths, extensions)
# is absent, so a repository that sets one is refused before git starts.
_GIT_CONFIG_KEYS = frozenset({
    "core.repositoryformatversion", "core.filemode", "core.bare", "core.logallrefupdates",
    "core.ignorecase", "core.precomposeunicode", "core.symlinks", "core.autocrlf",
    "core.eol", "user.name", "user.email", "init.defaultbranch",
})
_GIT_CONFIG_SUBSECTION_KEYS = frozenset({
    "remote.url", "remote.fetch", "branch.remote", "branch.merge", "branch.rebase",
})
_GIT_SECTION = re.compile(r'\[\s*([A-Za-z0-9.-]+)(?:\s+"((?:[^"\\]|\\.)*)")?\s*\]\s*(?:[#;].*)?')
_GIT_KEY = re.compile(r"([A-Za-z][A-Za-z0-9-]*)\s*(?:=.*)?")

# Inside a git directory, any of these points git at files elsewhere.
_GIT_FORBIDDEN = ("commondir", "config.worktree", "objects/info/alternates")


def _git_environment(root: Path, worktree: Path, gitdir: Path) -> dict[str, str]:
    """The whole environment git gets. No system or home configuration."""
    return {
        "PATH": GIT_PATH, "HOME": str(root), "LANG": "C.UTF-8",
        "GIT_DIR": str(gitdir), "GIT_WORK_TREE": str(worktree),
        "GIT_CEILING_DIRECTORIES": str(root.parent),
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_ATTR_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_PROTOCOL_FROM_USER": "0", "GIT_PAGER": "cat", "PAGER": "cat",
    }


def check_git_config(raw: bytes) -> None:
    """Refuse a repository config holding anything but the reviewed keys.

    The parser is deliberately stricter than git's: a line it does not
    understand is a refusal, so git can never read a key this did not see.
    """
    if len(raw) > GIT_MAX_CONFIG_BYTES:
        raise UnsafeRepository("the repository config is larger than the cap")
    try:
        text = raw.decode("utf-8").replace("\r\n", "\n")
    except UnicodeDecodeError:
        raise UnsafeRepository("the repository config is not UTF-8") from None
    if any(ord(ch) < 32 and ch not in "\t\n" for ch in text) or "\x7f" in text:
        raise UnsafeRepository("the repository config holds control characters")
    section: tuple[str, bool] | None = None
    for number, raw_line in enumerate(text.split("\n"), start=1):
        line = raw_line.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("["):
            m = _GIT_SECTION.fullmatch(line)
            if m is None:
                raise UnsafeRepository(f"config line {number} is not a plain section header")
            name, quoted = m.group(1).lower(), m.group(2) is not None
            if "." in name:
                if quoted:
                    raise UnsafeRepository(f"config line {number} is not a plain section header")
                name, quoted = name.split(".", 1)[0], True
            section = (name, quoted)
            continue
        m = _GIT_KEY.fullmatch(line)
        if m is None or section is None:
            raise UnsafeRepository(f"config line {number} is not a plain key")
        key = f"{section[0]}.{m.group(1).lower()}"
        allowed = _GIT_CONFIG_SUBSECTION_KEYS if section[1] else _GIT_CONFIG_KEYS
        if key not in allowed:
            raise UnsafeRepository(f"the repository config sets {key}, which is not allowed")


def _gitfile_target(ws: Workspace, parts: list[str], repo_fd: int) -> list[str]:
    """Where a ``.git`` file points, as workspace parts, or refuse."""
    fd = _open_regular(repo_fd, ".git")
    if fd is None:
        raise UnsafeRepository(".git is not a regular file")
    with os.fdopen(fd, "rb") as fh:
        raw = fh.read(4097)
    m = re.fullmatch(rb"gitdir: ([^\n\0]+)\n?", raw[:4096]) if len(raw) <= 4096 else None
    if m is None:
        raise UnsafeRepository(".git is a file but not a plain gitdir pointer")
    target = os.fsdecode(m.group(1)).rstrip("\r")
    joined = os.path.normpath(os.path.join(str(ws.root), *parts, target))
    for root in dict.fromkeys((str(ws.root), str(ws.root.resolve()))):
        relative = os.path.relpath(joined, root)
        if ".." not in PurePosixPath(relative).parts:
            return ws.parts(relative)
    raise UnsafeRepository(".git points outside the workspace")


def _check_git_directory(git_fd: int) -> None:
    """No symlink, special file or pointer elsewhere anywhere in it."""
    count = 0
    with _walk(git_fd) as walker:
        for dirpath, dirnames, filenames, dfd in walker:
            for name in (*dirnames, *filenames):
                count += 1
                if count > GIT_MAX_ENTRIES:
                    raise UnsafeRepository("the git directory has too many files to check")
                mode = os.stat(name, dir_fd=dfd, follow_symlinks=False).st_mode
                where = str(PurePosixPath(dirpath) / name)
                if stat.S_ISLNK(mode):
                    raise UnsafeRepository(f"{where} in the git directory is a symlink")
                if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                    raise UnsafeRepository(f"{where} in the git directory is a special file")
    for name in _GIT_FORBIDDEN:
        with contextlib.suppress(FileNotFoundError, NotADirectoryError):
            os.stat(name, dir_fd=git_fd, follow_symlinks=False)
            raise UnsafeRepository(f"the git directory has {name}, which points elsewhere")
    fd = _open_regular(git_fd, "config")
    if fd is not None:
        with os.fdopen(fd, "rb") as fh:
            check_git_config(fh.read(GIT_MAX_CONFIG_BYTES + 1))


def _git_repository(ws: Workspace, relative: str) -> tuple[Path, Path]:
    """The work tree and git directory for ``relative``, both checked."""
    ws.resolve(relative)
    parts = ws.parts(relative)
    try:
        with ws.dir_fd(parts) as repo_fd:
            try:
                mode = os.stat(".git", dir_fd=repo_fd, follow_symlinks=False).st_mode
            except FileNotFoundError:
                raise UnsafeRepository(f"{relative!r} is not a repository: no .git") from None
            if stat.S_ISDIR(mode):
                gitdir = [*parts, ".git"]
            elif stat.S_ISREG(mode):
                gitdir = _gitfile_target(ws, parts, repo_fd)
            else:
                raise UnsafeRepository(".git is a symlink or a special file")
    except FileNotFoundError:
        raise UnsafeRepository(f"{relative!r} is not a repository: no such directory") from None
    with ws.dir_fd(gitdir) as git_fd:
        _check_git_directory(git_fd)
    return ws.root.joinpath(*parts), ws.root.joinpath(*gitdir)


def _check_index_paths(listing: bytes) -> None:
    """Every path the index names stays inside the work tree."""
    for entry in listing.split(b"\0"):
        if not entry:
            continue
        path = os.fsdecode(entry)
        parts = PurePosixPath(path).parts
        if path.startswith("/") or any(p == ".." or p.casefold() == ".git" for p in parts):
            raise UnsafeRepository("the index names a path outside the work tree")


def _git_result(tool: str, done: sandbox._Execution) -> ToolResult:
    out = clean(done.stdout.decode("utf-8", errors="replace"))
    err = clean(done.stderr.decode("utf-8", errors="replace")).strip()[-2000:]
    ok = done.returncode == 0 and not done.timed_out and not done.cancelled
    return ToolResult(ok, tool, {
        "output": out, "truncated": done.truncated, "returncode": done.returncode,
        "timed_out": done.timed_out, "cancelled": done.cancelled,
    }, error=None if ok else (err or "git failed"))


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
