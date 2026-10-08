"""Shipped migrations are pinned in lab/migrations/SHA256SUMS (items 3.1 and #348).

A migration that has run on a deployed database is history. Editing it
changes nothing there, but a fresh database gets the edited text, so the
two drift apart with no error. This test recomputes every hash and fails on
a changed file, a file the list leaves out, and a line for a file that is gone.
"""

from __future__ import annotations

import hashlib
import re

from lab.migrations import MIGRATIONS_DIR

SUMS = MIGRATIONS_DIR / "SHA256SUMS"
LINE = re.compile(r"^([0-9a-f]{64})  ([0-9]{4}_[a-z0-9_]+\.sql)$")
ADVICE = (
    "Do not edit a shipped migration. Add a new migration with the next number instead, "
    "and add the new file's line to lab/migrations/SHA256SUMS "
    "(sha256sum format: the hash, two spaces, the file name)."
)


def _listed() -> dict[str, str]:
    listed: dict[str, str] = {}
    names: list[str] = []
    for line in SUMS.read_text(encoding="utf-8").splitlines():
        match = LINE.match(line)
        assert match is not None, f"SHA256SUMS line is not 'sha256  filename': {line!r}"
        listed[match.group(2)] = match.group(1)
        names.append(match.group(2))
    assert len(names) == len(set(names)), "SHA256SUMS lists a file twice"
    assert names == sorted(names), "SHA256SUMS must list the files in sorted order"
    return listed


def test_shipped_migrations_match_their_pinned_hashes() -> None:
    shipped = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in MIGRATIONS_DIR.glob("*.sql")
    }
    listed = _listed()
    changed = sorted(name for name in listed.keys() & shipped.keys()
                     if listed[name] != shipped[name])
    unlisted = sorted(shipped.keys() - listed.keys())
    missing = sorted(listed.keys() - shipped.keys())

    problems = []
    if changed:
        problems.append("changed since they were pinned: " + ", ".join(changed))
    if unlisted:
        problems.append("not in SHA256SUMS: " + ", ".join(unlisted))
    if missing:
        problems.append("listed but missing from lab/migrations: " + ", ".join(missing))
    assert not problems, "\n".join([*problems, ADVICE])
