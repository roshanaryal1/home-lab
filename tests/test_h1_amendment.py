"""The hashes the H1 amendment says are frozen are the hashes of the files (#84)."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AMENDMENT = ROOT / "docs" / "PREREGISTRATION-AMENDMENT-2-DRAFT.md"
ROW = re.compile(r"^\|(?P<what>[^|]*)\|(?P<value>[^|]*)\|$", re.MULTILINE)
PATH = re.compile(r"`((?:evals|lab)/[A-Za-z0-9_./-]+)`")
HASH = re.compile(r"`([0-9a-f]{64})`")


ADJACENT = re.compile(r"`((?:evals|lab)/[A-Za-z0-9_./-]+)`\s*`([0-9a-f]{64})`")


def _frozen() -> list[tuple[str, str]]:
    """(path, sha256) pairs the amendment's table states: a path in the first column with a
    hash in the second, or a path written directly before its hash."""
    text = AMENDMENT.read_text(encoding="utf-8")
    section = text[text.index("## What is frozen"):]
    section = section[:section.index("\n\n", section.index("|---|---|"))]
    pairs = []
    for row in ROW.finditer(section):
        paths, hashes = PATH.findall(row["what"]), HASH.findall(row["value"])
        if len(paths) == 1 and len(hashes) == 1:
            pairs.append((paths[0], hashes[0]))
        else:
            pairs += ADJACENT.findall(row["value"])
    return pairs


def test_every_hash_in_the_amendment_is_the_hash_of_its_file() -> None:
    pairs = _frozen()
    named = {path for path, _ in pairs}
    # Every file the amendment freezes is in the table, so none can be dropped quietly.
    for expected in (
            "evals/shadow_cases_DRAFT.jsonl", "evals/h1_review/extra-cases-UNLABELED.jsonl",
            "evals/h1_review/review-sheet.md", "evals/h1_review/answers-template.json",
            "evals/h1_review/ai-reviewer-prompt.md", "evals/h1_review/answers-owner.json",
            "evals/h1_review/answers-owner-spares.json",
            "evals/h1_review/spare-cases-UNLABELED.jsonl", "evals/h1_review/spares/review-sheet.md",
            "evals/h1_review/spares/answers-template.json",
            "lab/shadow.py", "lab/rubric.py", "lab/reviewsheet.py"):
        assert expected in named, f"{expected} is not frozen in the amendment"
    wrong = [(path, want) for path, want in pairs
             if hashlib.sha256((ROOT / path).read_bytes()).hexdigest() != want]
    assert not wrong, f"the file changed after the amendment froze it: {wrong}"
