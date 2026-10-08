"""Every deploy step in the runbook keeps pytest installed (#270).

The nightly self-test runs the safety tests with the deployed interpreter, so
pytest must be in /opt/homelab/.venv. pytest is in the dev extra, and a plain
`uv sync --locked` removes it, which made the self-test fail every night.
"""

from __future__ import annotations

import re
from pathlib import Path

RUNBOOK = Path(__file__).resolve().parent.parent / "ops" / "runbook-lab-account-and-daemons.md"


def _shell_blocks(text: str) -> list[str]:
    return re.findall(r"^```sh\n(.*?)^```", text, flags=re.MULTILINE | re.DOTALL)


def test_every_deploy_sync_in_the_runbook_installs_the_dev_extra() -> None:
    syncs = [line for block in _shell_blocks(RUNBOOK.read_text(encoding="utf-8"))
             for line in block.replace("\\\n", " ").splitlines()
             if re.search(r"\bsync --locked\b", line)]
    assert syncs, "no uv sync step found in the runbook"
    missing = [line.strip() for line in syncs if "--extra dev" not in line]
    assert not missing, f"these deploy steps would remove pytest: {missing}"
