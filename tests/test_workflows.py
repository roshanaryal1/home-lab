"""Workflow hygiene beyond what zizmor checks (issue #62).

zizmor audits the workflows in CI. These tests keep two properties from
being weakened by an edit: every third-party action is pinned to a full
commit SHA, and the code-scanning workflows ask for no more than they need.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WORKFLOWS = sorted((Path(__file__).resolve().parent.parent / ".github" / "workflows").glob("*.yml"))
USES = re.compile(r"^\s*-?\s*uses:\s*(\S+)", re.M)
PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


def test_there_are_workflows() -> None:
    assert {w.name for w in WORKFLOWS} >= {"check.yml", "scorecard.yml"}


@pytest.mark.safety
@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_full_commit_sha(path: Path) -> None:
    for ref in USES.findall(path.read_text()):
        assert PINNED.match(ref), f"{path.name}: {ref} is not pinned to a 40-hex commit"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_checkout_never_keeps_credentials(path: Path) -> None:
    text = path.read_text()
    for match in re.finditer(r"uses:\s*actions/checkout@\S+.*\n((?:\s+.*\n){0,4})", text):
        assert "persist-credentials: false" in match.group(1), path.name


def test_scorecard_grants_only_what_it_uses() -> None:
    text = (WORKFLOWS[[w.name for w in WORKFLOWS].index("scorecard.yml")]).read_text()
    assert re.search(r"^permissions:\s*\{\}", text, re.M), "top-level permissions must be empty"
    granted = set(re.findall(r"^\s{6}([a-z-]+): (?:read|write)$", text, re.M))
    assert granted <= {"security-events", "id-token", "contents", "actions"}
    assert "pull_request_target" not in text
    assert "secrets." not in text


def test_the_workflow_is_documented_as_public_repo_only() -> None:
    root = Path(__file__).resolve().parent.parent
    assert "OpenSSF Scorecard" in (root / "SECURITY.md").read_text()
    assert "public" in (root / "README.md").read_text().lower()
