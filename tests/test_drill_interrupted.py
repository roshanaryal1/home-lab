"""The interrupted-task drill (#91): two tasks left running on a scratch
database, a failure injected from outside, and a check of what recovery did.

CI cannot restart its machine or pull its power, so here the failure is a
kill -9 or a SIGTERM of the holder: a rehearsal. A restart is simulated by
moving the boot time past the moment the drill was armed.
"""

from __future__ import annotations

import json
import os
import signal
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from lab import drills
from lab.cli import main


def _wait_until(condition, seconds: float = 15.0) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def _stop(pid: int, sig: int = signal.SIGKILL) -> None:
    """Inject the failure and wait until the holder is really gone."""
    os.kill(pid, sig)
    _wait_until(lambda: not drills._holder_running(pid))


@pytest.fixture()
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A scratch folder; any holder a test leaves running is killed afterwards."""
    monkeypatch.delenv("LAB_TARGET", raising=False)
    folder = tmp_path / "drill-interrupted"
    yield folder
    armed = drills._read_json(folder / "armed.json")
    if armed is not None and drills._holder_running(int(armed["holder_pid"])):
        os.kill(int(armed["holder_pid"]), signal.SIGKILL)


def test_arm_then_kill_then_check_passes_as_a_rehearsal(state: Path) -> None:
    armed = drills.arm_interrupted(state, 120)
    pid = int(armed["holder_pid"])
    assert drills._holder_running(pid)
    assert set(armed["tasks"]) == {"idempotent", "non-idempotent"}

    with pytest.raises(drills.DrillError, match="still running"):
        drills.check_interrupted(state)
    _stop(pid)

    result = drills.check_interrupted(state)
    assert result.passed, result.actual
    assert result.name == "interrupted-task"
    assert "the holder was killed and the Mac did not restart (a rehearsal)" in result.injected
    assert "recovery {'interrupted': 2, 'requeued': 1, 'held_for_review': 1}" in result.actual
    assert "idempotent task queued, non-idempotent task interrupted" in result.actual
    assert "integrity ok; audit chain ok=True" in result.actual
    text = drills.record(result, state.parent / "log").read_text()
    assert "counts as demonstrated: no" in text and "rehearsal" in text


def test_a_clean_shutdown_is_told_apart_by_its_sigterm(state: Path) -> None:
    pid = int(drills.arm_interrupted(state, 120)["holder_pid"])
    _stop(pid, signal.SIGTERM)
    result = drills.check_interrupted(state)
    assert result.passed, result.actual
    assert "the holder got SIGTERM and the Mac did not restart" in result.injected


def test_a_restart_since_the_arm_is_recorded_as_a_power_loss_without_sigterm(
        state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid = int(drills.arm_interrupted(state, 120)["holder_pid"])
    _stop(pid)
    monkeypatch.setattr(drills, "on_target", lambda: True)
    monkeypatch.setattr(drills, "boot_time", lambda: time.time())
    result = drills.check_interrupted(state)
    assert result.passed, result.actual
    assert "the Mac lost power or was forced off: no SIGTERM reached the holder" \
        in result.injected
    assert "restarted=yes" in result.actual
    assert "the Mac restarted between arm and check" in result.expected


def test_on_the_target_a_check_without_a_restart_fails(
        state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid = int(drills.arm_interrupted(state, 120)["holder_pid"])
    _stop(pid)
    monkeypatch.setattr(drills, "on_target", lambda: True)
    monkeypatch.setattr(drills, "boot_time", lambda: 0.0)
    result = drills.check_interrupted(state)
    assert not result.passed
    assert "restarted=no" in result.actual
    assert result.follow_up.startswith("no restart or power loss came between arm and check")


def test_a_lost_database_fails_the_check(state: Path) -> None:
    pid = int(drills.arm_interrupted(state, 120)["holder_pid"])
    _stop(pid)
    for name in ("drill.db", "drill.db-wal", "drill.db-shm"):
        (state / name).unlink(missing_ok=True)
    result = drills.check_interrupted(state)
    assert not result.passed
    assert "integrity the database file is missing" in result.actual
    assert result.follow_up == "investigate before trusting recovery after a power loss"


def test_a_second_arm_is_refused_while_one_is_armed(state: Path) -> None:
    drills.arm_interrupted(state, 120)
    with pytest.raises(drills.DrillError, match="already armed"):
        drills.arm_interrupted(state, 120)


def test_a_check_with_nothing_armed_is_refused(state: Path) -> None:
    with pytest.raises(drills.DrillError, match="nothing is armed"):
        drills.check_interrupted(state)


def test_an_unchecked_drill_removes_itself_after_its_hold_time(state: Path) -> None:
    pid = int(drills.arm_interrupted(state, 1.5)["holder_pid"])
    _wait_until(lambda: not state.exists())
    _wait_until(lambda: not drills._holder_running(pid))


def test_removing_the_scratch_leaves_anything_else_alone(tmp_path: Path) -> None:
    folder = tmp_path / "scratch"
    folder.mkdir()
    (folder / "armed.json").write_text("{}")
    (folder / "notes.txt").write_text("the operator's own file")
    drills.remove_state(folder)
    assert not (folder / "armed.json").exists()
    assert (folder / "notes.txt").read_text() == "the operator's own file"


def test_only_the_holder_counts_as_the_holder() -> None:
    assert not drills._holder_running(os.getpid())
    assert not drills._holder_running(2**22 + 12345)


def test_the_boot_time_is_known_and_in_the_past() -> None:
    booted = drills.boot_time()
    assert booted is not None and 0 < booted <= time.time()


# ------------------------------------------------------------------ CLI


def test_cli_arm_then_check_writes_a_record_and_clears_the_scratch(
        state: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = ["--db", str(state.parent / "unused.db")]
    assert main([*db, "drill", "interrupted", "--phase", "arm", "--state", str(state),
                 "--hold-minutes", "2"]) == 0
    out = capsys.readouterr().out
    assert "Then run this drill again with --phase check --state" in out
    pid = int(json.loads((state / "armed.json").read_text())["holder_pid"])
    assert f"kill -9 {pid}" in out
    _stop(pid)
    log = state.parent / "log"
    assert main([*db, "drill", "interrupted", "--phase", "check", "--state", str(state),
                 "--log", str(log)]) == 0
    (record,) = log.glob("*-interrupted-task.md")
    assert "result: PASS" in record.read_text()
    assert not state.exists(), "the scratch database is removed once the record is written"
    assert not (state.parent / "unused.db").exists()


@pytest.mark.parametrize("args", [
    ["drill", "interrupted"],
    ["drill", "crash", "--phase", "arm"],
    ["drill", "crash", "--from-backup", "somewhere"],
    ["drill", "interrupted", "--phase", "arm", "--hold-minutes", "0"],
])
def test_cli_refuses_options_that_do_not_fit_the_drill(
        tmp_path: Path, args: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--db", str(tmp_path / "unused.db"), *args, "--state",
                 str(tmp_path / "s"), "--log", str(tmp_path / "log")]) == 2
    assert "drill:" in capsys.readouterr().err
    assert not (tmp_path / "s").exists() and not (tmp_path / "log").exists()
