"""Disposable containers for untrusted code (issue #181, ADR 0007).

Seatbelt (`lab/sandbox.py`) confines our own reviewed tools. Code fetched or
generated from untrusted input gets a stronger boundary: one Apple
`container` per task, a Linux guest in its own lightweight VM, with the task
workspace as the only host directory it can see, no network, and the whole
VM destroyed when the task ends.

The shape of a run
------------------

1. Every check happens before anything starts, and every check fails closed:
   the image must be pinned by digest, the workspace must be a real
   directory inside the lab's workspace root and not a symlink, the command
   and environment must be plain, and the `container` CLI must be present.
2. The exact command line is built by one pure function,
   ``build_run_argv``, so tests can assert on it. It is always an argument
   list, never a shell string.
3. The container is started attached, under a name chosen here. Whatever
   happens next (normal exit, timeout, a stop, an exception), a ``finally``
   block runs ``container delete --force <name>``.

Why removal is the control, not a courtesy
------------------------------------------

Killing the CLI client on a timeout or a stop does not end the guest: the
container keeps running in its VM. Deleting it with ``--force`` stops the VM,
and every process inside it goes with it, including one that called
``setsid()`` to leave its process group. That is the gap #223 left open for
Seatbelt, where a ``setsid`` survivor outlives ``sandbox.run``. For untrusted
code the container tier closes it, as long as removal always runs, which is
what the tests here prove on every path.

The tool's defaults are not ours
--------------------------------

Measured on the M6 (ADR 0007): Apple's `container` gives a guest the network
by default. ``--network none`` is therefore always passed explicitly, and
nothing a caller can set removes it.

Integration point
-----------------

Not wired into the broker yet. A broker tool would add a schema to
``broker.TOOL_SCHEMAS``, and that table also generates the model's grammar
and the pre-registered tool-call corpus (``evals/toolcalls-v1.jsonl``, 70
tasks over the current seven tools). Adding an eighth tool would change a
measured artifact, so it belongs in its own change. When it lands, the tool
is a thin wrapper over ``ContainerExecutor.run``: approve tier, journaled as
non-idempotent, and classified in ``authority.TOOL_LEGS`` as untrusted input,
since its output is written by untrusted code. The executor takes the same
``cancel`` flag ``shell.run`` uses, so ``broker.revoke()`` and
``cancel_running()`` reach it unchanged.
"""

from __future__ import annotations

import contextlib
import logging
import os
import platform
import re
import shutil
import threading
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from lab import sandbox

LOG = logging.getLogger(__name__)

# Where the workspace appears inside the guest. Fixed: a caller cannot pick
# another target, so it cannot mount over something the guest relies on.
GUEST_WORKDIR = "/work"

# The installer puts the CLI here; a PATH lookup is the fallback.
DEFAULT_CLI = "/usr/local/bin/container"

# Same ceilings as the Seatbelt tier: a request may lower them, never raise.
MAX_TIMEOUT_SECONDS = sandbox.MAX_TIMEOUT_SECONDS
MAX_OUTPUT_BYTES = sandbox.MAX_OUTPUT_BYTES
# Removing a container stops its VM. Bounded so a wedged runtime cannot hang
# the caller, and long enough for a slow stop.
REMOVE_TIMEOUT_SECONDS = 60.0

# Guest sizes. Measured on the M6: a busy 1024 MB guest costs about 2 GiB of
# host memory, so the ceiling is kept close to that (ADR 0007).
DEFAULT_CPUS = 2
MAX_CPUS = 4
DEFAULT_MEMORY_MB = 1024
MAX_MEMORY_MB = 2048

# The only variables a caller may set in the guest. Nothing is inherited from
# the host: the CLI does not pass its own environment through, and only what
# is listed here (plus the fixed values below) is added with --env.
ENV_ALLOWLIST = frozenset({"LANG", "LC_ALL", "TZ", "PYTHONUNBUFFERED",
                           "PYTHONDONTWRITEBYTECODE", "PYTHONHASHSEED", "CI"})
FIXED_ENV = {"HOME": GUEST_WORKDIR, "TMPDIR": "/tmp", "PATH":
             "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"}

# registry/name[:tag]@sha256:<64 hex>. A tag alone can be moved by whoever
# controls the registry; a digest cannot.
_PINNED_IMAGE = re.compile(
    r"^[a-z0-9][a-z0-9._-]*(?::[0-9]+)?(?:/[a-z0-9][a-z0-9._-]*)*"
    r"(?::[A-Za-z0-9_][A-Za-z0-9._-]{0,127})?@sha256:[0-9a-f]{64}$")
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
# A volume is written as host:guest. A colon or comma in the host path could
# change how the CLI splits it, so such a path is refused rather than quoted.
_MOUNT_UNSAFE = frozenset(":,\n\r\0")
_CONTROL = frozenset("\n\r\0")


class ContainerUnavailable(RuntimeError):
    """Raised when the container tier cannot run this safely.

    Never a silent downgrade: untrusted code that cannot get a container
    does not run at all, and in particular does not fall back to Seatbelt
    (ADR 0007, Consequences).
    """


