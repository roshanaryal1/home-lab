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


def test_keepawake_can_run_as_the_lab_account() -> None:
    """#235: keepawake needs no privilege. The generator can drop root; the
    committed copy switches once the operator's caffeinate check passes."""
    args = {"python": "/opt/lab/.venv/bin/python", "workdir": "/opt/lab", "db": "/var/lab/lab.db"}
    assert "UserName" not in plistlib.loads(service.keepawake_plist(**args))
    as_lab = plistlib.loads(service.keepawake_plist(**args, user="lab"))
    assert as_lab["UserName"] == "lab"
    assert as_lab["KeepAlive"] is True and "keepawake" in as_lab["ProgramArguments"]


def test_ops_copies_of_the_plists_are_current(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parent.parent / "ops" / "launchd"
    py, wd, db = "/opt/homelab/.venv/bin/python", "/opt/homelab", "/var/homelab/lab.db"
    assert (root / "com.homelab.supervisor.plist").read_bytes() == service.supervisor_plist(
        user="lab", python=py, workdir=wd, db=db)
    assert (root / "com.homelab.watchdog.plist").read_bytes() == service.watchdog_plist(
        python=py, workdir=wd, db=db)
    assert (root / "com.homelab.keepawake.plist").read_bytes() == service.keepawake_plist(
        python=py, workdir=wd, db=db)
    assert (root / "com.homelab.tick.plist").read_bytes() == service.tick_plist(
        user="lab", python=py, workdir=wd, db=db)
    assert (root / "com.homelab.chat.plist").read_bytes() == service.chat_plist(
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


# ---------------------------------------- a supervisor that never wrote a heartbeat (#271)

REAL_PYTHON = os.path.realpath(sys.executable)


def _stand_in_package(root: Path) -> Path:
    """A tiny ``lab.supervisor`` that only sleeps, so a process can have exactly the command
    line the real one has: ``<python> -m lab.supervisor --db <db>``."""
    package = root / "stand_in" / "lab"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("")
    (package / "supervisor.py").write_text("import time\ntime.sleep(120)\n")
    return package.parent


def _spawn_unbeaten(db: Path, *, db_arg: str | None = None) -> subprocess.Popen[bytes]:
    """A process whose command line is this db's supervisor, and which never beats."""
    if " " in REAL_PYTHON:
        pytest.skip("the interpreter path contains a space; ps cannot show it unambiguously")
    root = _stand_in_package(db.parent)
    return subprocess.Popen([REAL_PYTHON, "-m", "lab.supervisor", "--db", db_arg or str(db)],
                            cwd=root)


def _wait_listed(db: Path, proc: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if proc.pid in [pid for pid, _ in service.supervisor_processes(db) or []]:
            return
        time.sleep(0.1)
    raise AssertionError("the stand-in never showed up in ps")


def test_etime_is_parsed_in_all_its_forms() -> None:
    assert service._etime_seconds("00:05") == 5
    assert service._etime_seconds("12:34") == 12 * 60 + 34
    assert service._etime_seconds("01:02:03") == 3723
    assert service._etime_seconds("2-03:04:05") == 2 * 86400 + 3 * 3600 + 4 * 60 + 5
    for bad in ("", "abc", "1:2:3:4", "5"):
        assert service._etime_seconds(bad) is None


def test_the_supervisor_is_found_by_an_exact_command_line_and_nothing_else(
        tmp_path: Path) -> None:
    db = tmp_path / "lab.db"
    other = tmp_path / "other" / "lab.db"
    other.parent.mkdir()
    ours, theirs = _spawn_unbeaten(db), _spawn_unbeaten(other)
    longer = _spawn_unbeaten(db, db_arg=str(db) + "x")
    try:
        _wait_listed(db, ours)
        _wait_listed(other, theirs)
        assert [pid for pid, _ in service.supervisor_processes(db) or []] == [ours.pid]
        assert [pid for pid, _ in service.supervisor_processes(other) or []] == [theirs.pid]
    finally:
        for proc in (ours, theirs, longer):
            proc.kill()


@pytest.mark.safety
def test_a_process_that_merely_mentions_the_command_is_never_picked_up(tmp_path: Path) -> None:
    """Review of #275: the words may appear in the arguments of something else entirely."""
    db = tmp_path / "lab.db"
    words = ["-m", "lab.supervisor", "--db", str(db)]
    impostors = [
        subprocess.Popen([REAL_PYTHON, "-c", "import time; time.sleep(120)", *words]),
        subprocess.Popen(["/bin/sleep", "120"]),
        subprocess.Popen(["/bin/sh", "-c", "sleep 120", "x", *words]),
    ]
    try:
        time.sleep(0.5)
        assert service.supervisor_processes(db) == []
        time.sleep(2.7)                          # older than max_age, with no heartbeat
        assert service.check(db, max_age=2).action == "no_heartbeat"
        assert all(proc.poll() is None for proc in impostors), "an impostor was killed"
    finally:
        for proc in impostors:
            proc.kill()


@pytest.mark.safety
def test_a_supervisor_that_hung_before_its_first_heartbeat_is_killed(tmp_path: Path) -> None:
    db = tmp_path / "lab.db"
    proc = _spawn_unbeaten(db)
    try:
        _wait_listed(db, proc)
        time.sleep(3.2)                      # older than max_age, and no heartbeat file
        assert not (tmp_path / "lab.db.heartbeat").exists()
        verdict = service.check(db, max_age=2, dry_run=True)
        assert verdict.action == "would_kill" and verdict.pid == proc.pid
        assert proc.poll() is None, "a dry run must not kill"
        verdict = service.check(db, max_age=2)
        assert verdict.action == "killed" and verdict.pid == proc.pid
        assert verdict.uptime is not None and verdict.uptime > 2
        proc.wait(timeout=5)
        assert proc.returncode == -9
    finally:
        if proc.poll() is None:
            proc.kill()


@pytest.mark.safety
def test_the_exact_2026_10_06_case_a_stale_heartbeat_names_a_dead_pid(tmp_path: Path) -> None:
    """The first drill killed pid A; launchd started B; B was frozen before it beat, so the
    file still named dead A and the watchdog took no action for 182 s."""
    db = tmp_path / "lab.db"
    (tmp_path / "lab.db.heartbeat").write_text(
        json.dumps({"pid": 2**22 + 777, "ts": time.time() - 600}))
    proc = _spawn_unbeaten(db)
    try:
        _wait_listed(db, proc)
        time.sleep(3.2)
        verdict = service.check(db, max_age=2)
        assert verdict.action == "killed" and verdict.pid == proc.pid
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()


@pytest.mark.safety
@pytest.mark.parametrize("content", ["not json", "{}", '{"pid": "x"}', "[]", ""])
def test_an_unreadable_heartbeat_does_not_hide_a_hung_supervisor(
        tmp_path: Path, content: str) -> None:
    db = tmp_path / "lab.db"
    (tmp_path / "lab.db.heartbeat").write_text(content)
    proc = _spawn_unbeaten(db)
    try:
        _wait_listed(db, proc)
        time.sleep(3.2)
        verdict = service.check(db, max_age=2)
        assert verdict.action == "killed" and verdict.pid == proc.pid
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()


def test_an_unreadable_heartbeat_with_no_supervisor_is_still_corrupt(tmp_path: Path) -> None:
    db = tmp_path / "lab.db"
    (tmp_path / "lab.db.heartbeat").write_text("not json")
    assert service.check(db, max_age=2).action == "corrupt"


def test_a_supervisor_that_has_only_just_started_is_given_time(tmp_path: Path) -> None:
    db = tmp_path / "lab.db"
    proc = _spawn_unbeaten(db)
    try:
        _wait_listed(db, proc)
        verdict = service.check(db, max_age=600)
        assert verdict.action == "starting" and verdict.pid == proc.pid
        assert proc.poll() is None
    finally:
        proc.kill()


def test_no_supervisor_and_no_heartbeat_is_still_just_a_report(tmp_path: Path) -> None:
    db = tmp_path / "lab.db"
    assert service.check(db, max_age=2).action == "no_heartbeat"
    (tmp_path / "lab.db.heartbeat").write_text(
        json.dumps({"pid": 2**22 + 777, "ts": time.time() - 600}))
    assert service.check(db, max_age=2).action == "not_running"


@pytest.mark.safety
def test_an_unrelated_process_is_never_killed_by_the_fallback(tmp_path: Path) -> None:
    db = tmp_path / "lab.db"
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        time.sleep(3.2)
        assert service.check(db, max_age=2).action == "no_heartbeat"
        assert bystander.poll() is None
    finally:
        bystander.kill()


@pytest.mark.safety
def test_the_identity_is_checked_again_just_before_the_signal(tmp_path: Path) -> None:
    db = tmp_path / "lab.db"
    proc = _spawn_unbeaten(db)
    try:
        _wait_listed(db, proc)
        time.sleep(0.5)
        up = dict(service.supervisor_processes(db) or [])[proc.pid]
        assert service._still_that_supervisor(db, proc.pid, up)
        assert not service._still_that_supervisor(db, proc.pid, up + 1000), \
            "a younger process holding the pid (a reused pid) must not count"
        assert service._still_that_supervisor(db, proc.pid, 0)
        assert not service._still_that_supervisor(tmp_path / "other.db", proc.pid, 0)
        assert not service._still_that_supervisor(db, 2**22 + 99, 0), "no such process"
        assert not service._still_that_supervisor(db, os.getpid(), 0), "not a supervisor"
    finally:
        proc.kill()


@pytest.mark.safety
def test_a_pid_that_changed_hands_after_the_listing_is_not_signalled(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "lab.db"
    proc = _spawn_unbeaten(db)
    try:
        _wait_listed(db, proc)
        time.sleep(3.2)
        monkeypatch.setattr(service, "_still_that_supervisor", lambda *a, **k: False)
        assert service.check(db, max_age=2).action == "no_heartbeat"
        assert proc.poll() is None, "it was signalled although the recheck said no"
    finally:
        proc.kill()


def test_a_failed_process_lookup_is_reported_and_not_mistaken_for_nothing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "lab.db"
    for failure in (None, (1, ""), (2, "partial output")):
        monkeypatch.setattr(service, "_ps", lambda *a, _f=failure: _f)
        assert service.supervisor_processes(db) is None
        verdict = service.check(db, max_age=2)
        assert verdict.action == "lookup_failed"
    assert main(["--db", str(db), "watchdog", "--max-age", "2"]) == 1
    assert capsys.readouterr().out.startswith("watchdog: lookup_failed")


def test_cli_watchdog_reports_the_uptime_and_exits_2_when_it_kills(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "lab.db"
    proc = _spawn_unbeaten(db)
    try:
        _wait_listed(db, proc)
        time.sleep(3.2)
        assert main(["--db", str(db), "watchdog", "--max-age", "2"]) == 2
        out = capsys.readouterr().out
        assert out.startswith(f"watchdog: killed pid {proc.pid} (process up ")
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
