"""OS-level process isolation for anything that executes code.

Closes issue #17. The broker (#10) confines *filesystem paths* in Python.
That is enforcement by the caller, which works only as long as the caller
is the one doing the opening. The moment a tool spawns a subprocess, the
subprocess makes its own syscalls and walks straight past every check the
broker performs.

This module closes that gap using macOS Seatbelt via `sandbox-exec`. The
kernel intercepts each file open, network connect and fork at the syscall
boundary, and **child processes inherit the restrictions**, which is the
property that matters: a sandboxed shell cannot escape by spawning
something else.

Why Seatbelt and not a container
--------------------------------

Section 15 of the reference architecture says not to build heavy
virtualization initially, because on 32 GB the overhead is a real cost
measured against the one heavy inference slot. Apple's container
framework on macOS 26 gives a VM per container, which is stronger
isolation and the right answer later for genuinely untrusted code, but
paying a VM's memory for every ordinary task on a machine with one
inference slot is the wrong trade today.

Seatbelt costs no memory, needs no daemon and enforces in the kernel.

The honest caveat
-----------------

`sandbox-exec` is **deprecated** by Apple. It remains fully functional,
macOS's own daemons use Seatbelt internally, and Apple has published no
replacement covering headless process sandboxing: App Sandbox requires
code signing and an Xcode project, which does not apply to a background
worker. The deprecation is tracked as a risk with Apple's container
framework as the fallback, rather than pretended away.

Three implementation details that are easy to get wrong
-------------------------------------------------------

**Paths must be fully resolved.** On macOS `/tmp` is a symlink to
`/private/tmp`, so a profile granting access to `/tmp/work` matches
nothing at all and every operation is denied, including the ones meant to
be allowed. That failure looks like a broken sandbox rather than a
misconfigured one, so `resolve()` is applied before a path is ever
written into a profile. Confirmed still true on macOS 27.

**A deny-default profile needs read access to `/` itself.** Granting
subpaths of `/usr`, `/bin` and `/System` is not sufficient: on macOS 27
dyld can fail during image loading and the process dies with SIGABRT
before `main`, with no useful message. Adding `(literal "/")` fixes it,
and grants only the root directory entry, not its contents.

**Output must be captured through pipes, never an inherited terminal.**
Under deny-default, writing to a controlling tty is refused even with a
blanket `file-write*`, and the error blames `stdout` rather than the
sandbox. `run()` always uses `capture_output=True`, so this is handled,
but a caller that reaches for `subprocess` directly will hit it.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

SANDBOX_EXEC = "/usr/bin/sandbox-exec"

# Imported so ordinary process startup works: dynamic linking, sysctl
# reads, and the handful of devices any binary touches before main().
# Without it even /bin/cat aborts before reaching its arguments.
BSD_PROFILE = "/System/Library/Sandbox/Profiles/bsd.sb"


class SandboxUnavailable(RuntimeError):
    """Raised when OS-level isolation cannot be provided.

    Deliberately an error rather than a silent downgrade. A caller that
    asked for isolation and quietly did not get it is worse than one that
    failed, because it will be trusted with work it cannot safely run.
    """


@dataclass(frozen=True)
class SandboxResult:
    ok: bool
    returncode: int
    stdout: str
    stderr: str
    denied: bool = False


def available() -> bool:
    """True if this host can enforce OS-level isolation."""
    return platform.system() == "Darwin" and Path(SANDBOX_EXEC).exists()


def build_profile(
    workspace: Path,
    *,
    allow_network: bool = False,
    extra_readable: tuple[Path, ...] = (),
) -> str:
    """Generate a Seatbelt profile confining a process to one workspace.

    Deny by default. Everything permitted is permitted explicitly, and
    the workspace is the only writable location.
    """
    root = workspace.resolve()
    lines = [
        "(version 1)",
        "(deny default)",
        f'(import "{BSD_PROFILE}")',
        "(allow process-exec)",
        "(allow process-fork)",
        # Read access to the root directory ENTRY, not its contents.
        # Without it, dyld can fail during image loading on macOS 27 and
        # the process dies with SIGABRT before reaching main, producing
        # no useful message. It presents as "the sandbox is broken"
        # when the profile is merely missing one clause.
        #
        # Verified not to weaken confinement: with this clause present,
        # reads of /etc/hosts, ~/.ssh and any path outside the workspace
        # are still denied. It permits stat and readdir on "/" alone.
        '(allow file-read* (literal "/"))',
        f'(allow file-read* file-write* (subpath "{root}"))',
        # bsd.sb permits /etc so ordinary processes can do user lookups,
        # which leaks usernames, home directories and shells to a
        # sandboxed agent. Later rules win in SBPL, so denying it after
        # the import closes that without breaking process startup.
        # Verified: /bin/echo, /bin/cat and /bin/sh -c all still run.
        '(deny file-read* (subpath "/etc") (subpath "/private/etc"))',
    ]
    for path in extra_readable:
        lines.append(f'(allow file-read* (subpath "{path.resolve()}"))')

    if allow_network:
        # Not reachable yet: nothing calls this with network on, and
        # per-host egress control is issue #14. Present so the shape of
        # the profile is right when that lands, and denied by default
        # until then.
        lines.append("(allow network-outbound)")

    return "\n".join(lines) + "\n"


def run(
    argv: list[str],
    workspace: Path,
    *,
    timeout: float = 30.0,
    allow_network: bool = False,
    extra_readable: tuple[Path, ...] = (),
) -> SandboxResult:
    """Run a command confined to ``workspace``.

    Raises SandboxUnavailable rather than running unconfined if the host
    cannot enforce isolation.
    """
    if not available():
        raise SandboxUnavailable(
            f"no OS-level sandbox on {platform.system()}; refusing to run "
            f"{argv[0]!r} unconfined"
        )

    root = workspace.resolve()
    if not root.is_dir():
        raise SandboxUnavailable(f"workspace does not exist: {root}")

    profile = root / ".sandbox.sb"
    profile.write_text(
        build_profile(root, allow_network=allow_network,
                      extra_readable=extra_readable),
        encoding="utf-8",
    )

    try:
        completed = subprocess.run(
            [SANDBOX_EXEC, "-f", str(profile), *argv],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(root),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return SandboxResult(
            ok=False, returncode=-1, stdout="", stderr="timed out"
        )
    finally:
        profile.unlink(missing_ok=True)

    stderr = completed.stderr
    # Seatbelt surfaces refusals as EPERM, which tools report in their
    # own words. Recognising it lets a caller tell "the sandbox stopped
    # this" apart from "the command failed on its own merits".
    denied = (
        "Operation not permitted" in stderr
        or "sandbox-exec" in stderr
        or "deny " in stderr
    )
    return SandboxResult(
        ok=completed.returncode == 0,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=stderr,
        denied=denied and completed.returncode != 0,
    )


def which(binary: str) -> str | None:
    """Resolve a binary to an absolute path, as profiles require."""
    return shutil.which(binary)
