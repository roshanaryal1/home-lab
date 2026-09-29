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
    assert (root / "com.homelab.statuscheck.plist").read_bytes() == service.statuscheck_plist(
        user="lab", python=py, workdir=wd, db=db, alert_config=cfg)
    assert os.access(root, os.R_OK) and stat.S_ISDIR(root.stat().st_mode)
