"""`lab restore --plan MANIFEST` prints the runbook's steps for putting a backup back
into service (#339)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from lab import restore_plan
from lab.cli import main

MANIFEST = "/Volumes/lab backup/home-lab-backups/lab-20261022T024700Z.manifest.json"


def test_the_plan_is_the_runbook_section_with_the_manifest_filled_in(
        capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["restore", "--plan", MANIFEST]) == 0
    out, err = capsys.readouterr()
    assert err == ""
    assert out.startswith("## Putting a backup back into service\n")
    assert f'MANIFEST="{MANIFEST}"\n' in out
    assert "PASTE_THE_MANIFEST" not in out
    assert "restore-check \"$MANIFEST\"" in out
    assert "update --verify" in out
    # Only the one section: the next heading is not printed.
    assert "## Moving the backup's Full Disk Access" not in out


@pytest.mark.parametrize("manifest", [
    "lab-20261022T024700Z.manifest.json",
    "/backups/lab-20261022T024700Z.db",
    "/backups/other.manifest.json",
    '/backups/"$(id)"/lab-20261022T024700Z.manifest.json',
    "/backups/`id`/lab-20261022T024700Z.manifest.json",
    "/backups/a\nb/lab-20261022T024700Z.manifest.json",
])
def test_a_manifest_path_that_is_not_safe_to_paste_is_refused(
        manifest: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["restore", "--plan", manifest]) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("restore: MANIFEST must ")


def test_a_runbook_without_the_section_is_one_line(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runbook = tmp_path / "runbook.md"
    runbook.write_text("# Runbook\n\n## Something else\n", encoding="utf-8")
    monkeypatch.setattr(restore_plan, "RUNBOOK", runbook)
    with pytest.raises(restore_plan.RestorePlanError, match="no 'Putting a backup"):
        restore_plan.render(MANIFEST)


def test_the_command_does_not_open_the_database(tmp_path: Path) -> None:
    db = tmp_path / "absent.db"
    assert main(["--db", str(db), "restore", "--plan", MANIFEST]) == 0
    assert not db.exists()


def test_an_existing_database_is_not_opened(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "lab.db"
    db.write_bytes(b"not a database")

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the plan opened a database")

    monkeypatch.setattr(sqlite3, "connect", refuse)
    assert main(["--db", str(db), "restore", "--plan", MANIFEST]) == 0
    assert db.read_bytes() == b"not a database"
