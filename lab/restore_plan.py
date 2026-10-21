"""`lab restore --plan MANIFEST`: the steps that put a backup back into service (#339).

The steps are the runbook section "Putting a backup back into service", read from
the runbook each time and printed as written, with only the manifest line filled
in. Like `lab update --plan`, the command runs no sudo, writes no file and opens no
database. The first printed step restore-checks the backup, so the manifest itself
is checked there, by the lab account that can read the backup folder.

The manifest path goes into a shell line the operator pastes, so it must be an
absolute path to a manifest name `lab backup` writes, made only of characters that
mean nothing to the shell inside double quotes.
"""

from __future__ import annotations

import re
from pathlib import Path

from lab.update_plan import RUNBOOK

SECTION_HEADING = "## Putting a backup back into service"
_MANIFEST_LINE = 'MANIFEST="PASTE_THE_MANIFEST"'
_SAFE_PATH = re.compile(r"/[A-Za-z0-9/._ +-]*")
_MANIFEST_NAME = re.compile(r"lab-\d{8}T\d{6}Z\.manifest\.json")


class RestorePlanError(Exception):
    """Something the plan cannot be made from. The message is one line."""


def check_manifest_path(manifest: str) -> None:
    if not _SAFE_PATH.fullmatch(manifest):
        raise RestorePlanError("MANIFEST must be an absolute path made of letters, digits, "
                               "spaces and / . _ + -")
    if not _MANIFEST_NAME.fullmatch(Path(manifest).name):
        raise RestorePlanError("MANIFEST must name a lab-<stamp>.manifest.json file that "
                               "lab backup wrote")


def render(manifest: str) -> str:
    """The runbook section, with the manifest line filled in."""
    check_manifest_path(manifest)
    try:
        lines = RUNBOOK.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise RestorePlanError(f"cannot read the runbook at {RUNBOOK}") from exc
    if SECTION_HEADING not in lines:
        raise RestorePlanError(f"the runbook has no '{SECTION_HEADING[3:]}' section")
    start = lines.index(SECTION_HEADING)
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
               len(lines))
    section = lines[start:end]
    if section.count(_MANIFEST_LINE) != 1:
        raise RestorePlanError("the runbook section does not have exactly one manifest line. "
                               "Update lab/restore_plan.py to match.")
    filled = [f'MANIFEST="{manifest}"' if line == _MANIFEST_LINE else line
              for line in section]
    return "\n".join(filled).rstrip("\n") + "\n"
