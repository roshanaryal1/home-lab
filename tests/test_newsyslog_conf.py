"""The newsyslog rules for the launchd logs have the shape newsyslog.conf(5) needs (#349).

This checks the shape only. Whether macOS accepts the file is what
`sudo newsyslog -nvv` shows on the Mac mini, which the runbook has.
"""

from __future__ import annotations

import re
from pathlib import Path

CONF = Path(__file__).resolve().parent.parent / "ops" / "newsyslog" / "homelab.conf"
LOG_DIR = "/var/log/homelab/"


def rules() -> list[list[str]]:
    lines = [line.strip() for line in CONF.read_text().splitlines()]
    return [line.split() for line in lines if line and not line.startswith("#")]


def test_the_file_covers_the_log_and_the_err_files() -> None:
    assert {fields[0] for fields in rules()} == {LOG_DIR + "*.log", LOG_DIR + "*.err"}


def test_every_rule_has_seven_fields() -> None:
    for fields in rules():
        assert len(fields) == 7, fields


def test_every_rule_names_the_launchd_logs_and_has_valid_values() -> None:
    for path, _owner, mode, count, size, _when, flags in rules():
        assert path.startswith(LOG_DIR), path
        assert re.fullmatch(r"[0-7]{3,4}", mode), mode
        assert count.isdigit() and size.isdigit(), (count, size)
        assert "G" in flags, flags


def test_the_logs_stay_private_to_the_lab_account() -> None:
    for _path, owner, mode, *_rest in rules():
        assert owner == "lab:", owner
        assert mode == "600", mode


def test_rotation_signals_nothing_and_never_deletes_a_copy_a_daemon_still_writes() -> None:
    # A KeepAlive daemon keeps writing to the renamed copy until it restarts.
    # Compression (J bzip2, X xz, Y zstd, Z gzip) would delete that copy.
    for *_rest, flags in rules():
        assert "N" in flags, flags
        assert not set(flags) & set("JXYZ"), flags
