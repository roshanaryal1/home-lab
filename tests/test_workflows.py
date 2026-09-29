"""Workflow hygiene beyond what zizmor checks (issue #62).

zizmor audits the workflows in CI. These tests keep two properties from
being weakened by an edit: every third-party action is pinned to a full
commit SHA, and the code-scanning workflows ask for no more than they need.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WORKFLOW_DIR = Path(__file__).resolve().parent.parent / ".github" / "workflows"
WORKFLOWS = sorted([*WORKFLOW_DIR.glob("*.yml"), *WORKFLOW_DIR.glob("*.yaml")])
STEP_START = re.compile(r"^ {6}- ", re.M)
USES = re.compile(r"^\s*-?\s*uses:\s*(\S+)", re.M)
PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


def test_there_are_workflows() -> None:
    assert {w.name for w in WORKFLOWS} >= {"check.yml", "scorecard.yml"}


@pytest.mark.safety
@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_full_commit_sha(path: Path) -> None:
    for ref in USES.findall(path.read_text()):
        assert PINNED.match(ref), f"{path.name}: {ref} is not pinned to a 40-hex commit"


def _steps(text: str) -> list[str]:
    """Each step of each job as its own block of text (a step starts at a
    six-space ``- `` and runs to the next one)."""
    starts = [m.start() for m in STEP_START.finditer(text)]
    return [text[a:b] for a, b in zip(starts, [*starts[1:], len(text)], strict=True)]


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_checkout_never_keeps_credentials(path: Path) -> None:
    checkouts = [step for step in _steps(path.read_text()) if "actions/checkout@" in step]
    assert checkouts, f"{path.name}: expected a checkout step"
    for step in checkouts:
        assert re.search(r"^\s+persist-credentials:\s*false\s*$", step, re.M), \
            f"{path.name}: a checkout step keeps credentials"


def test_the_step_splitter_isolates_each_step() -> None:
    text = ("    steps:\n"
            "      - uses: actions/checkout@" + "a" * 40 + "\n"
            "      - name: other\n        with:\n          persist-credentials: false\n")
    steps = _steps(text)
    assert len(steps) == 2 and "persist-credentials" not in steps[0]


def test_scorecard_grants_only_what_it_uses() -> None:
    text = (WORKFLOW_DIR / "scorecard.yml").read_text()
    assert re.search(r"^permissions:\s*\{\}", text, re.M), "top-level permissions must be empty"
    granted = dict(re.findall(r"^\s{6}([a-z-]+): (read|write)\b", text, re.M))
    assert granted == {"security-events": "write", "id-token": "write",
                       "contents": "read", "actions": "read"}
    assert "pull_request_target" not in text
    assert "secrets." not in text


def test_the_workflow_is_documented_as_public_repo_only() -> None:
    root = Path(__file__).resolve().parent.parent
    assert "OpenSSF Scorecard" in (root / "SECURITY.md").read_text()
    assert "public" in (root / "README.md").read_text().lower()
