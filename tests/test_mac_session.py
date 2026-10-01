"""The Mac mini session script (#241): every check in one run, one report.

The script runs against stub commands that record each call to a file, so every
case is deterministic and the same on a Mac and on the Linux CI runner.
"""

from __future__ import annotations

import re
import stat
import subprocess
import sys
from pathlib import Path

from lab import operator as op

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "ops" / "mac-session.sh"

STEPS = [
    ("context", "#241"), ("home", "#225"), ("caffeinate", "#235"), ("signature", "#70"),
    ("backup", "#67"), ("alert", "#79, #80"), ("selftest", "#80"), ("concurrency", "#211"),
    ("drills", "#78"), ("network", "#79"), ("power", "#77, #91"),
]

STUBS = {
    "sudo": r'''
case "$*" in
  -v) exit 0 ;;
  *"/bin/ls "*)
    if [ "${STUB_HOME_READABLE:-0}" = 1 ] && [ ! -e "$STUB_DIR/home-locked" ]; then
      echo Desktop; exit 0
    fi
    echo "ls: Permission denied"; exit 1 ;;
  *caffeinate*) exit 0 ;;
  *" -c "*) exit 0 ;;
  *" - "*) cat >/dev/null; echo "REFUSED unsigned"; echo "PASS 3 of 3 attempts refused" ;;
  *"git -C"*) echo 0123456789abcdef0123456789abcdef01234567 ;;
  *" status"*) echo "health IDLE" ;;
  *"/bin/cat "*) echo '{"command": ["/usr/bin/logger"]}' ;;
  *" backup "*) echo "wrote /Volumes/labbackup/home-lab-backups/b.manifest.json" ;;
  *mktemp*) echo /tmp/homelab-restore.stub ;;
  *restore-check*) echo "ok: 0 audit events, 0 artifact blobs checked, restored to /x" ;;
  *"/usr/bin/find "*) echo /var/log/homelab/selftest.log ;;
  *selftest.log*) echo "ok   audit_chain     0 events" ;;
  *watchdog*) echo "watchdog: healthy pid 1 (heartbeat 3s old)" ;;
esac
exit 0''',
    "pmset": 'echo \'   pid 123(caffeinate): [0x1] 00:00:02 PreventUserIdleSystemSleep '
             'named: "caffeinate command-line tool"\'',
    "caffeinate": "exit 0",
    "curl": r'''
case "$*" in
  *chat/completions*) printf 200 ;;
  */models*) echo '{"data": [{"id": "/models/stub-model"}]}' ;;
esac''',
    "sysctl": 'echo "vm.swapusage: total = 2048.00M  used = 1247.75M  free = 800.25M  (encrypted)"',
    "top": 'echo "PhysMem: 30G used (2943M wired, 1100M compressor), 1562M unused."',
    "vm_stat": 'echo "Pages free: 1000."',
    "memory_pressure": "echo 'System-wide memory free percentage: 60%'",
    "sw_vers": 'echo "ProductVersion: 26.0"',
    "pgrep": 'n=$(cat "$STUB_DIR/pid" 2>/dev/null || echo 100); n=$((n + 1)); '
             'echo "$n" >"$STUB_DIR/pid"; echo "$n"',
    "ps": "exit 1",
    "sleep": "exit 0",
    "chmod": 'touch "$STUB_DIR/home-locked"',
}


def make_stubs(directory: Path, overrides: dict[str, str] | None = None) -> Path:
    bin_dir = directory / "bin"
    bin_dir.mkdir()
    for name, body in {**STUBS, **(overrides or {})}.items():
        path = bin_dir / name
        path.write_text(f'#!/bin/sh\necho "{name} $*" >>"$CALLS"\n{body}\n')
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return bin_dir


