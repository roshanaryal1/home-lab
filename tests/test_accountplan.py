"""The lab-account setup plan (H5c).

Creating the non-admin account, the directories and the LaunchDaemons is the
step that turns the code's boundaries into real ones, and it can only happen
on the machine. This module writes the plan down as data, prints it for a
person to read, and applies it only if explicitly told to, as root, on macOS.
Generating the plan never runs anything.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import pytest

from lab import accountplan
from lab.cli import main

OPS = Path(__file__).resolve().parent.parent / "ops" / "launchd"


def test_the_plan_creates_a_non_admin_account_and_never_grants_admin() -> None:
    plan = accountplan.build()
    text = "\n".join(step.command for step in plan)
    assert "sysadminctl" in text and "-addUser lab" in text
    assert "-admin" not in text and "dseditgroup" not in text
    assert "/usr/bin/false" in text or "/usr/sbin/nologin" in text or "-shell" in text


@pytest.mark.safety
def test_building_the_plan_runs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> None:
        raise AssertionError("the plan must not execute anything")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    accountplan.build()
    accountplan.render(accountplan.build())


def test_every_step_is_an_absolute_argv_and_round_trips_through_shlex() -> None:
    for step in accountplan.build():
        assert step.argv[0].startswith("/"), step.command
        assert shlex.split(step.command) == list(step.argv)


@pytest.mark.safety
@pytest.mark.parametrize("bad", ["", "Root", "a b", "lab;id", "-lab", "lab\n", "x" * 40, "../lab"])
def test_a_hostile_account_name_is_refused(bad: str) -> None:
    with pytest.raises(ValueError):
        accountplan.build(user=bad)


def test_the_private_directories_are_owned_by_lab_and_closed_to_others() -> None:
    plan = accountplan.build()
    joined = "\n".join(step.command for step in plan)
    assert "/var/homelab" in joined and "/var/log/homelab" in joined
    for step in plan:
        if step.argv[0].endswith("/chmod") and "/var/homelab" in step.command:
            assert step.argv[1] in {"700", "750"}


def test_the_operator_public_key_is_readable_but_not_writable_by_lab() -> None:
    plan = accountplan.build()
    key_steps = [s for s in plan if "operator.pub" in s.command]
    assert key_steps
    install = next(s for s in key_steps if s.argv[0].endswith("/install"))
    assert {"root", "wheel", "644"} <= set(install.argv)
    assert not any("operator.key" in s.command for s in plan if s.mutates), \
        "no step that changes anything touches the private key"
    reads = [s for s in plan if "operator.key" in s.command]
    assert reads and all(not s.mutates and s.expect == "Permission denied" for s in reads)


def test_every_launchd_file_in_ops_is_installed_root_owned() -> None:
    plan = accountplan.build()
    installed = {Path(s.argv[-1]).name for s in plan if "/Library/LaunchDaemons" in s.command
                 and s.argv[0].endswith("/install")}
    assert installed == {p.name for p in OPS.glob("*.plist")}
    for step in plan:
        if "/Library/LaunchDaemons" in step.command and step.argv[0].endswith("/install"):
            assert "root" in step.argv and "wheel" in step.argv and "644" in step.argv


def test_verification_steps_are_marked_read_only_and_state_what_to_expect() -> None:
    checks = [s for s in accountplan.build() if not s.mutates]
    assert len(checks) >= 3
    assert all(s.expect for s in checks)
    assert any("sudo" in s.command for s in checks), "must confirm lab cannot sudo"


@pytest.mark.safety
def test_apply_refuses_unless_root_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("nothing may run")))
    monkeypatch.setattr(accountplan.os, "geteuid", lambda: 1000)
    with pytest.raises(accountplan.ApplyRefused, match="root"):
        accountplan.apply(accountplan.build(), platform="darwin")
    monkeypatch.setattr(accountplan.os, "geteuid", lambda: 0)
    with pytest.raises(accountplan.ApplyRefused, match="macOS"):
        accountplan.apply(accountplan.build(), platform="linux")


def test_apply_runs_mutating_steps_in_order_and_stops_at_the_first_failure(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(accountplan.os, "geteuid", lambda: 0)
    ran: list[list[str]] = []

    def fake(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        ran.append(argv)
        return subprocess.CompletedProcess(argv, 1 if len(ran) == 3 else 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake)
    result = accountplan.apply(accountplan.build(), platform="darwin")
    assert not result.ok and len(ran) == 3
    assert all(step.mutates for step in result.steps_run)


def test_cli_prints_the_plan_and_does_not_apply(capsys: pytest.CaptureFixture[str],
                                                tmp_path: Path) -> None:
    assert main(["--db", str(tmp_path / "x.db"), "setup-plan"]) == 0
    out = capsys.readouterr().out
    assert "sysadminctl" in out and "DRY RUN" in out


def test_cli_apply_is_refused_when_not_root(capsys: pytest.CaptureFixture[str],
                                            tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(accountplan.os, "geteuid", lambda: 1000)
    assert main(["--db", str(tmp_path / "x.db"), "setup-plan", "--apply"]) == 1
    assert "root" in capsys.readouterr().err
