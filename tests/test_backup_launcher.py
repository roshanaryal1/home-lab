"""The backup launcher, the one executable that holds Full Disk Access (#287).

macOS ties that grant to an executable, not to an account. The launcher holds
it instead of the interpreter every lab service shares, so what it runs must
not depend on how it is called: no arguments, one fixed command, and an
environment of only PATH and LAB_BACKUP_DIR. These tests build it from the
committed source with the system C compiler, as the runbook does on the Mac
mini, and run it against a stand-in program that records what it was given.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from lab import service
from lab.artifacts import ArtifactStore
from lab.broker import Workspace
from lab.queue import TaskQueue

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "ops" / "backup-launcher" / "lab-backup.c"
CC = shutil.which("cc")
needs_cc = pytest.mark.skipif(CC is None, reason="no C compiler on this machine")

# What the installed launcher runs: the same paths the other service definitions use.
PRODUCTION = service.backup_command(python="/opt/homelab/.venv/bin/python",
                                    db="/var/homelab/lab.db",
                                    alert_config="/etc/homelab/alert.json")
CLEAN_PATH = "PATH=/usr/bin:/bin:/usr/sbin:/sbin"

RECORDER = r"""
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
extern char **environ;
int main(int argc, char *argv[]) {
    FILE *out = fopen(RECORD_TO, "w");
    if (out == NULL) return 99;
    for (int i = 0; i < argc; i++) fprintf(out, "arg %s\n", argv[i]);
    for (char **e = environ; *e != NULL; e++) fprintf(out, "env %s\n", *e);
    fclose(out);
    if (RAISE != 0) raise(RAISE);
    return EXIT_WITH;
}
"""


def _c_string(text: str) -> str:
    assert '"' not in text and "\\" not in text and "\n" not in text
    return f'"{text}"'


def _compile(source: Path, exe: Path, defines: dict[str, str]) -> Path:
    assert CC is not None
    subprocess.run([CC, "-O2", "-Wall", "-Wextra", "-Werror",
                    *(f"-D{name}={value}" for name, value in defines.items()),
                    "-o", str(exe), str(source)],
                   check=True, capture_output=True, timeout=120)
    return exe


def _launcher(tmp: Path, **paths: str) -> Path:
    return _compile(SOURCE, tmp / "lab-backup",
                    {f"LAB_BACKUP_{name.upper()}": _c_string(value)
                     for name, value in paths.items()})


def _recorder(tmp: Path, *, exit_with: int = 0, raise_signal: int = 0) -> tuple[Path, Path]:
    source = tmp / "recorder.c"
    source.write_text(RECORDER)
    record = tmp / "record.txt"
    exe = _compile(source, tmp / "recorder", {"RECORD_TO": _c_string(str(record)),
                                              "EXIT_WITH": str(exit_with),
                                              "RAISE": str(raise_signal)})
    return exe, record


def _read_record(record: Path) -> tuple[list[str], list[str]]:
    lines = record.read_text().splitlines()
    return ([line[4:] for line in lines if line.startswith("arg ")],
            [line[4:] for line in lines if line.startswith("env ")])


def _run(exe: Path, *args: str, env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(exe), *args], env=env, cwd=cwd, capture_output=True,
                          text=True, timeout=120, check=False)


def test_the_built_in_command_is_the_one_the_service_definition_names() -> None:
    defaults = dict(re.findall(r'^#define (LAB_BACKUP_\w+) "([^"]*)"$', SOURCE.read_text(),
                               re.MULTILINE))
    assert defaults == {"LAB_BACKUP_PYTHON": PRODUCTION[0],
                        "LAB_BACKUP_DB": PRODUCTION[PRODUCTION.index("--db") + 1],
                        "LAB_BACKUP_ALERT_CONFIG": PRODUCTION[-1],
                        "LAB_BACKUP_KEEP": str(service.BACKUP_KEEP)}
    assert PRODUCTION[1] == "-I" and "--to" not in PRODUCTION
    assert PRODUCTION[PRODUCTION.index("--keep") + 1] == str(service.BACKUP_KEEP)
    # Outside the clone and the interpreter tree, so neither a code update nor a
    # new Python replaces it: a replaced binary no longer matches its grant.
    assert not service.BACKUP_LAUNCHER.startswith(("/opt/homelab/", "/opt/homelab-python/"))


@needs_cc
@pytest.mark.safety
def test_the_launcher_runs_one_fixed_command_with_only_path_and_the_backup_folder(
        tmp_path: Path) -> None:
    recorder, record = _recorder(tmp_path)
    launcher = _launcher(tmp_path, python=str(recorder))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    hostile = {"PATH": f"{elsewhere}:/usr/bin:/bin", "PYTHONPATH": str(elsewhere),
               "PYTHONSTARTUP": str(elsewhere / "startup.py"), "PYTHONINSPECT": "1",
               "PYTHONHOME": str(elsewhere), "COVERAGE_PROCESS_START": str(elsewhere / "rc"),
               "TMPDIR": str(elsewhere), "HOME": str(elsewhere),
               "LAB_BACKUP_DIR": "/Volumes/labbackup/home-lab-backups"}
    result = _run(launcher, env=hostile, cwd=elsewhere)
    assert result.returncode == 0, result.stderr
    argv, env = _read_record(record)
    assert argv == [str(recorder), *PRODUCTION[1:]]
    assert env == [CLEAN_PATH, "LAB_BACKUP_DIR=/Volumes/labbackup/home-lab-backups"]


@needs_cc
@pytest.mark.safety
def test_the_launcher_refuses_any_argument(tmp_path: Path) -> None:
    recorder, record = _recorder(tmp_path)
    launcher = _launcher(tmp_path, python=str(recorder))
    for extra in (["--to", str(tmp_path)], ["restore-check"], [""]):
        result = _run(launcher, *extra, env={"LAB_BACKUP_DIR": str(tmp_path)}, cwd=tmp_path)
        assert result.returncode == 2
        assert "takes no arguments" in result.stderr
        assert not record.exists()


@needs_cc
def test_without_a_backup_folder_the_command_runs_with_only_path(tmp_path: Path) -> None:
    recorder, record = _recorder(tmp_path)
    launcher = _launcher(tmp_path, python=str(recorder))
    assert _run(launcher, env={"PYTHONPATH": "x"}, cwd=tmp_path).returncode == 0
    assert _read_record(record)[1] == [CLEAN_PATH]


@needs_cc
def test_the_exit_status_reaches_launchd(tmp_path: Path) -> None:
    failing = tmp_path / "failing"
    failing.mkdir()
    recorder, _ = _recorder(failing, exit_with=3)
    assert _run(_launcher(failing, python=str(recorder)), env={}, cwd=tmp_path).returncode == 3

    killed = tmp_path / "killed"
    killed.mkdir()
    recorder, _ = _recorder(killed, raise_signal=int(signal.SIGTERM))
    result = _run(_launcher(killed, python=str(recorder)), env={}, cwd=tmp_path)
    assert result.returncode == 128 + int(signal.SIGTERM)
    assert "signal" in result.stderr

    missing = tmp_path / "missing"
    missing.mkdir()
    result = _run(_launcher(missing, python=str(missing / "no-python")), env={}, cwd=tmp_path)
    assert result.returncode == 127
    assert "cannot start" in result.stderr


@pytest.fixture()
def live(tmp_path: Path):
    db = tmp_path / "live" / "lab.db"
    db.parent.mkdir()
    q = TaskQueue(db)
    q.add_task("one")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_bytes(b"alpha")
    ArtifactStore(db.parent / "artifacts", q._conn).ingest_workspace(Workspace(ws), "t1", 1)
    yield db
    q.close()


@needs_cc
@pytest.mark.safety
def test_the_launcher_runs_a_real_backup_that_nothing_around_it_can_change(
        tmp_path: Path, live: Path) -> None:
    """End to end with this checkout's interpreter. A ``lab`` package planted in
    the working directory and on PYTHONPATH would fail the run if it were
    imported; ``-I`` and the cleared environment keep it out."""
    planted = tmp_path / "planted"
    (planted / "lab").mkdir(parents=True)
    (planted / "lab" / "__init__.py").write_text("raise SystemExit(42)\n")
    (planted / "lab" / "cli.py").write_text("raise SystemExit(42)\n")
    dest = tmp_path / "backups"
    launcher = _launcher(tmp_path, python=sys.executable, db=str(live),
                         alert_config=str(tmp_path / "no-alert.json"))
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(planted),
           "LAB_BACKUP_DIR": str(dest)}

    result = _run(launcher, env=env, cwd=planted)
    assert result.returncode == 0, result.stderr
    assert "restore check ok: " in result.stdout
    assert "kept 1, removed 0 backups" in result.stdout
    assert len(list(dest.glob("lab-*.manifest.json"))) == 1

    del env["LAB_BACKUP_DIR"]
    result = _run(launcher, env=env, cwd=planted)
    assert result.returncode == 1
    assert "LAB_BACKUP_DIR" in result.stderr
