"""The newsyslog rules for the launchd logs have the shape newsyslog.conf(5) needs (#349).

This checks the shape, and which files are in the rules, against the service
definitions in ops/launchd. Whether macOS accepts the file is what
`sudo newsyslog -nvv` shows on the Mac mini, which the runbook has.
"""

from __future__ import annotations

import plistlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONF = ROOT / "ops" / "newsyslog" / "homelab.conf"
LAUNCHD = ROOT / "ops" / "launchd"
LOG_DIR = "/var/log/homelab/"


def rules() -> list[list[str]]:
    lines = [line.strip() for line in CONF.read_text().splitlines()]
    return [line.split() for line in lines if line and not line.startswith("#")]


def launchd_logs(*, keep_alive: bool) -> set[str]:
    """The stdout and stderr files of the jobs in ops/launchd that do or do not run
    with KeepAlive."""
    found: set[str] = set()
    for path in LAUNCHD.glob("com.homelab.*.plist"):
        job = plistlib.loads(path.read_bytes())
        if bool(job.get("KeepAlive")) == keep_alive:
            found |= {job[key] for key in ("StandardOutPath", "StandardErrorPath") if key in job}
    return found


def test_every_scheduled_job_has_its_two_files_rotated() -> None:
    rotated = {fields[0] for fields in rules()}
    assert launchd_logs(keep_alive=False) <= rotated


def test_no_daemon_that_holds_its_files_open_is_rotated() -> None:
    held_open = launchd_logs(keep_alive=True)
    assert held_open, "chat, keepawake and supervisor run with KeepAlive"
    assert not held_open & {fields[0] for fields in rules()}


def test_every_rule_has_seven_fields_and_names_one_file() -> None:
    for fields in rules():
        assert len(fields) == 7, fields
        path = fields[0]
        assert path.startswith(LOG_DIR) and re.fullmatch(r"[a-z-]+\.(log|err)", path[len(LOG_DIR):])
        assert "G" not in fields[6], fields       # one named file, not a pattern


def test_the_logs_stay_private_to_the_lab_account() -> None:
    for _path, owner, mode, count, size, _when, _flags in rules():
        assert owner == "lab:", owner
        assert mode == "600", mode
        assert count.isdigit() and size.isdigit(), (count, size)


def test_rotation_compresses_and_signals_nothing() -> None:
    for *_rest, flags in rules():
        assert set(flags) == {"J", "N"}, flags
