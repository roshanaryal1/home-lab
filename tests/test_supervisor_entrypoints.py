"""Every place that builds a Supervisor is a decision about approval signatures (#190, #200).

A Supervisor built without an operator key does not check approval
signatures. That is right for tests and for a throwaway database, and wrong
for anything that can run against the lab's real database. This test does
not judge the code; it fails when a construction site appears or disappears
so that whoever adds one has to write down which case it is.
"""

from __future__ import annotations

import re
from pathlib import Path

LAB = Path(__file__).resolve().parent.parent / "lab"

# file -> why an unsigned or keyed Supervisor is right there
KNOWN_SITES = {
    "supervisor.py": "the daemon entry point: require_operator_key unless --allow-unsigned",
    "loop.py": "lab tick: the CLI requires the key unless --allow-unsigned or a mock reply",
    "attacks.py": "the attack harness: a temporary database and a dummy secret only",
}

CALL = re.compile(r"(?<![\w.])Supervisor\(")


def _sites() -> dict[str, int]:
    found: dict[str, int] = {}
    for path in sorted(LAB.glob("*.py")):
        count = sum(1 for line in path.read_text().splitlines()
                    if CALL.search(line) and not line.lstrip().startswith(("class ", "#", '"""')))
        if count:
            found[path.name] = count
    return found


def test_no_new_supervisor_construction_site_without_a_recorded_reason() -> None:
    assert set(_sites()) == set(KNOWN_SITES), (
        "a Supervisor is now built in a file that is not listed in KNOWN_SITES. If it can "
        "run against a real database it must set require_operator_key; add it here with the "
        f"reason. Found: {_sites()}")


def test_the_service_entry_points_ask_for_the_operator_key() -> None:
    for name in ("supervisor.py", "loop.py"):
        assert "require_operator_key" in (LAB / name).read_text(), name