def run(tmp_path: Path, *args: str, answers: str | None = None, home_readable: bool = False,
        stubs: dict[str, str] | None = None,
        ) -> tuple[subprocess.CompletedProcess[str], str, list[str]]:
    bin_dir = make_stubs(tmp_path, stubs)
    home = tmp_path / "home"
    home.mkdir()
    report = tmp_path / "report.md"
    calls = tmp_path / "calls.txt"
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(home), "TMPDIR": str(tmp_path),
        "CALLS": str(calls), "STUB_DIR": str(tmp_path),
        "STUB_HOME_READABLE": "1" if home_readable else "0",
    }
    result = subprocess.run(
        ["bash", str(SCRIPT), "--report", str(report), *args], env=env, text=True,
        capture_output=True, timeout=120, cwd=tmp_path,
        input=answers if answers is not None else None,
        stdin=subprocess.DEVNULL if answers is None else None)
    text = report.read_text() if report.exists() else ""
    recorded = calls.read_text().splitlines() if calls.exists() else []
    return result, text, recorded


def result_of(report: str, number: int) -> str:
    section = report.split(f"\n## {number}. ", 1)[1].split("\n## ", 1)[0]
    found = re.search(r"\*\*Result: (\w+)\.\*\*", section)
    assert found, section
    return found.group(1)


