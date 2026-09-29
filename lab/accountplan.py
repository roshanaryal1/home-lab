"""The lab-account setup plan (H5c).

What makes the lab's boundaries real is which OS account can read and write
what: the agent runs as a non-admin ``lab`` user that cannot read the
operator's private key, cannot write the LaunchDaemon definitions and cannot
gain root. That needs the machine, so this module writes the setup down as
data instead of running it:

* ``build()`` returns an ordered list of steps, each an absolute-path argv.
  Building the plan executes nothing.
* ``render()`` prints it for a person to read and copy.
* ``apply()`` runs the mutating steps only when asked, as root, on macOS, in
  order, and stops at the first failure. Read-only verification steps are
  listed with what to expect and are never run by ``apply``.

The operator's private key is deliberately absent from the plan: nothing here
reads, copies or changes it.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
LAUNCHD_SRC = Path(__file__).resolve().parent.parent / "ops" / "launchd"
LAUNCH_DAEMONS = "/Library/LaunchDaemons"
DATA_DIR = "/var/homelab"
LOG_DIR = "/var/log/homelab"
CONFIG_DIR = "/etc/homelab"


class ApplyRefused(RuntimeError):
    """Apply was asked for in an environment where it must not run."""


@dataclass(frozen=True)
class Step:
    title: str
    argv: tuple[str, ...]
    mutates: bool = True
    expect: str = ""             # for read-only checks: what a correct machine prints

    @property
    def command(self) -> str:
        return shlex.join(self.argv)


@dataclass
class ApplyResult:
    ok: bool = True
    steps_run: list[Step] = field(default_factory=list)
    failed: Step | None = None
    detail: str = ""


def default_pubkey() -> str:
    """Where ``operator init --dir ~/.lab-operator`` puts the public key."""
    return str(Path.home() / ".lab-operator" / "operator.pub")


def build(user: str = "lab", *, operator_pubkey: str | None = None,
          launchd_dir: Path = LAUNCHD_SRC) -> list[Step]:
    if not USER.fullmatch(user):
        raise ValueError("the account name must be a short lowercase POSIX name")
    operator_pubkey = operator_pubkey or default_pubkey()
    if not operator_pubkey.startswith("/"):
        raise ValueError("the operator public key path must be absolute")
    home = f"/Users/{user}"
    steps = [
        Step("Create the non-admin lab account (prompts for a password; never admin)",
             ("/usr/sbin/sysadminctl", "-addUser", user, "-fullName", "Home Lab",
              "-password", "-", "-home", home, "-shell", "/bin/zsh")),
        Step("Private data directory, created owned by the lab account and closed to others",
             ("/usr/bin/install", "-d", "-o", user, "-g", "staff", "-m", "700", DATA_DIR)),
        Step("Private log directory, created owned by the lab account and closed to others",
             ("/usr/bin/install", "-d", "-o", user, "-g", "staff", "-m", "700", LOG_DIR)),
        Step("Configuration directory, root-owned, world-readable and traversable",
             ("/usr/bin/install", "-d", "-o", "root", "-g", "wheel", "-m", "755", CONFIG_DIR)),
        Step("Install only the operator PUBLIC key, root-owned, readable by lab, not writable",
             ("/usr/bin/install", "-o", "root", "-g", "wheel", "-m", "644",
              operator_pubkey, f"{CONFIG_DIR}/operator.pub")),
    ]
    for plist in sorted(launchd_dir.glob("*.plist")):
        steps.append(Step(
            f"Install {plist.name} root-owned so the lab account cannot edit its own service",
            ("/usr/bin/install", "-o", "root", "-g", "wheel", "-m", "644",
             str(plist), f"{LAUNCH_DAEMONS}/{plist.name}")))
    steps += [
        Step("lab must not be able to become root",
             ("/usr/bin/sudo", "-u", user, "/usr/bin/sudo", "-n", "-l"), mutates=False,
             expect="a refusal ('a password is required' or 'not allowed to run sudo')"),
        Step("lab must not be in the admin group",
             ("/usr/bin/dscl", ".", "-read", "/Groups/admin", "GroupMembership"),
             mutates=False, expect=f"a list that does not include {user}"),
        Step("lab must not be able to read the operator's private key",
             # The private key sits next to the public one (``operator init``
             # writes both). An absolute path: a quoted ~ would not expand.
             ("/usr/bin/sudo", "-u", user, "/bin/cat",
              str(Path(operator_pubkey).with_name("operator.key"))),
             mutates=False, expect="Permission denied"),
        Step("lab must not be able to overwrite a service definition",
             ("/usr/bin/sudo", "-u", user, "/usr/bin/touch",
              f"{LAUNCH_DAEMONS}/com.homelab.supervisor.plist"), mutates=False,
             expect="Permission denied"),
    ]
    return steps


def render(steps: list[Step]) -> str:
    lines = ["DRY RUN: nothing below has been executed. Read it, then run it as root "
             "yourself or pass --apply.", ""]
    for number, step in enumerate(steps, 1):
        kind = "do   " if step.mutates else "check"
        lines.append(f"{number:>2}. [{kind}] {step.title}")
        lines.append(f"      {step.command}")
        if step.expect:
            lines.append(f"      expect: {step.expect}")
    return "\n".join(lines)


def apply(steps: list[Step], *, platform: str | None = None) -> ApplyResult:
    if os.geteuid() != 0:
        raise ApplyRefused("apply must run as root (sudo); nothing was changed")
    if (platform or sys.platform) != "darwin":
        raise ApplyRefused("apply only runs on macOS; this plan is for the Mac mini")
    result = ApplyResult()
    for step in steps:
        if not step.mutates:
            continue
        try:
            # A fixed absolute argv from build(), no shell, no input from outside.
            # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
            proc = subprocess.run(list(step.argv), capture_output=True, text=True, check=False)
        except OSError as exc:
            result.ok, result.failed, result.detail = False, step, str(exc)[:300]
            break
        result.steps_run.append(step)
        if proc.returncode != 0:
            result.ok = False
            result.failed = step
            result.detail = (proc.stderr or proc.stdout).strip()[:300]
            break
    return result