@dataclass(frozen=True)
class Outcome:
    """What one CLI invocation did, as a runtime reports it."""

    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""
    timed_out: bool = False
    truncated: bool = False
    cancelled: bool = False


class ContainerRuntime(Protocol):
    """The seam between the executor and the real `container` CLI.

    Tests use a fake that records each argument list; the Mac uses
    ``AppleContainerRuntime``.
    """

    def cli(self) -> str | None:
        """Absolute path of the CLI, or None if this host cannot run it."""

    def execute(self, argv: Sequence[str], *, timeout: float,
                cancel: threading.Event | None = None) -> Outcome:
        """Run one CLI command as an argument list, bounded by ``timeout``."""


class AppleContainerRuntime:
    """Shells out to Apple's `container` CLI. macOS 26 or later, Apple silicon."""

    def __init__(self, cli_path: str | None = None) -> None:
        self._cli_path = cli_path

    def cli(self) -> str | None:
        if platform.system() != "Darwin":
            return None
        candidate = self._cli_path or (DEFAULT_CLI if Path(DEFAULT_CLI).exists()
                                       else shutil.which("container"))
        if candidate is None or not os.path.isabs(candidate):
            return None
        return candidate if os.access(candidate, os.X_OK) else None

    def execute(self, argv: Sequence[str], *, timeout: float,
                cancel: threading.Event | None = None) -> Outcome:
        # The CLI is trusted host code, but it still gets no tokens from the
        # supervisor's environment: only what it needs to find its service.
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
               "HOME": os.environ.get("HOME", "/")}
        done = sandbox._execute(list(argv), cwd="/", env=env, timeout=timeout,
                                cap=MAX_OUTPUT_BYTES, cancel=cancel)
        return Outcome(done.returncode, done.stdout, done.stderr, done.timed_out,
                       done.truncated, done.cancelled)


@dataclass(frozen=True)
class ContainerConfig:
    """Set by trusted code, never by a task or by model output."""

    image: str
    cpus: int = DEFAULT_CPUS
    memory_mb: int = DEFAULT_MEMORY_MB


@dataclass(frozen=True)
class ContainerResult:
    ok: bool
    returncode: int
    stdout: str
    stderr: str
    name: str
    removed: bool
    timed_out: bool = False
    cancelled: bool = False
    truncated: bool = False


def check_image(image: str) -> str:
    if not _PINNED_IMAGE.match(image):
        raise ContainerUnavailable(f"image must be pinned by sha256 digest: {image!r}")
    return image


def check_limits(config: ContainerConfig) -> None:
    for label, value, ceiling in (("cpus", config.cpus, MAX_CPUS),
                                  ("memory_mb", config.memory_mb, MAX_MEMORY_MB)):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= ceiling:
            raise ContainerUnavailable(f"{label} must be between 1 and {ceiling}: {value!r}")


def check_workspace(workspace: Path, root: Path) -> Path:
    """The resolved workspace, or a refusal.

    It must be a real directory strictly inside the lab's workspace root.
    A symlink is refused even when it points somewhere harmless: the mount
    is what the guest gets, and it must be the directory the broker made.
    """
    workspace = Path(workspace)
    if workspace.is_symlink():
        raise ContainerUnavailable(f"workspace is a symlink: {workspace}")
    resolved, base = workspace.resolve(), Path(root).resolve()
    if resolved == base or not resolved.is_relative_to(base):
        raise ContainerUnavailable(f"workspace is not inside {base}: {resolved}")
    if not resolved.is_dir():
        raise ContainerUnavailable(f"workspace does not exist: {resolved}")
    if _MOUNT_UNSAFE.intersection(str(resolved)):
        raise ContainerUnavailable(f"refusing a path that could alter the mount: {resolved!r}")
    return resolved


def check_command(command: Sequence[str]) -> list[str]:
    if (isinstance(command, str) or not command
            or not all(isinstance(a, str) and "\0" not in a for a in command)):
        raise ContainerUnavailable("command must be a non-empty list of strings")
    return list(command)


def check_env(env: Mapping[str, str] | None) -> dict[str, str]:
    """Only allowlisted names, with plain values. Refused, not filtered: a
    caller that asked for a variable and silently lost it would be confused,
    and one that tried to pass a token should be told no."""
    out: dict[str, str] = {}
    for name, value in (env or {}).items():
        if name not in ENV_ALLOWLIST or not _ENV_NAME.match(name):
            raise ContainerUnavailable(f"environment variable not allowed: {name!r}")
        if not isinstance(value, str) or _CONTROL.intersection(value):
            raise ContainerUnavailable(f"environment value not allowed for {name}")
        out[name] = value
    return out


def container_name(task_id: str) -> str:
    """A name chosen here, unique per run, so removal targets exactly this one."""
    slug = re.sub(r"[^a-z0-9]+", "-", task_id.lower()).strip("-")[:24] or "task"
    name = f"lab-{slug}-{uuid.uuid4().hex[:12]}"
    if not _NAME.match(name):          # pragma: no cover - the slug above guarantees it
        raise ContainerUnavailable(f"bad container name {name!r}")
    return name