def test_a_dry_run_lists_every_step_and_runs_nothing(tmp_path: Path) -> None:
    result, report, calls = run(tmp_path, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert calls == [], f"a dry run ran commands: {calls}"
    for number, (name, issues) in enumerate(STEPS, 1):
        assert re.search(rf"^## {number}\. .+ \({re.escape(issues)}\)$", report, re.M), name
        assert f"Step `{name}`." in report
    assert "mode: dry run" in report
    assert report.count("(dry run: not run)") > 20
    # The commands behind each prompt are shown, so the operator can read them first.
    for command in ('chmod 700 "$HOME"', 'sudo kill -9 "$OLD"', 'sudo kill -STOP "$OLD"',
                    "backup --to", "restore-check", "/usr/bin/caffeinate -i -t 15",
                    "pmset -g assertions", "sysctl vm.swapusage", "watchdog --dry-run"):
        assert command in report, command
    assert "**Result: FAIL" not in report and "**Result: PASS" not in report
    assert "## Summary" in report


def test_a_full_run_with_no_answers_changes_nothing(tmp_path: Path) -> None:
    result, report, calls = run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls[0] == "sudo -v", "sudo -v must come first, once"
    assert calls.count("sudo -v") == 1
    joined = "\n".join(calls)
    for change in ("chmod", "kill", " backup ", "restore-check", " -c "):
        assert change not in joined, f"{change!r} ran without a yes"
    expected = {"context": "PASS", "home": "PASS", "caffeinate": "PASS",
                "signature": "PASS", "backup": "SKIPPED", "alert": "SKIPPED",
                "selftest": "PASS", "concurrency": "PASS", "drills": "SKIPPED",
                "network": "MANUAL", "power": "MANUAL"}
    for number, (name, _) in enumerate(STEPS, 1):
        assert result_of(report, number) == expected[name], name
    assert report.count("answer: none, so no") == 3


def test_every_prompt_defaults_to_no_on_an_empty_answer(tmp_path: Path) -> None:
    result, report, calls = run(tmp_path, "--only", "home", answers="\n",
                                home_readable=True)
    assert result.returncode == 1
    assert not any(c.startswith("chmod") for c in calls)
    assert result_of(report, 1) == "FAIL"
    assert "chmod 700 was not run" in report


def test_a_yes_runs_chmod_and_checks_again(tmp_path: Path) -> None:
    result, report, calls = run(tmp_path, "--only", "home", answers="y\n",
                                home_readable=True)
    assert result.returncode == 0, report
    assert any(c.startswith("chmod 700 ") for c in calls)
    assert sum("/bin/ls" in c for c in calls) == 4
    assert result_of(report, 1) == "PASS"


def test_the_concurrency_step_records_timing_memory_and_swap(tmp_path: Path) -> None:
    result, report, calls = run(tmp_path, "--only", "concurrency")
    assert result.returncode == 0, report
    assert "/models/stub-model" in report
    assert sum("chat/completions" in c for c in calls) == 2
    assert re.search(r"http 200 and 200; wall [0-9.]+ s and [0-9.]+ s; overlapped (yes|no)",
                     report)
    assert "peak PhysMem used during: 30720 MB" in report
    assert "swap used: before 1247.8 MB" in report


def test_the_drills_run_only_after_a_yes_and_are_timed(tmp_path: Path) -> None:
    result, report, calls = run(tmp_path, "--only", "drills", answers="y\n")
    assert result.returncode == 0, report
    assert any(c.startswith("sudo kill -9 ") for c in calls)
    assert any(c.startswith("sudo kill -STOP ") for c in calls)
    assert re.search(r"killed \d+; new supervisor \d+ after \d+ s", report)
    assert re.search(r"frozen \d+; gone after \d+ s; new supervisor \d+ after \d+ s", report)
    assert result_of(report, 1) == "PASS"


def test_a_ctrl_c_during_the_freeze_drill_resumes_the_supervisor(tmp_path: Path) -> None:
    """The supervisor is stopped with SIGSTOP. If the operator interrupts the
    wait, the script must resume it: a lab left frozen is what the drill guards
    against when the watchdog is the thing that failed."""
    result, report, calls = run(
        tmp_path, "--only", "drills", answers="y\n",
        stubs={"ps": "exit 0",
               "sleep": 'grep -q "kill -STOP" "$CALLS" && kill -INT "$PPID"; exit 0'})
    assert result.returncode == 130, report
    stopped = next(c for c in calls if c.startswith("sudo kill -STOP "))
    pid = stopped.split()[-1]
    assert f"sudo kill -CONT {pid}" in calls, calls
    assert "resumed the frozen supervisor" in report


def test_a_backup_runs_after_a_yes_and_cleans_its_restore_folder(tmp_path: Path) -> None:
    result, report, calls = run(tmp_path, "--only", "backup", answers="y\n")
    assert result.returncode == 0, report
    assert any("backup --to /Volumes/labbackup/home-lab-backups" in c for c in calls)
    assert any("restore-check /Volumes/labbackup/home-lab-backups/b.manifest.json" in c
               for c in calls)
    assert any("/bin/rm -rf /tmp/homelab-restore.stub" in c for c in calls)
    assert result_of(report, 1) == "PASS"


def test_an_alert_the_operator_did_not_see_is_a_failure(tmp_path: Path) -> None:
    result, report, _ = run(tmp_path, "--only", "alert", answers="y\nn\n")
    assert result.returncode == 1
    assert result_of(report, 1) == "FAIL"
    again = tmp_path / "again"
    again.mkdir()
    result, report, calls = run(again, "--only", "alert", answers="y\ny\n")
    assert result.returncode == 0 and result_of(report, 1) == "PASS"
    assert any(" -c " in c and "/etc/homelab/alert.json" in c for c in calls)


def test_an_unknown_step_is_refused(tmp_path: Path) -> None:
    result, _, calls = run(tmp_path, "--only", "nope")
    assert result.returncode == 2 and "unknown step" in result.stderr
    assert calls == []


def test_the_default_report_name_is_a_utc_timestamp(tmp_path: Path) -> None:
    bin_dir = make_stubs(tmp_path)
    result = subprocess.run(
        ["bash", str(SCRIPT), "--dry-run"], cwd=tmp_path, text=True, capture_output=True,
        timeout=60, env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path),
                         "CALLS": str(tmp_path / "calls.txt")})
    assert result.returncode == 0, result.stderr
    assert [p.name for p in tmp_path.glob("mac-session-*.md")] != []
    assert re.fullmatch(r"mac-session-\d{8}T\d{6}Z\.md",
                        next(tmp_path.glob("mac-session-*.md")).name)


def test_the_signature_probe_refuses_all_three_forged_approvals(tmp_path: Path) -> None:
    """Run the probe itself, as the script does, against a real public key."""
    probe = SCRIPT.read_text().split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    _, public = op.generate(tmp_path / "keys")
    result = subprocess.run([sys.executable, "-", str(public)], input=probe, text=True,
                            capture_output=True, timeout=120, cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("REFUSED") == 3
    assert "PASS 3 of 3 attempts refused" in result.stdout


def test_the_script_is_executable_keeps_going_and_uses_no_em_dash() -> None:
    assert SCRIPT.stat().st_mode & stat.S_IXUSR
    text = SCRIPT.read_text()
    assert text.endswith("\n") and chr(0x2014) not in text
    assert "set -u" in text
    assert not re.search(r"^\s*set -[a-z]*e", text, re.M), "one failure must not stop the rest"
