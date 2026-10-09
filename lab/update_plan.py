"""`lab update --plan COMMIT`: the update steps for this install, with the values filled in.

The steps are the shell blocks of "Updating the deployed code" in the runbook. They
are read from the runbook each time, so every command line printed is the runbook's
own line. Only three things are filled in: COMMIT, the deployed commit (OLD) and the
writers whose service definition is installed.

The command is read-only. It asks git for the deployed HEAD and whether COMMIT is in
the checkout (no shell, 30 second timeout), and checks which service definitions
exist. Then it prints the plan. It never runs sudo and never writes a file. The
update needs sudo for launchctl and for the ownership changes around the checkout,
and the lab never runs sudo, so the operator copies these lines into a Terminal
window and runs them there.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

RUNBOOK = Path(__file__).resolve().parent.parent / "ops" / "runbook-lab-account-and-daemons.md"
SECTION_HEADING = "## Updating the deployed code"
DEFAULT_DEPLOY = Path("/opt/homelab")
DEFAULT_LAUNCH_DAEMONS = Path("/Library/LaunchDaemons")
WRITER_NAMES = ("supervisor", "tick", "selftest", "weekly-eval", "chat")
GIT_TIMEOUT_SECONDS = 30

Runner = Callable[..., subprocess.CompletedProcess[str]]

_FULL_COMMIT = re.compile(r"[0-9a-f]{40}")
_FENCE_OPEN = re.compile(r"^(\s*)```sh\s*$")
_FENCE_CLOSE = re.compile(r"^\s*```\s*$")
_INLINE_LOOP = re.compile(r"`(for s in [^`]+)`")

# The three lines the plan fills in, exactly as the runbook writes them.
_COMMIT_LINE = 'COMMIT="PASTE_THE_NEW_COMMIT"'
_OLD_LINE = "OLD=$(sudo git -C /opt/homelab rev-parse HEAD)"
_WRITERS_PREFIX = "WRITERS=("

_HEADING, _NOTE, _CHUNK = "heading", "note", "chunk"

# The plan, in order. A chunk is the next command chunk of the section: a fenced sh
# block, or an inline `for s in` loop from the rollback text. The notes are short
# paraphrases of the runbook's prose, not command lines.
_LAYOUT: tuple[tuple[str, str], ...] = (
    (_HEADING, "Set the values for this update."),
    (_CHUNK, ""),
    (_NOTE, "Write down the deployed now hash. It is the commit to roll back to."),
    (_HEADING, "Back up before anything changes. The old code is still in place."),
    (_NOTE, "BACKUP_VOLUME must be set in this window, with the volume attached."),
    (_CHUNK, ""),
    (_NOTE, "Go on only if the first line prints wrote, then restore check ok, and the "
            "listing shows a new lab-*.manifest.json."),
    (_HEADING, "Stop the jobs that write to the database."),
    (_CHUNK, ""),
    (_NOTE, "If the second loop prints STILL LOADED, bootout that job again before the "
            "checkout. In a new Terminal window, run the WRITERS line again first."),
    (_HEADING, "Check out the new code and sync its environment."),
    (_CHUNK, ""),
    (_NOTE, "If diff --stat lists a file, its installed copy is stale. Reinstall it first, "
            "as the runbook says."),
    (_HEADING, "Try the migrations on a copy before anything starts."),
    (_CHUNK, ""),
    (_NOTE, "Go on only if the line starts with migrate --check: and says the database was "
            "not changed. If it starts with migrate --check failed, roll back as below."),
    (_HEADING, "Start the jobs again on the new code."),
    (_CHUNK, ""),
    (_NOTE, "rev-parse must print the new commit. status must show health IDLE or OK and "
            "mode running."),
    (_HEADING, "Roll back before anything started."),
    (_NOTE, "The database was never opened by the new code, so nothing is restored. Run the "
            "checkout and sync block above with COMMIT set to OLD, then the start again block."),
    (_HEADING, "Roll back after the new code ran."),
    (_NOTE, "Stop everything that opens the database, keep-awake included. Do not go on while "
            "anything is still loaded."),
    (_CHUNK, ""),
    (_CHUNK, ""),
    (_NOTE, "Set N to the first schema version that migrate --check named. The upgraded files "
            "are moved aside, not deleted."),
    (_CHUNK, ""),
    (_NOTE, "Then run the checkout and sync block above with COMMIT set to OLD."),
    (_CHUNK, ""),
    (_NOTE, "Then run the checks from the start again block. rev-parse must print OLD."),
)
_CHUNK_COUNT = sum(1 for kind, _ in _LAYOUT if kind == _CHUNK)


class UpdateError(Exception):
    """Something the plan cannot be made from. The message is one line."""


class PlanLine(NamedTuple):
    text: str
    command: bool = False


def is_full_commit(text: str) -> bool:
    """True for exactly 40 lowercase hex characters."""
    return _FULL_COMMIT.fullmatch(text) is not None


def gather(commit: str, deploy: Path, launch_daemons: Path,
           run: Runner = subprocess.run) -> tuple[str, bool, list[str]]:
    """OLD (the deployed HEAD), whether COMMIT is in the deployed checkout, and the
    writers whose service definition is installed, in WRITER_NAMES order."""
    head = _git(run, deploy, "rev-parse", "HEAD")
    old = head.stdout.strip()
    if head.returncode != 0 or not old:
        raise UpdateError(f"cannot read the deployed commit in {deploy}")
    present = _git(run, deploy, "cat-file", "-e", f"{commit}^{{commit}}")
    writers = [name for name in WRITER_NAMES
               if (launch_daemons / f"com.homelab.{name}.plist").exists()]
    return old, present.returncode == 0, writers


def _git(run: Runner, deploy: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return run(["git", "-C", str(deploy), *args], capture_output=True, text=True,
                   check=False, timeout=GIT_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateError(f"cannot run git in {deploy}") from exc


def plan(commit: str, old: str, present: bool, writers: list[str]) -> list[PlanLine]:
    """The lines of the plan. A line with command=True is a runbook command line, with
    the three filled-in values substituted. Every other line is text from this module."""
    chunks = _command_chunks(_read_section())
    if len(chunks) != _CHUNK_COUNT:
        raise UpdateError(f"the runbook section has {len(chunks)} command blocks, the plan "
                          f"expects {_CHUNK_COUNT}. Update lab/update_plan.py to match.")
    lines = [
        PlanLine("Update plan for this install. It runs no sudo and writes no file."),
        PlanLine(f"Deployed now: {old}"),
        PlanLine(f"Updating to: {commit}"),
        PlanLine("The new commit is present in the deployed checkout." if present else
                 "The new commit is not present in the deployed checkout yet. The git fetch "
                 "step under \"Check out the new code\" is needed."),
        PlanLine("Writers installed here: " + (" ".join(writers) if writers else "none")),
    ]
    next_chunk = iter(chunks)
    for kind, text in _LAYOUT:
        if kind == _HEADING:
            lines += [PlanLine(""), PlanLine(text)]
        elif kind == _NOTE:
            lines.append(PlanLine(text))
        else:
            lines += [PlanLine(_fill(line, commit, old, writers), command=True)
                      for line in next(next_chunk)]
    return lines


def render(commit: str, old: str, present: bool, writers: list[str]) -> str:
    """The plan as the text the command prints."""
    return "".join(f"{line.text}\n" for line in plan(commit, old, present, writers))


def _fill(line: str, commit: str, old: str, writers: list[str]) -> str:
    if line == _COMMIT_LINE:
        return f'COMMIT="{commit}"'
    if line == _OLD_LINE:
        return f"OLD={old}"
    if line.startswith(_WRITERS_PREFIX):
        return f"{_WRITERS_PREFIX}{' '.join(writers)})"
    return line


def _read_section() -> list[str]:
    try:
        text = RUNBOOK.read_text(encoding="utf-8")
    except OSError as exc:
        raise UpdateError(f"cannot read the runbook at {RUNBOOK}") from exc
    lines = text.splitlines()
    if SECTION_HEADING not in lines:
        raise UpdateError(f"the runbook has no '{SECTION_HEADING[3:]}' section")
    start = lines.index(SECTION_HEADING)
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
               len(lines))
    return lines[start:end]


def _command_chunks(section: list[str]) -> list[list[str]]:
    """The command chunks of the section in order: each fenced sh block (dedented, so an
    indented block in a list reads the same), and each inline `for s in` loop. Blank
    lines are dropped."""
    found: list[list[str]] = []
    index = 0
    while index < len(section):
        opening = _FENCE_OPEN.match(section[index])
        if opening is None:
            found += [[match.group(1)] for match in _INLINE_LOOP.finditer(section[index])]
            index += 1
            continue
        indent = len(opening.group(1))
        end = index + 1
        while end < len(section) and not _FENCE_CLOSE.match(section[end]):
            end += 1
        if end == len(section):
            raise UpdateError("a shell block in the runbook section is not closed")
        body = [_dedent(line, indent) for line in section[index + 1:end] if line.strip()]
        found.append(body)
        index = end + 1
    return found


def _dedent(line: str, indent: int) -> str:
    return line[indent:] if line[:indent].strip() == "" else line
