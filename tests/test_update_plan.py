"""`lab update --plan COMMIT` prints the update steps of the runbook for this install."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lab import update_plan
from lab.cli import main

ROOT = Path(__file__).resolve().parent.parent
RUNBOOK = ROOT / "ops" / "runbook-lab-account-and-daemons.md"
SECTION = "## Updating the deployed code"
COMMIT = "1" * 40
OLD = "2" * 40
MISSING = "0123456789abcdef0123456789abcdef01234567"
REFUSED = "update: COMMIT must be a full 40 character commit hash\n"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True)
    return done.stdout.strip()


@pytest.fixture
def deploy(tmp_path: Path) -> Path:
    """A real checkout with one commit, standing in for /opt/homelab."""
    repo = tmp_path / "homelab"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "README.md").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-q",
         "-m", "one")
    return repo


@pytest.fixture
def launch_daemons(tmp_path: Path) -> Path:
    """A LaunchDaemons folder where only the supervisor and tick are installed."""
    folder = tmp_path / "LaunchDaemons"
    folder.mkdir()
    for name in ("supervisor", "tick"):
        (folder / f"com.homelab.{name}.plist").write_text("<plist/>\n", encoding="utf-8")
    return folder


@pytest.fixture(autouse=True)
def _restore_paths() -> Iterator[None]:
    paths = (update_plan.DEFAULT_DEPLOY, update_plan.DEFAULT_LAUNCH_DAEMONS)
    yield
    update_plan.DEFAULT_DEPLOY, update_plan.DEFAULT_LAUNCH_DAEMONS = paths


def _update(deploy: Path, launch_daemons: Path, commit: str) -> list[str]:
    """The command line, with the two places the command reads pointed at test folders.
    The command has no option for them: the runbook's commands name the real ones."""
    update_plan.DEFAULT_DEPLOY = deploy
    update_plan.DEFAULT_LAUNCH_DAEMONS = launch_daemons
    return ["update", "--plan", commit]


def test_the_plan_fills_in_both_hashes_and_the_installed_writers(
        deploy: Path, launch_daemons: Path, capsys: pytest.CaptureFixture[str]) -> None:
    head = _git(deploy, "rev-parse", "HEAD")
    assert main(_update(deploy, launch_daemons, head)) == 0
    out, err = capsys.readouterr()
    assert err == ""
    assert "WRITERS=(supervisor tick)\n" in out
    assert f"OLD={head}\n" in out
    assert f'COMMIT="{head}"\n' in out
    assert "The new commit is present in the deployed checkout." in out
    assert "PASTE_THE_NEW_COMMIT" not in out and "$(sudo git" not in out


def test_a_commit_that_is_not_in_the_checkout_says_the_fetch_is_needed(
        deploy: Path, launch_daemons: Path, capsys: pytest.CaptureFixture[str]) -> None:
    head = _git(deploy, "rev-parse", "HEAD")
    assert main(_update(deploy, launch_daemons, MISSING)) == 0
    out = capsys.readouterr().out
    assert f"OLD={head}\n" in out
    assert f'COMMIT="{MISSING}"\n' in out
    assert "is not present in the deployed checkout yet" in out
    assert "git fetch step" in out
    assert "The new commit is present" not in out


