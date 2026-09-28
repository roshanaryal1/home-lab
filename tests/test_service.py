"""launchd service, heartbeat and watchdog (item 6.2, #78).

KeepAlive restarts a dead process, not a hung one. The supervisor writes
a heartbeat from its event loop; a loop that is blocked stops beating,
and the watchdog kills that process so launchd restarts it.
"""

from __future__ import annotations

import asyncio
import json
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lab import service
from lab.cli import main
from lab.supervisor import Supervisor, SupervisorConfig


def test_supervisor_plist_restarts_with_bounded_backoff_and_runs_as_lab() -> None:
    data = plistlib.loads(service.supervisor_plist(
        user="lab", python="/opt/lab/.venv/bin/python", workdir="/opt/lab",
        db="/var/lab/lab.db"))
    assert data["Label"] == service.SUPERVISOR_LABEL
    assert data["UserName"] == "lab"
    assert data["RunAtLoad"] is True
    assert data["KeepAlive"] is True
    assert data["ThrottleInterval"] >= 10, "a crash loop must be throttled"
    assert data["ExitTimeOut"] >= 10
    assert data["ProgramArguments"][0] == "/opt/lab/.venv/bin/python"
    assert "/var/lab/lab.db" in data["ProgramArguments"]


def test_watchdog_plist_runs_on_an_interval_as_root() -> None:
    data = plistlib.loads(service.watchdog_plist(
        python="/opt/lab/.venv/bin/python", workdir="/opt/lab", db="/var/lab/lab.db"))
    assert data["Label"] == service.WATCHDOG_LABEL
    assert data["StartInterval"] <= 60
    assert "KeepAlive" not in data, "the watchdog is a periodic job, not a daemon"
    assert "watchdog" in data["ProgramArguments"]


def test_ops_copies_of_the_plists_are_current(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parent.parent / "ops" / "launchd"
    py, wd, db = "/opt/homelab/.venv/bin/python", "/opt/homelab", "/var/homelab/lab.db"
    assert (root / "com.homelab.supervisor.plist").read_bytes() == service.supervisor_plist(
        user="lab", python=py, workdir=wd, db=db)
    assert (root / "com.homelab.watchdog.plist").read_bytes() == service.watchdog_plist(
        python=py, workdir=wd, db=db)
    assert (root / "com.homelab.tick.plist").read_bytes() == service.tick_plist(
        user="lab", python=py, workdir=wd, db=db)


def test_a_running_supervisor_beats(tmp_path: Path) -> None:
    async def scenario() -> None:
        sup = Supervisor(SupervisorConfig(db_path=tmp_path / "lab.db",
                                          idle_poll_seconds=0.01,
                                          heartbeat_seconds=0.05))
        run = asyncio.create_task(sup.run())
        await asyncio.sleep(0.3)
        beat = service.read_heartbeat(tmp_path / "lab.db")
        assert beat is not None and beat.pid == os.getpid()
        assert beat.age() < 1.0
        sup.stop()
        await asyncio.wait_for(run, 5)
        sup.close()

    asyncio.run(scenario())


def _spawn_hung(db: Path) -> subprocess.Popen[bytes]:
    """A process that beat once, long ago, and is now stuck."""
    code = (
        "import sys, time, pathlib\n"
        "from lab import service\n"
        "service.write_heartbeat(pathlib.Path(sys.argv[1]), stamp=time.time() - 600)\n"
        "print('ready', flush=True)\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code, str(db)],
                            stdout=subprocess.PIPE)
    assert proc.stdout is not None and proc.stdout.readline().strip() == b"ready"
    return proc


@pytest.mark.safety
def test_a_hung_supervisor_is_killed_so_launchd_can_restart_it(tmp_path: Path) -> None:
    db = tmp_path / "lab.db"
    proc = _spawn_hung(db)
    try:
        # the heartbeat names the hung child, not this test process
        beat = service.read_heartbeat(db)
        assert beat is not None and beat.pid == proc.pid
        verdict = service.check(db, max_age=60)
        assert verdict.action == "killed" and verdict.pid == proc.pid
        proc.wait(timeout=5)
        assert proc.returncode == -9
    finally:
        if proc.poll() is None:
            proc.kill()


def test_a_fresh_heartbeat_is_left_alone(tmp_path: Path) -> None:
    db = tmp_path / "lab.db"
    service.write_heartbeat(db)
    verdict = service.check(db, max_age=60)
    assert verdict.action == "healthy"


def test_no_heartbeat_or_a_dead_pid_is_not_killed(tmp_path: Path) -> None:
    db = tmp_path / "lab.db"
    assert service.check(db, max_age=60).action == "no_heartbeat"
    (tmp_path / "lab.db.heartbeat").write_text(
        json.dumps({"pid": 2**22 + 12345, "ts": time.time() - 600}))
    assert service.check(db, max_age=60).action == "not_running"


@pytest.mark.safety
def test_the_watchdog_never_signals_a_pid_it_did_not_read_from_the_file(tmp_path: Path) -> None:
    db = tmp_path / "lab.db"
    for bad in (0, 1, -5, "12", None, True):
        (tmp_path / "lab.db.heartbeat").write_text(json.dumps({"pid": bad, "ts": 1.0}))
        assert service.check(db, max_age=60).action in {"corrupt", "not_running"}
    (tmp_path / "lab.db.heartbeat").write_text("not json")
    assert service.check(db, max_age=60).action == "corrupt"


def test_dry_run_reports_without_killing(tmp_path: Path) -> None:
    db = tmp_path / "lab.db"
    proc = _spawn_hung(db)
    try:
        assert service.check(db, max_age=60, dry_run=True).action == "would_kill"
        assert proc.poll() is None
    finally:
        proc.kill()


def test_cli_watchdog_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "lab.db"
    service.write_heartbeat(db)
    assert main(["--db", str(db), "watchdog", "--max-age", "60"]) == 0
    proc = _spawn_hung(db)
    try:
        assert main(["--db", str(db), "watchdog", "--max-age", "60"]) == 2
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()


@pytest.mark.safety
def test_a_reused_pid_is_not_killed(tmp_path: Path) -> None:
    """The heartbeat names a pid and its start time; a different process now
    holding that pid is not the supervisor and must be left alone."""
    db = tmp_path / "lab.db"
    (tmp_path / "lab.db.heartbeat").write_text(json.dumps(
        {"pid": os.getpid(), "ts": time.time() - 600, "started": "Thu Jan  1 00:00:00 1970"}))
    verdict = service.check(db, max_age=60)
    assert verdict.action == "not_running"
