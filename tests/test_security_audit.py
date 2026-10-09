"""``lab security-audit`` (#362): one read-only check per concern, each with a pass and a fail.

The layouts are built in tmp_path, and the expected root uid is the current uid, so the
files a test creates count as correctly owned. An owner failure passes a different
expected uid. Only root can give a file another owner, so the two rules that need a
folder owned by someone else patch os.lstat for that one path, and nothing is written
outside tmp_path.
"""

from __future__ import annotations

import functools
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from lab import security_audit as sa
from lab.cli import main

ME = os.getuid()
EUID = os.geteuid()


def _dir(path: Path, mode: int = 0o755) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, mode)
    return path


def _file(path: Path, mode: int = 0o644) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    os.chmod(path, mode)
    return path


@dataclass(frozen=True)
class Layout:
    base: Path
    deploy: Path
    launch: Path
    config: Path
    data: Path
    home: Path

    @property
    def db(self) -> Path:
        return self.data / "lab.db"

    @property
    def key(self) -> Path:
        return self.home / ".lab-operator" / "operator.key"

    def run_kwargs(self) -> dict[str, Any]:
        return {"home": self.home, "deploy_root": self.deploy, "launch_daemons": self.launch,
                "config_dir": self.config, "data_dir": self.data, "root_uid": ME,
                "euid": EUID}


@pytest.fixture()
def layout(tmp_path: Path) -> Layout:
    """A tidy install: a deploy tree, two lab plists and one plist that is not ours, a config
    folder, a private data folder and an operator key. Every file is closed to others."""
    base = tmp_path / "install"
    lay = Layout(base=base, deploy=base / "opt" / "homelab", launch=base / "LaunchDaemons",
                 config=base / "etc" / "homelab", data=base / "var" / "homelab",
                 home=base / "home")
    _dir(lay.deploy)
    _dir(lay.deploy / "lab")
    _file(lay.deploy / "lab" / "__init__.py")
    _dir(lay.launch)
    _file(lay.launch / "com.homelab.supervisor.plist")
    _file(lay.launch / "com.homelab.backup.plist")
    _file(lay.launch / "com.apple.unrelated.plist", 0o666)   # not ours, so not judged
    _dir(lay.config)
    _file(lay.config / "operator.pub")
    _file(lay.config / "mcp.json")
    _dir(lay.data, 0o700)
    _file(lay.db, 0o600)
    _dir(lay.home / ".lab-operator", 0o700)
    _file(lay.key, 0o600)
    return lay


def _fake_owner(monkeypatch: pytest.MonkeyPatch, path: Path, uid: int) -> None:
    """Make os.lstat report ``uid`` as the owner of ``path``. Nothing else changes."""
    real = os.lstat
    target = os.fspath(path)

    def lstat(p: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        st = real(p, *args, **kwargs)
        if os.fspath(p) != target:
            return st
        return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, uid, st.st_gid,
                               st.st_size, int(st.st_atime), int(st.st_mtime),
                               int(st.st_ctime)))

    monkeypatch.setattr(os, "lstat", lstat)