def build_run_argv(cli: str, name: str, workspace: Path, command: Sequence[str],
                   config: ContainerConfig, env: Mapping[str, str] | None = None) -> list[str]:
    """The exact `container run` command line. Pure, so tests assert on it.

    Every option is fixed here. The image and command come last, and the
    image is digest-pinned, so nothing the caller passes can be read as an
    option to `container run` itself.
    """
    if not _NAME.match(name):
        raise ContainerUnavailable(f"bad container name {name!r}")
    check_limits(config)
    argv = [
        cli, "run",
        "--name", name,
        # The tool's default is network ON (ADR 0007, measured).
        "--network", "none",
        "--read-only",
        # The root is read-only, so give the guest a scratch /tmp in its own
        # memory rather than a second host mount.
        "--tmpfs", "/tmp",
        "--cpus", str(config.cpus),
        "--memory", f"{config.memory_mb}M",
        # The only host-backed mount.
        "--volume", f"{workspace}:{GUEST_WORKDIR}",
        "--workdir", GUEST_WORKDIR,
    ]
    merged = {**check_env(env), **FIXED_ENV}
    for key in sorted(merged):
        argv += ["--env", f"{key}={merged[key]}"]
    argv.append(check_image(config.image))
    argv += check_command(command)
    return argv


def build_remove_argv(cli: str, name: str) -> list[str]:
    """Force-delete stops the VM if it is still running, then removes it."""
    if not _NAME.match(name):
        raise ContainerUnavailable(f"bad container name {name!r}")
    return [cli, "delete", "--force", name]


def build_list_argv(cli: str) -> list[str]:
    """Every container, running or not, one id per line."""
    return [cli, "list", "--all", "--quiet"]


class ContainerExecutor:
    """Runs one command in one disposable container, then destroys it."""

    def __init__(self, runtime: ContainerRuntime, workspace_root: Path,
                 config: ContainerConfig) -> None:
        self.runtime = runtime
        self.workspace_root = Path(workspace_root)
        self.config = config

    def run(self, workspace: Path, command: Sequence[str], *, task_id: str = "task",
            timeout: float = 30.0, env: Mapping[str, str] | None = None,
            cancel: threading.Event | None = None) -> ContainerResult:
        """Run ``command`` with ``workspace`` mounted read-write at /work.

        Raises ContainerUnavailable before starting anything if a check
        fails. Once a container may exist, it is removed on every path out.
        """
        check_image(self.config.image)
        check_limits(self.config)
        cli = self.runtime.cli()
        if cli is None:
            raise ContainerUnavailable("no container runtime on this host; refusing to run "
                                       "untrusted code without one")
        root = check_workspace(workspace, self.workspace_root)
        name = container_name(task_id)
        run_argv = build_run_argv(cli, name, root, command, self.config, env)
        clamped = min(max(float(timeout), 0.0), MAX_TIMEOUT_SECONDS)

        try:
            outcome = self.runtime.execute(run_argv, timeout=clamped, cancel=cancel)
        finally:
            removed = self._remove(cli, name)

        stderr = outcome.stderr.decode(errors="replace")
        if outcome.cancelled:
            stderr = "cancelled"
        elif outcome.timed_out:
            stderr = "timed out"
        if not removed:
            stderr = (stderr + "\n" if stderr else "") + f"container {name} was not removed"
        return ContainerResult(
            ok=outcome.returncode == 0 and removed and not (outcome.timed_out
                                                            or outcome.cancelled),
            returncode=-1 if outcome.timed_out or outcome.cancelled else outcome.returncode,
            stdout=outcome.stdout.decode(errors="replace"),
            stderr=stderr,
            name=name,
            removed=removed,
            timed_out=outcome.timed_out,
            cancelled=outcome.cancelled,
            truncated=outcome.truncated,
        )

    def _remove(self, cli: str, name: str) -> bool:
        """True once ``name`` is known to be gone.

        Never raises: it runs in a ``finally`` and must not hide the original
        error. A delete can fail because the container was never created (the
        run failed before it started); the listing tells that apart from a
        container that is still there. Anything uncertain counts as not
        removed, is logged loudly and is reported in the result, because a
        container left running is a VM still holding the task's processes.
        """
        argv = build_remove_argv(cli, name)
        for _attempt in range(2):
            with contextlib.suppress(Exception):
                if self.runtime.execute(argv, timeout=REMOVE_TIMEOUT_SECONDS).returncode == 0:
                    return True
        with contextlib.suppress(Exception):
            listed = self.runtime.execute(build_list_argv(cli), timeout=REMOVE_TIMEOUT_SECONDS)
            names = listed.stdout.decode(errors="replace").split()
            if listed.returncode == 0 and name not in names:
                return True
        LOG.error("container %s could not be removed; it may still be running", name)
        return False
