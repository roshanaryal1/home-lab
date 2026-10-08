"""Nightly self-test and the alert hook (H5b).

The self-test proves, on the machine that runs the lab, that the audit chain
verifies, that a backup restores, that the health check is not red and that
the safety-marked tests still pass. A failure runs the operator's alert
command. That command comes only from an operator-owned configuration file:
never from the database, a task, or a model.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from lab import alert, selftest
from lab.cli import main
from lab.queue import TaskQueue


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    path = tmp_path / "lab.db"
    with TaskQueue(path, owner="t") as q:
        q.add_task("one")
    return path


def _config(tmp_path: Path, command: list[str], **extra: object) -> Path:
    path = tmp_path / "alert.json"
    path.write_text(json.dumps({"command": command, **extra}))
    path.chmod(0o600)
    return path


def _recorder(tmp_path: Path) -> tuple[Path, Path]:
    """A tiny notifier that records its argv and stdin."""
    out = tmp_path / "recorded.json"
    script = tmp_path / "notify.py"
    script.write_text(
        "import json, sys\n"
        f"open({str(out)!r}, 'a').write(json.dumps({{'argv': sys.argv[1:], "
        "'stdin': sys.stdin.read()}) + '\\n')\n")
    script.chmod(0o700)
    return script, out


# ------------------------------------------------------------- self-test


def test_a_healthy_lab_passes_every_check(db: Path) -> None:
    report = selftest.run(db, run_safety_tests=False)
    assert report.ok
    assert {c.name for c in report.checks} >= {"audit_chain", "backup_restore", "health"}
    assert all(c.ok for c in report.checks)


@pytest.mark.safety
def test_a_broken_audit_chain_fails_the_selftest(db: Path) -> None:
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute("DROP TRIGGER events_no_update")
    conn.execute("UPDATE events SET detail = '{\"forged\": true}' WHERE id = 1")
    conn.commit()
    conn.close()
    report = selftest.run(db, run_safety_tests=False)
    assert not report.ok
    failed = {c.name for c in report.checks if not c.ok}
    assert "audit_chain" in failed


def test_the_result_is_written_to_the_audit_log(db: Path) -> None:
    selftest.run(db, run_safety_tests=False)
    with TaskQueue(db, owner="t") as q:
        row = q._conn.execute(
            "SELECT detail FROM events WHERE kind = 'selftest' ORDER BY id DESC").fetchone()
    detail = json.loads(row["detail"])
    assert detail["ok"] is True and "checks" in detail


def test_the_safety_tests_run_when_a_tests_directory_exists(db: Path, tmp_path: Path) -> None:
    tests = tmp_path / "t"
    tests.mkdir()
    (tests / "test_ok.py").write_text(
        "import pytest\n@pytest.mark.safety\ndef test_ok():\n    assert True\n")
    (tests / "conftest.py").write_text("")
    (tests / "pytest.ini").write_text("[pytest]\nmarkers = safety\n")
    report = selftest.run(db, tests_dir=tests, run_safety_tests=True)
    check = next(c for c in report.checks if c.name == "safety_tests")
    assert check.ok and "1 passed" in check.detail


def test_a_failing_safety_test_fails_the_selftest(db: Path, tmp_path: Path) -> None:
    tests = tmp_path / "t"
    tests.mkdir()
    (tests / "test_bad.py").write_text(
        "import pytest\n@pytest.mark.safety\ndef test_bad():\n    assert False\n")
    (tests / "pytest.ini").write_text("[pytest]\nmarkers = safety\n")
    report = selftest.run(db, tests_dir=tests, run_safety_tests=True)
    assert not report.ok


def test_without_a_tests_directory_the_safety_check_is_skipped_not_failed(
        db: Path, tmp_path: Path) -> None:
    report = selftest.run(db, tests_dir=tmp_path / "missing", run_safety_tests=True)
    check = next(c for c in report.checks if c.name == "safety_tests")
    assert check.ok and check.detail.startswith("skipped")


# ------------------------------------------------------------- alert hook


def test_the_hook_gets_the_message_on_stdin_and_nothing_extra_in_argv(tmp_path: Path) -> None:
    script, out = _recorder(tmp_path)
    cfg = alert.load(_config(tmp_path, [sys.executable, str(script), "--title", "lab"]))
    assert alert.send(cfg, kind="unhealthy", message="1 task on an expired lease")
    (record,) = [json.loads(line) for line in out.read_text().splitlines()]
    assert record["argv"] == ["--title", "lab"]
    assert "1 task on an expired lease" in record["stdin"]


@pytest.mark.safety
def test_hostile_text_cannot_reach_a_shell_or_the_argv(tmp_path: Path) -> None:
    script, out = _recorder(tmp_path)
    marker = tmp_path / "pwned"
    cfg = alert.load(_config(tmp_path, [sys.executable, str(script)]))
    hostile = f"x'; touch {marker}; echo '\x1b[2J\nsecond line $(touch {marker}) `id`"
    alert.send(cfg, kind="unhealthy", message=hostile)
    assert not marker.exists()
    (record,) = [json.loads(line) for line in out.read_text().splitlines()]
    assert record["argv"] == []
    assert "\x1b" not in record["stdin"]


@pytest.mark.safety
@pytest.mark.parametrize("bad", [
    ["notify"],                       # relative: found through PATH
    "notify --now",                   # a string would need a shell
    [],
    [""],
])
def test_only_an_absolute_argv_list_is_accepted(tmp_path: Path, bad: object) -> None:
    path = tmp_path / "alert.json"
    path.write_text(json.dumps({"command": bad}))
    path.chmod(0o600)
    with pytest.raises(alert.AlertConfigError):
        alert.load(path)


@pytest.mark.safety
def test_a_config_others_can_write_is_refused(tmp_path: Path) -> None:
    path = _config(tmp_path, [sys.executable])
    path.chmod(0o666)
    with pytest.raises(alert.AlertConfigError):
        alert.load(path)
    path.chmod(0o620)
    with pytest.raises(alert.AlertConfigError):
        alert.load(path)


def test_a_missing_or_malformed_config_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(alert.AlertConfigError):
        alert.load(tmp_path / "nope.json")
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    bad.chmod(0o600)
    with pytest.raises(alert.AlertConfigError):
        alert.load(bad)


def test_a_hook_that_hangs_is_killed(tmp_path: Path) -> None:
    script = tmp_path / "hang.py"
    script.write_text("import time\ntime.sleep(60)\n")
    cfg = alert.load(_config(tmp_path, [sys.executable, str(script)], timeout_seconds=1))
    assert alert.send(cfg, kind="x", message="m") is False


def test_a_hook_that_fails_is_reported_not_raised(tmp_path: Path) -> None:
    cfg = alert.load(_config(tmp_path, [sys.executable, "-c", "import sys; sys.exit(3)"]))
    assert alert.send(cfg, kind="x", message="m") is False


def test_repeat_alerts_are_suppressed_within_the_window(tmp_path: Path) -> None:
    script, out = _recorder(tmp_path)
    cfg = alert.load(_config(tmp_path, [sys.executable, str(script)], min_interval_seconds=3600))
    state = tmp_path / "alert.state"
    assert alert.send(cfg, kind="unhealthy", message="a", state_file=state)
    assert alert.send(cfg, kind="unhealthy", message="a", state_file=state) is False
    assert alert.send(cfg, kind="selftest", message="b", state_file=state), "another kind"
    assert len(out.read_text().splitlines()) == 2


def test_the_alert_state_file_is_a_private_regular_file(tmp_path: Path) -> None:
    script, _out = _recorder(tmp_path)
    cfg = alert.load(_config(tmp_path, [sys.executable, str(script)]))
    state = tmp_path / "lab.db.alert"
    assert alert.send(cfg, kind="backup", message="m", state_file=state)
    info = state.lstat()
    assert stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
    assert set(json.loads(state.read_text())) == {"backup"}


@pytest.mark.safety
@pytest.mark.parametrize("shape", ["symlink", "dangling symlink", "hard link"])
def test_the_alert_state_file_is_never_written_through_a_link(tmp_path: Path,
                                                               shape: str) -> None:
    script, out = _recorder(tmp_path)
    cfg = alert.load(_config(tmp_path, [sys.executable, str(script)]))
    other = tmp_path / "other.txt"
    state = tmp_path / "lab.db.alert"
    if shape == "dangling symlink":
        state.symlink_to(other)
    else:
        other.write_text("unchanged")
        if shape == "symlink":
            state.symlink_to(other)
        else:
            os.link(other, state)
    assert alert.send(cfg, kind="backup", message="m", state_file=state)
    assert out.exists(), "the alert itself still runs"
    if shape == "dangling symlink":
        assert state.is_symlink() and not other.exists()
    else:
        assert other.read_text() == "unchanged"


@pytest.mark.safety
@pytest.mark.skipif(os.geteuid() == 0, reason="root opens a mode 000 file")
def test_a_hard_linked_state_file_that_cannot_be_opened_is_refused_not_raised(
        tmp_path: Path) -> None:
    script, out = _recorder(tmp_path)
    cfg = alert.load(_config(tmp_path, [sys.executable, str(script)]))
    other = tmp_path / "other.txt"
    other.write_text("unchanged")
    state = tmp_path / "lab.db.alert"
    os.link(other, state)
    os.chmod(other, 0)
    warnings: list[str] = []
    try:
        assert alert.send(cfg, kind="backup", message="m", state_file=state,
                          warn=warnings.append)
    finally:
        os.chmod(other, 0o600)
    assert out.exists(), "the alert itself still runs"
    assert any("more than one link" in w for w in warnings), warnings
    assert other.read_text() == "unchanged"


@pytest.mark.safety
@pytest.mark.parametrize("shape", ["directory", "fifo"])
def test_an_alert_state_file_that_is_not_a_regular_file_is_reported_and_left_alone(
        tmp_path: Path, shape: str) -> None:
    script, out = _recorder(tmp_path)
    cfg = alert.load(_config(tmp_path, [sys.executable, str(script)]))
    state = tmp_path / "lab.db.alert"
    if shape == "directory":
        state.mkdir()
    else:
        os.mkfifo(state)
    notes: list[str] = []
    assert alert.send(cfg, kind="backup", message="m", state_file=state, warn=notes.append)
    assert out.exists(), "the alert itself still runs"
    assert len(notes) == 1 and f"{state} is not a regular file" in notes[0]
    assert "not rate limited" in notes[0]
    kind = stat.S_IFMT(state.lstat().st_mode)
    assert kind == (stat.S_IFDIR if shape == "directory" else stat.S_IFIFO)


@pytest.mark.safety
def test_the_alert_lock_file_is_never_opened_through_a_symbolic_link(tmp_path: Path) -> None:
    script, out = _recorder(tmp_path)
    cfg = alert.load(_config(tmp_path, [sys.executable, str(script)]))
    state = tmp_path / "lab.db.alert"
    other = tmp_path / "other.lock"
    Path(f"{state}.lock").symlink_to(other)
    notes: list[str] = []
    assert alert.send(cfg, kind="backup", message="m", state_file=state, warn=notes.append)
    assert out.exists() and not other.exists()
    assert len(notes) == 1 and f"{state}.lock is a symbolic link" in notes[0]
    assert json.loads(state.read_text()).keys() == {"backup"}, "the state file is still used"


@pytest.mark.safety
def test_the_alert_state_file_is_checked_again_when_the_time_is_recorded(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = alert.load(_config(tmp_path, [sys.executable]))
    state = tmp_path / "lab.db.alert"
    other = tmp_path / "other.txt"
    other.write_text("unchanged")

    def hook_that_links_the_state_file(*_a: object) -> bool:
        state.symlink_to(other)
        return True

    monkeypatch.setattr(alert, "_run_hook", hook_that_links_the_state_file)
    assert alert.send(cfg, kind="backup", message="m", state_file=state)
    assert state.is_symlink() and other.read_text() == "unchanged"


# ------------------------------------------------------------------- CLI


def test_cli_selftest_exit_codes_and_alert(db: Path, tmp_path: Path,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    script, out = _recorder(tmp_path)
    cfg = _config(tmp_path, [sys.executable, str(script)])
    assert main(["--db", str(db), "selftest", "--no-safety-tests",
                 "--alert-config", str(cfg)]) == 0
    assert not out.exists(), "no alert when everything passes"
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute("DROP TRIGGER events_no_update")
    conn.execute("UPDATE events SET detail = '{}' WHERE id = 1")
    conn.commit()
    conn.close()
    capsys.readouterr()
    assert main(["--db", str(db), "selftest", "--no-safety-tests",
                 "--alert-config", str(cfg)]) == 1
    assert "audit_chain" in capsys.readouterr().out
    assert out.exists() and "audit_chain" in out.read_text()


def test_report_ok_sends_one_short_ok_alert_inside_the_rate_limit(
        db: Path, tmp_path: Path) -> None:
    """#80: with --report-ok a passing run says so, once per alert window."""
    script, out = _recorder(tmp_path)
    cfg = _config(tmp_path, [sys.executable, str(script)], min_interval_seconds=3600)
    args = ["--db", str(db), "selftest", "--no-safety-tests", "--alert-config", str(cfg),
            "--report-ok"]
    assert main(args) == 0
    (record,) = [json.loads(line) for line in out.read_text().splitlines()]
    count = len(selftest.run(db, run_safety_tests=False).checks)
    assert record["stdin"] == f"selftest_ok: selftest ok: {count} checks\n"
    assert main(args) == 0
    assert len(out.read_text().splitlines()) == 1, "the config's rate limit still applies"