def test_only_the_installed_writers_are_listed_in_the_runbook_order(
        deploy: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    folder = tmp_path / "daemons"
    folder.mkdir()
    for name in ("chat", "weekly-eval", "supervisor"):
        (folder / f"com.homelab.{name}.plist").write_text("<plist/>\n", encoding="utf-8")
    head = _git(deploy, "rev-parse", "HEAD")
    assert main(_update(deploy, folder, head)) == 0
    assert "WRITERS=(supervisor weekly-eval chat)\n" in capsys.readouterr().out


@pytest.mark.parametrize("commit", [
    "abc123",
    "8F2620B4514CCC4D7D5F0949199470E8F70B0ED5",
    "g" * 40,
    "a" * 41,
    "a" * 40 + "\n",
])
def test_a_commit_that_is_not_40_lowercase_hex_is_refused(
        commit: str, deploy: Path, launch_daemons: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    assert main(_update(deploy, launch_daemons, commit)) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err == REFUSED


@pytest.mark.parametrize("folder_name", ["nowhere", "plain"])
def test_a_deploy_folder_git_cannot_read_is_one_line_that_names_it(
        folder_name: str, tmp_path: Path, launch_daemons: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    folder = tmp_path / folder_name
    if folder_name == "plain":
        folder.mkdir()
    assert main(_update(folder, launch_daemons, MISSING)) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err == f"update: cannot read the deployed commit in {folder}\n"


def test_the_command_does_not_open_the_database(
        deploy: Path, launch_daemons: Path, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "absent.db"
    head = _git(deploy, "rev-parse", "HEAD")
    assert main(["--db", str(db), *_update(deploy, launch_daemons, head)]) == 0
    assert not db.exists()


def test_git_runs_as_a_list_with_a_thirty_second_timeout(tmp_path: Path,
                                                         launch_daemons: Path) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout=f"{OLD}\n", stderr="")

    old, present, writers = update_plan.gather(COMMIT, tmp_path, launch_daemons, run=fake)
    assert (old, present, writers) == (OLD, True, ["supervisor", "tick"])
    assert calls[0][0][-2:] == ["rev-parse", "HEAD"]
    assert calls[1][0][-3:] == ["cat-file", "-e", f"{COMMIT}^{{commit}}"]
    for argv, kwargs in calls:
        assert isinstance(argv, list)
        assert argv[:3] == ["git", "-C", str(tmp_path)]
        assert kwargs["timeout"] == update_plan.GIT_TIMEOUT_SECONDS == 30
        assert kwargs.get("shell", False) is False


def test_git_is_told_to_trust_only_the_deploy_folder(tmp_path: Path,
                                                     launch_daemons: Path) -> None:
    calls: list[list[str]] = []

    def fake(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=f"{OLD}\n", stderr="")

    update_plan.gather(COMMIT, tmp_path, launch_daemons, run=fake)
    for argv in calls:
        assert argv[3:5] == ["-c", f"safe.directory={os.path.realpath(tmp_path)}"]


def test_a_checkout_another_account_owns_is_refused_before_git_runs(
        deploy: Path, launch_daemons: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(update_plan, "_owner", lambda path: os.geteuid() + 4242)
    assert main(_update(deploy, launch_daemons, COMMIT)) == 1
    err = capsys.readouterr().err
    assert err.startswith(f"update: {deploy} is owned by another account")


@pytest.mark.skipif(os.geteuid() != 0, reason="needs root to give the checkout another owner")
def test_git_reads_a_root_style_checkout_another_account_owns(
        deploy: Path, launch_daemons: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # On the Mac mini the checkout is root's and the operator runs this. Here the
    # roles are swapped: the checkout goes to another uid, which plain git refuses.
    head = _git(deploy, "rev-parse", "HEAD")
    for folder, dirs, files in os.walk(deploy):
        for name in (folder, *(os.path.join(folder, n) for n in (*dirs, *files))):
            os.lchown(name, 65534, 65534)
    with pytest.raises(subprocess.CalledProcessError):
        _git(deploy, "rev-parse", "HEAD")
    monkeypatch.setattr(update_plan, "_owner", lambda path: 0)
    assert update_plan.gather(head, deploy, launch_daemons) == (head, True, ["supervisor",
                                                                            "tick"])


def test_git_ignores_a_git_dir_in_the_callers_environment(
        deploy: Path, launch_daemons: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    _git(other, "-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-q",
         "--allow-empty", "-m", "other")
    head = _git(deploy, "rev-parse", "HEAD")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    assert update_plan.gather(head, deploy, launch_daemons)[0] == head


def test_a_runbook_block_moved_to_another_step_is_refused(
        monkeypatch: pytest.MonkeyPatch) -> None:
    section = update_plan._read_section()
    chunks = update_plan._command_chunks(section)
    swapped = [chunks[1], chunks[0], *chunks[2:]]
    monkeypatch.setattr(update_plan, "_command_chunks", lambda lines: swapped)
    with pytest.raises(update_plan.UpdateError, match="command block 1"):
        update_plan.render(COMMIT, OLD, True, ["supervisor"])


def test_a_runbook_that_is_not_utf8_is_one_line(tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    bad = tmp_path / "runbook.md"
    bad.write_bytes(b"\xff\xfe not text")
    monkeypatch.setattr(update_plan, "RUNBOOK", bad)
    with pytest.raises(update_plan.UpdateError, match="cannot read the runbook"):
        update_plan.render(COMMIT, OLD, True, ["supervisor"])


def test_the_command_has_no_option_to_read_another_checkout(
        capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["update", "--plan", COMMIT, "--deploy", "/tmp/elsewhere"])
    assert "unrecognized arguments" in capsys.readouterr().err


def test_a_git_call_that_times_out_is_an_update_error(tmp_path: Path,
                                                      launch_daemons: Path) -> None:
    def hangs(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, 30)

    with pytest.raises(update_plan.UpdateError, match="cannot run git"):
        update_plan.gather(COMMIT, tmp_path, launch_daemons, run=hangs)


def test_the_plan_writes_no_file(deploy: Path, launch_daemons: Path,
                                 capsys: pytest.CaptureFixture[str]) -> None:
    def snapshot() -> dict[Path, tuple[int, int]]:
        return {p: (p.stat().st_mtime_ns, p.stat().st_size)
                for p in deploy.parent.rglob("*") if p.is_file()}

    before = snapshot()
    head = _git(deploy, "rev-parse", "HEAD")
    assert main(_update(deploy, launch_daemons, head)) == 0
    assert snapshot() == before


def _section() -> str:
    lines = RUNBOOK.read_text(encoding="utf-8").splitlines()
    start = lines.index(SECTION)
    end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("## "))
    return "\n".join(lines[start:end])


def _runbook_line(line: str) -> str:
    """Undo the three substitutions: the line as the runbook writes it."""
    if line == f'COMMIT="{COMMIT}"':
        return 'COMMIT="PASTE_THE_NEW_COMMIT"'
    if line == f"OLD={OLD}":
        return "OLD=$(sudo git -C /opt/homelab rev-parse HEAD)"
    if line.startswith("WRITERS=("):
        return "WRITERS=(supervisor tick selftest weekly-eval chat)"
    return line


@pytest.mark.parametrize("writers", [
    [],
    ["supervisor", "tick"],
    list(update_plan.WRITER_NAMES),
])
def test_every_command_line_the_plan_prints_is_a_runbook_line(writers: list[str]) -> None:
    section = _section()
    commands = [line.text for line in update_plan.plan(COMMIT, OLD, True, writers)
                if line.command]
    assert len(commands) >= 30, "the plan found too few command lines in the section"
    missing = [c for c in commands if _runbook_line(c) not in section]
    assert not missing, f"not the runbook's own lines: {missing}"


def test_the_plan_keeps_the_runbook_order_and_leaves_only_n_for_the_operator() -> None:
    text = update_plan.render(COMMIT, OLD, True, ["supervisor", "tick"])
    headings = ["Set the values", "Back up before", "Stop the jobs", "Check out the new code",
                "Try the migrations", "Start the jobs again", "Roll back before",
                "Roll back after"]
    positions = [text.index(h) for h in headings]
    assert positions == sorted(positions)
    assert [line for line in text.splitlines() if "PASTE_" in line] == ['N="PASTE_N"']


def test_the_plan_never_says_to_reinstall_the_keep_awake_definition() -> None:
    """Moving keep-awake to lab waits on its own checks (#235), so the stale definitions
    note names it as the exception, as the runbook's diff --stat point does."""
    assert "keepawake.plist` is listed,** do not reinstall it" in _section()
    text = update_plan.render(COMMIT, OLD, True, list(update_plan.WRITER_NAMES))
    note = next(line for line in text.splitlines() if "its installed copy is stale" in line)
    assert "Not the keep-awake definition" in note
    assert "Moving keep-awake to the lab account" in note


# `lab update --verify COMMIT`: the check after the operator's run (#339).

def test_check_code_passes_only_for_a_package_inside_the_deployment(tmp_path: Path) -> None:
    deploy = tmp_path / "homelab"
    inside = deploy / ".venv" / "lib" / "lab"
    inside.mkdir(parents=True)
    assert update_plan.check_code(deploy, inside).ok
    outside = tmp_path / "checkout" / "lab"
    outside.mkdir(parents=True)
    check = update_plan.check_code(deploy, outside)
    assert not check.ok
    assert f"{deploy}/.venv/bin/python" in check.message


def test_check_deployed_compares_the_checkout_with_the_commit(deploy: Path) -> None:
    head = _git(deploy, "rev-parse", "HEAD")
    assert update_plan.check_deployed(head, deploy).ok
    check = update_plan.check_deployed(MISSING, deploy)
    assert not check.ok
    assert check.message.startswith(f"{deploy} is at {head}, not {MISSING}")


def test_check_deployed_fails_when_git_cannot_read_the_folder(tmp_path: Path) -> None:
    check = update_plan.check_deployed(MISSING, tmp_path / "nowhere")
    assert not check.ok
    assert "cannot read the deployed commit" in check.message


def _manifest(folder: Path, stamp: str) -> None:
    """A manifest and the database file it names, as lab backup writes them."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"lab-{stamp}.db").write_bytes(b"x" * 10)
    (folder / f"lab-{stamp}.manifest.json").write_text(
        json.dumps({"database": f"lab-{stamp}.db", "database_bytes": 10}), encoding="utf-8")


NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


def test_the_before_update_backup_is_skipped_without_a_folder() -> None:
    check = update_plan.check_before_update_backup(None, NOW)
    assert check.ok and check.skipped


def test_a_backup_from_the_last_day_passes_and_is_named(tmp_path: Path) -> None:
    _manifest(tmp_path, "20261021T080000Z")
    _manifest(tmp_path, "20261022T110000Z")
    check = update_plan.check_before_update_backup(tmp_path, NOW)
    assert check.ok and not check.skipped
    assert check.message == "lab-20261022T110000Z.manifest.json"


@pytest.mark.parametrize("stamp", ["20261021T115959Z", "20261022T130000Z"])
def test_an_old_or_future_backup_fails(stamp: str, tmp_path: Path) -> None:
    _manifest(tmp_path, stamp)
    check = update_plan.check_before_update_backup(tmp_path, NOW)
    assert not check.ok
    assert f"lab-{stamp}.manifest.json" in check.message


def test_a_folder_with_no_backup_fails(tmp_path: Path) -> None:
    check = update_plan.check_before_update_backup(tmp_path, NOW)
    assert not check.ok
    assert "no backup manifest" in check.message


def test_verify_prints_its_checks_then_the_doctor_checks_and_opens_no_database(
        deploy: Path, launch_daemons: Path, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "absent.db"
    head = _git(deploy, "rev-parse", "HEAD")
    update_plan.DEFAULT_DEPLOY = deploy
    assert main(["--db", str(db), "update", "--verify", head]) == 1
    lines = capsys.readouterr().out.splitlines()
    # This test runs from the checkout, not from the stand-in deployment.
    assert lines[0].startswith("FAIL code: this command runs from ")
    assert lines[1] == "ok deployed"
    assert lines[2] == "skip before-update backup: no --backup-dir given, so not checked"
    assert lines[3].startswith("FAIL database: no database at ")
    assert not db.exists()


def test_backup_dir_without_verify_is_refused(
        deploy: Path, launch_daemons: Path, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    head = _git(deploy, "rev-parse", "HEAD")
    assert main([*_update(deploy, launch_daemons, head), "--backup-dir", str(tmp_path)]) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err == "update: --backup-dir goes with --verify\n"


def test_verify_refuses_a_short_commit(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["update", "--verify", "abc123"]) == 1
    assert capsys.readouterr().err == REFUSED


def test_check_deployed_fails_for_a_changed_or_new_file(deploy: Path) -> None:
    head = _git(deploy, "rev-parse", "HEAD")
    (deploy / "README.md").write_text("changed\n", encoding="utf-8")
    check = update_plan.check_deployed(head, deploy)
    assert not check.ok
    assert "1 files differ from it" in check.message
    _git(deploy, "checkout", "--", "README.md")
    (deploy / "extra.py").write_text("x = 1\n", encoding="utf-8")
    assert not update_plan.check_deployed(head, deploy).ok


def test_check_deployed_ignores_ignored_files(deploy: Path) -> None:
    (deploy / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    _git(deploy, "add", ".gitignore")
    _git(deploy, "-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-q",
         "-m", "ignore")
    (deploy / ".venv").mkdir()
    (deploy / ".venv" / "python").write_text("", encoding="utf-8")
    assert update_plan.check_deployed(_git(deploy, "rev-parse", "HEAD"), deploy).ok


@pytest.mark.parametrize(("change", "problem"), [
    ("unreadable", "the manifest cannot be read"),
    ("no database", "its database file is missing"),
    ("wrong size", "its database file is not the one the manifest records"),
    ("outside", "the manifest names a database outside its folder"),
])
def test_a_recent_backup_that_is_not_usable_fails(
        change: str, problem: str, tmp_path: Path) -> None:
    stamp = "20261022T110000Z"
    _manifest(tmp_path, stamp)
    manifest = tmp_path / f"lab-{stamp}.manifest.json"
    if change == "unreadable":
        manifest.write_text("{", encoding="utf-8")
    elif change == "no database":
        (tmp_path / f"lab-{stamp}.db").unlink()
    elif change == "wrong size":
        (tmp_path / f"lab-{stamp}.db").write_bytes(b"x")
    else:
        manifest.write_text(json.dumps({"database": "../lab.db", "database_bytes": 10}),
                            encoding="utf-8")
    check = update_plan.check_before_update_backup(tmp_path, NOW)
    assert not check.ok
    assert check.message.startswith(f"lab-{stamp}.manifest.json: {problem}, so ")