def _deny(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    """Make os.lstat raise PermissionError for ``path``, as for a folder this account cannot
    search. A root test user could otherwise still stat it."""
    real: Callable[..., os.stat_result] = os.lstat
    target = os.fspath(path)

    def lstat(p: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if os.fspath(p) == target:
            raise PermissionError(13, "Permission denied", target)
        return real(p, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", lstat)


def _snapshot(base: Path) -> list[tuple[str, int, int, int, int]]:
    """Mode, owner, mtime and size of every folder and entry under ``base``, symlinks included."""
    rows: list[tuple[str, int, int, int, int]] = []
    for current, dirnames, filenames in os.walk(base):
        paths = [current, *(os.path.join(current, name) for name in (*dirnames, *filenames))]
        for path in paths:
            st = os.lstat(path)
            rows.append((path, st.st_mode, st.st_uid, st.st_mtime_ns, st.st_size))
    return sorted(rows)


# ------------------------------------------------------------ deploy


def test_deploy_passes_when_every_entry_is_root_owned_and_closed_to_others(layout: Layout) -> None:
    assert sa.check_deploy(layout.deploy, ME) == sa.Finding("deploy", "ok", [])


def test_deploy_fails_on_every_entry_when_the_expected_owner_is_another_uid(
        layout: Layout) -> None:
    finding = sa.check_deploy(layout.deploy, ME + 1)
    assert finding.status == "FAIL"
    assert str(layout.deploy) in finding.paths
    assert str(layout.deploy / "lab" / "__init__.py") in finding.paths


def test_deploy_names_a_group_writable_file(layout: Layout) -> None:
    target = layout.deploy / "lab" / "__init__.py"
    os.chmod(target, 0o664)
    assert sa.check_deploy(layout.deploy, ME) == sa.Finding("deploy", "FAIL", [str(target)])


def test_deploy_names_an_other_writable_folder(layout: Layout) -> None:
    target = layout.deploy / "lab"
    os.chmod(target, 0o757)
    assert sa.check_deploy(layout.deploy, ME) == sa.Finding("deploy", "FAIL", [str(target)])


def test_deploy_fails_on_a_missing_root(tmp_path: Path) -> None:
    missing = tmp_path / "opt" / "homelab"
    assert sa.check_deploy(missing, ME) == sa.Finding("deploy", "FAIL", [str(missing)])


def test_deploy_does_not_follow_a_symlink_out_of_the_tree(layout: Layout, tmp_path: Path) -> None:
    outside = _dir(tmp_path / "outside", 0o777)
    shared = _file(outside / "shared.txt", 0o666)            # group and other writable
    _file(outside / "loose.txt", 0o666)
    os.symlink(shared, layout.deploy / "shared-link")
    os.symlink(outside, layout.deploy / "folder-link")
    assert sa.check_deploy(layout.deploy, ME) == sa.Finding("deploy", "ok", [])


def test_deploy_fails_when_the_root_itself_is_a_symlink(layout: Layout, tmp_path: Path) -> None:
    link = tmp_path / "link-to-deploy"
    os.symlink(layout.deploy, link)
    assert sa.check_deploy(link, ME) == sa.Finding("deploy", "FAIL", [str(link)])


# ------------------------------------------------------------ services


def test_services_passes_when_every_lab_plist_is_root_owned_and_closed(layout: Layout) -> None:
    assert sa.check_services(layout.launch, ME) == sa.Finding("services", "ok", [])


def test_services_fails_when_the_expected_owner_is_another_uid(layout: Layout) -> None:
    finding = sa.check_services(layout.launch, ME + 1)
    assert finding == sa.Finding("services", "FAIL", sorted([
        str(layout.launch / "com.homelab.backup.plist"),
        str(layout.launch / "com.homelab.supervisor.plist")]))


def test_services_names_a_group_writable_plist(layout: Layout) -> None:
    target = layout.launch / "com.homelab.backup.plist"
    os.chmod(target, 0o664)
    assert sa.check_services(layout.launch, ME) == sa.Finding("services", "FAIL", [str(target)])


def test_services_names_an_other_writable_plist(layout: Layout) -> None:
    target = layout.launch / "com.homelab.supervisor.plist"
    os.chmod(target, 0o646)
    assert sa.check_services(layout.launch, ME) == sa.Finding("services", "FAIL", [str(target)])


def test_services_fails_with_no_path_when_no_lab_plist_is_installed(tmp_path: Path) -> None:
    launch = _dir(tmp_path / "LaunchDaemons")
    _file(launch / "com.apple.other.plist")
    assert sa.check_services(launch, ME) == sa.Finding("services", "FAIL", [])


def test_services_fails_on_a_missing_folder(tmp_path: Path) -> None:
    missing = tmp_path / "LaunchDaemons"
    assert sa.check_services(missing, ME) == sa.Finding("services", "FAIL", [str(missing)])


# ------------------------------------------------------------ config


def test_config_passes_when_the_folder_and_its_entries_are_root_owned(layout: Layout) -> None:
    assert sa.check_config(layout.config, ME) == sa.Finding("config", "ok", [])


def test_config_fails_on_the_folder_and_entries_when_the_owner_is_another_uid(
        layout: Layout) -> None:
    finding = sa.check_config(layout.config, ME + 1)
    assert finding.status == "FAIL"
    assert str(layout.config) in finding.paths
    assert str(layout.config / "operator.pub") in finding.paths


def test_config_names_a_group_writable_entry(layout: Layout) -> None:
    target = layout.config / "operator.pub"
    os.chmod(target, 0o664)
    assert sa.check_config(layout.config, ME) == sa.Finding("config", "FAIL", [str(target)])


def test_config_names_an_other_writable_folder(layout: Layout) -> None:
    os.chmod(layout.config, 0o757)
    assert sa.check_config(layout.config, ME) == sa.Finding("config", "FAIL", [str(layout.config)])


def test_config_fails_on_a_missing_folder(tmp_path: Path) -> None:
    missing = tmp_path / "etc" / "homelab"
    assert sa.check_config(missing, ME) == sa.Finding("config", "FAIL", [str(missing)])


def test_config_names_a_symlink_entry_because_its_target_would_be_read_unchecked(
        layout: Layout, tmp_path: Path) -> None:
    outside = _file(tmp_path / "outside.pub", 0o644)
    link = layout.config / "linked.pub"
    os.symlink(outside, link)
    assert sa.check_config(layout.config, ME) == sa.Finding("config", "FAIL", [str(link)])


# ------------------------------------------------------------ data


def test_data_passes_when_both_share_one_owner_and_nobody_else_has_access(layout: Layout) -> None:
    assert sa.check_data(layout.data) == sa.Finding("data", "ok", [])


def test_data_names_the_database_when_its_owner_differs_from_the_folder(
        layout: Layout, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_owner(monkeypatch, layout.db, ME + 1)
    assert sa.check_data(layout.data) == sa.Finding("data", "FAIL", [str(layout.db)])


def test_data_names_a_database_that_a_group_can_read(layout: Layout) -> None:
    os.chmod(layout.db, 0o640)
    assert sa.check_data(layout.data) == sa.Finding("data", "FAIL", [str(layout.db)])


def test_data_names_a_folder_that_others_can_enter(layout: Layout) -> None:
    os.chmod(layout.data, 0o701)
    assert sa.check_data(layout.data) == sa.Finding("data", "FAIL", [str(layout.data)])


def test_data_fails_on_a_missing_database(layout: Layout) -> None:
    layout.db.unlink()
    assert sa.check_data(layout.data) == sa.Finding("data", "FAIL", [str(layout.db)])


def test_data_fails_on_a_missing_folder_and_names_both(tmp_path: Path) -> None:
    missing = tmp_path / "var" / "homelab"
    assert sa.check_data(missing) == sa.Finding("data", "FAIL", sorted([
        str(missing), str(missing / "lab.db")]))


# ------------------------------------------------------------ public_key


def test_public_key_passes_when_root_owned_and_closed(layout: Layout) -> None:
    env = {"LAB_OPERATOR_PUBKEY": str(layout.config / "operator.pub")}
    assert sa.check_public_key(env, ME) == sa.Finding("public_key", "ok", [])


def test_public_key_names_the_file_when_it_is_group_writable(layout: Layout) -> None:
    target = layout.config / "operator.pub"
    os.chmod(target, 0o664)
    assert sa.check_public_key({"LAB_OPERATOR_PUBKEY": str(target)}, ME) == sa.Finding(
        "public_key", "FAIL", [str(target)])


def test_public_key_names_the_file_when_its_owner_is_another_uid(layout: Layout) -> None:
    target = layout.config / "operator.pub"
    assert sa.check_public_key({"LAB_OPERATOR_PUBKEY": str(target)}, ME + 1) == sa.Finding(
        "public_key", "FAIL", [str(target)])


@pytest.mark.parametrize("env", [{}, {"LAB_OPERATOR_PUBKEY": ""}, {"LAB_OPERATOR_PUBKEY": "  "}])
def test_public_key_fails_with_no_path_when_it_is_not_set(env: dict[str, str]) -> None:
    assert sa.check_public_key(env, ME) == sa.Finding("public_key", "FAIL", [])


def test_public_key_names_a_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "operator.pub"
    assert sa.check_public_key({"LAB_OPERATOR_PUBKEY": str(missing)}, ME) == sa.Finding(
        "public_key", "FAIL", [str(missing)])


# ------------------------------------------------------------ private_key


def test_private_key_passes_when_the_data_folder_has_another_owner(
        layout: Layout, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_owner(monkeypatch, layout.data, ME + 1)
    assert sa.check_private_key(layout.home, EUID, layout.data) == sa.Finding(
        "private_key", "ok", [])


def test_private_key_fails_when_the_data_folder_has_the_same_owner(layout: Layout) -> None:
    assert sa.check_private_key(layout.home, EUID, layout.data) == sa.Finding(
        "private_key", "FAIL", [str(layout.key)])


def test_private_key_fails_when_a_group_or_others_can_read_it(
        layout: Layout, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_owner(monkeypatch, layout.data, ME + 1)
    os.chmod(layout.key, 0o640)
    assert sa.check_private_key(layout.home, EUID, layout.data) == sa.Finding(
        "private_key", "FAIL", [str(layout.key)])


def test_private_key_fails_when_it_is_not_owned_by_this_account(
        layout: Layout, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_owner(monkeypatch, layout.data, ME + 2)
    assert sa.check_private_key(layout.home, EUID + 1, layout.data) == sa.Finding(
        "private_key", "FAIL", [str(layout.key)])


def test_private_key_fails_when_the_data_folder_is_missing(layout: Layout, tmp_path: Path) -> None:
    assert sa.check_private_key(layout.home, EUID, tmp_path / "missing") == sa.Finding(
        "private_key", "FAIL", [str(layout.key)])


def test_private_key_is_skipped_when_there_is_no_key(tmp_path: Path) -> None:
    assert sa.check_private_key(tmp_path / "home", EUID, tmp_path / "data") == sa.Finding(
        "private_key", "skip", [])


def test_private_key_is_skipped_when_this_account_cannot_see_it(
        layout: Layout, monkeypatch: pytest.MonkeyPatch) -> None:
    _deny(monkeypatch, layout.key)
    assert sa.check_private_key(layout.home, EUID, layout.data) == sa.Finding(
        "private_key", "skip", [])


# ------------------------------------------------------------ loopback


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8080/v1",
    "http://[::1]:8080/v1",
    "http://localhost:8080/v1",
    "http://LOCALHOST:8080/v1",
])
def test_loopback_passes_for_a_loopback_host(url: str) -> None:
    assert sa.check_loopback({"LAB_MODEL_URL": url}) == sa.Finding("loopback", "ok", [])


@pytest.mark.parametrize("env", [{}, {"LAB_MODEL_URL": ""}])
def test_loopback_passes_when_no_model_url_is_set(env: dict[str, str]) -> None:
    assert sa.check_loopback(env) == sa.Finding("loopback", "ok", [])


@pytest.mark.parametrize("url", [
    "http://192.0.2.10:8080/v1",
    "http://127.0.0.1.example.com:8080/v1",
    "http://[::1/v1",
    "not a url",
])
def test_loopback_fails_for_any_other_host(url: str) -> None:
    assert sa.check_loopback({"LAB_MODEL_URL": url}) == sa.Finding("loopback", "FAIL", [])


# ------------------------------------------------------------ run and the command


def test_run_gives_one_finding_per_check_in_order(layout: Layout) -> None:
    findings = sa.run({}, **layout.run_kwargs())
    assert [f.name for f in findings] == [
        "deploy", "services", "config", "data", "public_key", "private_key", "loopback"]


def test_the_audit_writes_nothing(layout: Layout, tmp_path: Path) -> None:
    os.chmod(layout.deploy / "lab" / "__init__.py", 0o664)     # a FAIL, so every branch runs
    os.symlink(_file(tmp_path / "outside.txt", 0o666), layout.deploy / "link")
    before = _snapshot(layout.base)
    env = {"LAB_OPERATOR_PUBKEY": str(layout.config / "operator.pub"),
           "LAB_MODEL_URL": "http://192.0.2.1:8080/v1"}
    findings = sa.run(env, **layout.run_kwargs())
    assert any(f.status == "FAIL" for f in findings)
    assert _snapshot(layout.base) == before


def _point_cli_at(monkeypatch: pytest.MonkeyPatch, layout: Layout, home: Path) -> None:
    """Make ``lab security-audit`` audit the layout, with ``home`` as the operator's home."""
    kwargs = {**layout.run_kwargs(), "home": home}
    monkeypatch.setattr(sa, "run", functools.partial(sa.run, **kwargs))
    monkeypatch.delenv("LAB_OPERATOR_PUBKEY", raising=False)
    monkeypatch.delenv("LAB_MODEL_URL", raising=False)


def test_the_command_prints_no_path_by_default(layout: Layout, monkeypatch: pytest.MonkeyPatch,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    os.chmod(layout.deploy / "lab" / "__init__.py", 0o664)
    _point_cli_at(monkeypatch, layout, layout.home)
    assert main(["security-audit"]) == 1
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines and all(re.fullmatch(r"[a-z_]+: (ok|FAIL|skip)", line) for line in lines)
    assert "deploy: FAIL" in lines
    assert str(layout.base) not in out


def test_the_command_details_print_each_offending_path_under_its_fail_line(
        layout: Layout, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    target = layout.deploy / "lab" / "__init__.py"
    os.chmod(target, 0o664)
    _point_cli_at(monkeypatch, layout, layout.home)
    assert main(["security-audit", "--details"]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines[lines.index("deploy: FAIL") + 1] == f"  {target}"


def test_the_command_exits_zero_when_no_check_fails(
        layout: Layout, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    # No operator key in this home, so the private key check is skipped, which is not a failure.
    _point_cli_at(monkeypatch, layout, layout.base / "no-home")
    monkeypatch.setenv("LAB_OPERATOR_PUBKEY", str(layout.config / "operator.pub"))
    assert main(["security-audit"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "deploy: ok", "services: ok", "config: ok", "data: ok", "public_key: ok",
        "private_key: skip", "loopback: ok"]