def test_an_ok_report_never_uses_up_the_window_a_failure_needs(db: Path, tmp_path: Path) -> None:
    script, out = _recorder(tmp_path)
    cfg = _config(tmp_path, [sys.executable, str(script)], min_interval_seconds=3600)
    args = ["--db", str(db), "selftest", "--no-safety-tests", "--alert-config", str(cfg),
            "--report-ok"]
    assert main(args) == 0
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute("DROP TRIGGER events_no_update")
    conn.execute("UPDATE events SET detail = '{}' WHERE id = 1")
    conn.commit()
    conn.close()
    assert main(args) == 1
    records = [json.loads(line)["stdin"] for line in out.read_text().splitlines()]
    assert len(records) == 2 and records[1].startswith("selftest: ")
    assert "audit_chain" in records[1]


def test_report_ok_without_an_alert_config_is_a_usage_error(
        db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--db", str(db), "selftest", "--no-safety-tests", "--report-ok"]) == 1
    assert "--alert-config" in capsys.readouterr().err


def test_cli_status_alerts_only_when_unhealthy(tmp_path: Path) -> None:
    script, out = _recorder(tmp_path)
    cfg = _config(tmp_path, [sys.executable, str(script)])
    healthy = tmp_path / "ok.db"
    TaskQueue(healthy, owner="t").close()
    assert main(["--db", str(healthy), "status", "--alert-config", str(cfg)]) == 0
    assert not out.exists()
    stuck = tmp_path / "stuck.db"
    with TaskQueue(stuck, owner="t") as q:
        q.add_task("t")
        leased = q.lease(ttl_seconds=1)
        assert leased is not None and leased.lease is not None
        q.start(leased.lease)
        q._conn.execute("UPDATE leases SET expires_at = '2000-01-01 00:00:00.000'")
    assert main(["--db", str(stuck), "status", "--alert-config", str(cfg)]) == 2
    assert out.exists() and "expired lease" in out.read_text()


def test_the_launchd_definitions_exist_and_match_the_generator() -> None:
    from lab import service
    root = Path(__file__).resolve().parent.parent / "ops" / "launchd"
    py, wd, db = "/opt/homelab/.venv/bin/python", "/opt/homelab", "/var/homelab/lab.db"
    cfg = "/etc/homelab/alert.json"
    assert (root / "com.homelab.selftest.plist").read_bytes() == service.selftest_plist(
        user="lab", python=py, workdir=wd, db=db, alert_config=cfg)
    import plistlib
    assert "--report-ok" in plistlib.loads(service.selftest_plist(
        user="lab", python=py, workdir=wd, db=db, alert_config=cfg))["ProgramArguments"]
    assert (root / "com.homelab.statuscheck.plist").read_bytes() == service.statuscheck_plist(
        user="lab", python=py, workdir=wd, db=db, alert_config=cfg)
    assert os.access(root, os.R_OK) and stat.S_ISDIR(root.stat().st_mode)


# ---------------------------------------------------- review findings on #144


def test_a_damaged_database_fails_the_checks_instead_of_crashing(tmp_path: Path) -> None:
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"this is not a sqlite database" * 100)
    report = selftest.run(bad, run_safety_tests=False)
    assert not report.ok
    assert {c.name for c in report.failures()} >= {"audit_chain"}


def test_a_pytest_that_cannot_launch_fails_the_check(db: Path, tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess as sp
    tests = tmp_path / "t"
    tests.mkdir()

    def boom(*a: object, **k: object) -> None:
        raise OSError("exec format error")

    monkeypatch.setattr(sp, "run", boom)
    report = selftest.run(db, tests_dir=tests, run_safety_tests=True)
    check = next(c for c in report.checks if c.name == "safety_tests")
    assert not check.ok and "OSError" in check.detail


def test_a_missing_pytest_is_named_in_the_detail(db: Path, tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """#270: with pytest missing, stdout is empty, and the failure used to say nothing."""
    import subprocess as sp
    tests = tmp_path / "t"
    tests.mkdir()

    def no_pytest(args: list[str], **k: object) -> sp.CompletedProcess[str]:
        return sp.CompletedProcess(args, 1, stdout="",
                                   stderr="/opt/homelab/.venv/bin/python: No module named pytest\n")

    monkeypatch.setattr(sp, "run", no_pytest)
    report = selftest.run(db, tests_dir=tests, run_safety_tests=True)
    check = next(c for c in report.checks if c.name == "safety_tests")
    assert not check.ok and "No module named pytest" in check.detail


def test_stderr_wins_over_stdout_noise_when_pytest_gives_no_summary(
        db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess as sp
    tests = tmp_path / "t"
    tests.mkdir()

    def noisy(args: list[str], **k: object) -> sp.CompletedProcess[str]:
        return sp.CompletedProcess(args, 4, stdout="some plugin banner\n",
                                   stderr="ERROR: file or directory not found: t\n")

    monkeypatch.setattr(sp, "run", noisy)
    report = selftest.run(db, tests_dir=tests, run_safety_tests=True)
    check = next(c for c in report.checks if c.name == "safety_tests")
    assert not check.ok and "file or directory not found" in check.detail


def test_a_relative_tests_dir_is_resolved_once(db: Path, tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    tests = tmp_path / "suite"
    tests.mkdir()
    (tests / "test_ok.py").write_text(
        "import pytest\n@pytest.mark.safety\ndef test_ok():\n    assert True\n")
    (tests / "pytest.ini").write_text("[pytest]\nmarkers = safety\n")
    monkeypatch.chdir(tmp_path)
    report = selftest.run(db, tests_dir=Path("suite"), run_safety_tests=True)
    check = next(c for c in report.checks if c.name == "safety_tests")
    assert check.ok and "1 passed" in check.detail


@pytest.mark.safety
def test_a_nul_byte_in_the_command_is_a_config_error(tmp_path: Path) -> None:
    path = tmp_path / "alert.json"
    path.write_text(json.dumps({"command": ["/bin/echo\u0000x"]}))
    path.chmod(0o600)
    with pytest.raises(alert.AlertConfigError):
        alert.load(path)


@pytest.mark.safety
def test_a_symlinked_config_is_refused_even_if_the_target_is_safe(tmp_path: Path) -> None:
    real = _config(tmp_path, [sys.executable])
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(alert.AlertConfigError):
        alert.load(link)


@pytest.mark.safety
def test_the_checked_file_is_the_file_that_is_read(tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """The mode and owner are read from the open descriptor, so swapping the
    path between a check and a read cannot change which file was checked."""
    path = _config(tmp_path, [sys.executable])
    calls: list[str] = []
    real_stat = Path.stat

    def spy(self: Path, *a: object, **k: object) -> os.stat_result:
        calls.append(str(self))
        return real_stat(self, *a, **k)

    monkeypatch.setattr(Path, "stat", spy)
    alert.load(path)
    assert str(path) not in calls, "the path must not be stat'ed separately from the open"


def test_two_processes_racing_send_one_alert(tmp_path: Path) -> None:
    import threading
    script = tmp_path / "slow.py"
    out = tmp_path / "runs"
    script.write_text(
        "import time\n"
        f"open({str(out)!r}, 'a').write('x')\n"
        "time.sleep(0.3)\n")
    cfg = alert.load(_config(tmp_path, [sys.executable, str(script)], min_interval_seconds=3600))
    state = tmp_path / "state"
    results: list[bool] = []

    def go() -> None:
        results.append(alert.send(cfg, kind="unhealthy", message="m", state_file=state))

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1 and len(out.read_text()) == 1
