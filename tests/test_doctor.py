"""``lab doctor`` (#347): one read-only check per concern, each with a pass and a fail test.

The database checks use tmp_path databases, the environment is a plain dict, and the
model check talks to a fake OpenAI-style server on 127.0.0.1. Nothing here reads the
operator's real environment or key, and nothing is written outside tmp_path.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
import urllib.request
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from lab import doctor
from lab import operator as op
from lab.cli import main
from lab.migrations import latest_version
from lab.queue import TaskQueue

NAMES = ["database", "operator_key", "model", "disk", "backup", "selftest", "audit_chain",
         "versions", "lab_account", "permissions", "services"]


def _no_commands(argv: Sequence[str]) -> doctor.Command:
    raise AssertionError(f"no command should run in these tests: {list(argv)}")


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A machine with the deployed operator key installed must not change these results.

    The default host is Linux with no pyproject.toml, no launchd folder and no commands, so
    the command tests do not depend on the machine they run on. Nothing is created under
    tmp_path here, because the command tests list it.
    """
    monkeypatch.setattr(doctor, "DEPLOYED_OPERATOR_KEY", tmp_path / "deployed" / "operator.pub")
    monkeypatch.setattr(doctor, "DEFAULT_HOST", doctor.Host(
        system="linux", launch_daemons=tmp_path / "LaunchDaemons",
        pyproject=tmp_path / "pyproject.toml", runner=_no_commands, owner_name=lambda _: None))
    for name in ("LAB_MODEL_URL", "LAB_MODEL_NAME", "LAB_BACKUP_DIR", "LAB_OPERATOR_PUBKEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    path = tmp_path / "lab.db"
    TaskQueue(path, owner="setup").close()
    return path


def record(db: Path, kind: str, detail: dict[str, object] | None = None) -> None:
    with TaskQueue(db, owner="test") as queue:
        queue.record_event(None, kind, detail)


def stamp(hours_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).strftime("%Y%m%dT%H%M%SZ")


@dataclass
class FakeModelServer:
    url: str
    requests: list[str] = field(default_factory=list)


def operator_public_key(tmp_path: Path) -> Path:
    """A real Ed25519 public key, written the way lab operator init writes it."""
    return op.generate(tmp_path / "operator-keys")[1]


@pytest.fixture()
def serve() -> Iterator[Callable[..., FakeModelServer]]:
    """Start fake model servers on loopback. They are shut down at teardown.

    ``raw`` replaces the model list with those bytes, and ``drip`` sends the headers and
    then one byte every ``drip`` seconds, for longer than any test waits.
    """
    servers: list[ThreadingHTTPServer] = []

    def start(models: tuple[str, ...] = ("lab-model",), *, delay: float = 0.0,
              redirect_to: str | None = None, raw: bytes | None = None,
              drip: float = 0.0) -> FakeModelServer:
        fake = FakeModelServer(url="")

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                fake.requests.append(self.path)
                if self.path != "/v1/models":
                    self.send_error(404)
                    return
                time.sleep(delay)
                if redirect_to is not None:
                    self.send_response(302)
                    self.send_header("Location", redirect_to)
                    self.end_headers()
                    return
                if drip:
                    self.send_response(200)
                    self.send_header("Content-Length", "100000")
                    self.end_headers()
                    for _ in range(200):   # bounded, so a test that forgets to stop cannot hang
                        time.sleep(drip)
                        try:
                            self.wfile.write(b" ")
                        except OSError:    # doctor gave up and closed the connection
                            return
                    return
                body = raw if raw is not None else json.dumps({"object": "list", "data": [
                    {"id": model, "object": "model"} for model in models]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        servers.append(httpd)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        fake.url = f"http://127.0.0.1:{httpd.server_address[1]}/v1"
        return fake

    yield start
    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()


# ------------------------------------------------------------ database


def test_database_passes_on_a_current_schema(db: Path) -> None:
    assert doctor.check_database(db) == doctor.Check("database", True)


def test_database_fails_on_an_old_schema_and_leaves_it_unmigrated(db: Path) -> None:
    raw = sqlite3.connect(db)
    raw.execute(f"PRAGMA user_version = {latest_version() - 1}")
    raw.close()
    check = doctor.check_database(db)
    assert not check.ok and "behind" in check.message
    raw = sqlite3.connect(db)
    version = raw.execute("PRAGMA user_version").fetchone()[0]
    raw.close()
    assert version == latest_version() - 1


# ------------------------------------------------------------ operator_key


def test_operator_key_passes_when_no_one_else_can_write_it(tmp_path: Path) -> None:
    key = operator_public_key(tmp_path)
    os.chmod(key, 0o644)
    assert doctor.check_operator_key({"LAB_OPERATOR_PUBKEY": str(key)}) == doctor.Check(
        "operator_key", True)


def test_operator_key_fails_when_the_group_or_others_can_write_it(tmp_path: Path) -> None:
    key = operator_public_key(tmp_path)
    os.chmod(key, 0o664)
    check = doctor.check_operator_key({"LAB_OPERATOR_PUBKEY": str(key)})
    assert not check.ok and "group or others" in check.message


def test_operator_key_fails_when_the_file_is_garbage(tmp_path: Path) -> None:
    key = tmp_path / "operator.pub"
    key.write_text("public key")
    os.chmod(key, 0o644)
    check = doctor.check_operator_key({"LAB_OPERATOR_PUBKEY": str(key)})
    assert check == doctor.Check("operator_key", False, f"{key} is not a usable operator public "
                                 "key, so point LAB_OPERATOR_PUBKEY at the operator.pub that "
                                 "lab operator init wrote")


def test_operator_key_fails_when_it_is_the_private_key_and_does_not_print_it(
        tmp_path: Path) -> None:
    private, _ = op.generate(tmp_path / "operator-keys")
    check = doctor.check_operator_key({"LAB_OPERATOR_PUBKEY": str(private)})
    assert not check.ok and "not a usable operator public key" in check.message
    assert "PRIVATE" not in check.line()


def test_operator_key_fails_when_none_is_configured() -> None:
    check = doctor.check_operator_key({})
    assert not check.ok and "LAB_OPERATOR_PUBKEY" in check.message


# ------------------------------------------------------------ model


def test_model_passes_when_the_named_model_is_listed(serve: Callable[..., FakeModelServer]
                                                     ) -> None:
    server = serve(("lab-model", "other"))
    env = {"LAB_MODEL_URL": server.url, "LAB_MODEL_NAME": "lab-model"}
    assert doctor.check_model(env) == doctor.Check("model", True)
    assert doctor.check_model({}) == doctor.Check("model", True, "not configured")


def test_model_fails_when_the_named_model_is_not_listed(serve: Callable[..., FakeModelServer]
                                                        ) -> None:
    server = serve(("other",))
    check = doctor.check_model({"LAB_MODEL_URL": server.url, "LAB_MODEL_NAME": "lab-model"})
    assert not check.ok and "lab-model" in check.message


@pytest.mark.parametrize("url", [
    "http://192.0.2.10:8080/v1",
    "http://127.0.0.1.example.com:8080/v1",
    "ftp://127.0.0.1/v1",
    "http://user:secret@127.0.0.1:8080/v1",
])
def test_model_refuses_a_non_loopback_url_without_any_network_call(
        url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("doctor tried to open a connection")

    monkeypatch.setattr(urllib.request, "build_opener", no_network)
    check = doctor.check_model({"LAB_MODEL_URL": url})
    assert not check.ok and "loopback" in check.message
    assert "secret" not in check.line()


def test_model_fails_when_the_answer_is_slower_than_the_limit(
        serve: Callable[..., FakeModelServer], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "MODEL_TIMEOUT_SECONDS", 0.2)
    server = serve(delay=1.0)
    check = doctor.check_model({"LAB_MODEL_URL": server.url})
    assert not check.ok and "within 0.2 seconds" in check.message


def test_model_fails_within_the_deadline_when_the_reply_drips_in(
        serve: Callable[..., FakeModelServer], monkeypatch: pytest.MonkeyPatch) -> None:
    # Headers arrive at once and then a byte every 0.1 seconds, so no single read stalls.
    # Only the total deadline ends it. The old single read would have waited for the body.
    monkeypatch.setattr(doctor, "MODEL_TIMEOUT_SECONDS", 0.5)
    server = serve(drip=0.1)
    started = time.monotonic()
    check = doctor.check_model({"LAB_MODEL_URL": server.url})
    elapsed = time.monotonic() - started
    assert not check.ok and "within 0.5 seconds" in check.message
    assert elapsed < 2.0


def test_model_fails_when_the_reply_is_over_the_size_limit(
        serve: Callable[..., FakeModelServer], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_MODELS_MAX_BYTES", 16)
    server = serve(("lab-model",))
    check = doctor.check_model({"LAB_MODEL_URL": server.url})
    assert not check.ok and "more than 16 bytes" in check.message


@pytest.mark.parametrize("raw", [
    pytest.param(b"<html><body>Model server</body></html>", id="html"),
    pytest.param(b'{"data": [', id="bad-json"),
    pytest.param(b'{"object": "list"}', id="no-data-list"),
    pytest.param(b'{"data": {"id": "lab-model"}}', id="data-not-a-list"),
    pytest.param(b'{"data": [{"name": "lab-model"}]}', id="item-without-id"),
])
def test_model_fails_when_the_reply_is_not_a_model_list_even_with_no_name_configured(
        serve: Callable[..., FakeModelServer], raw: bytes) -> None:
    server = serve(raw=raw)
    check = doctor.check_model({"LAB_MODEL_URL": server.url})
    assert check == doctor.Check("model", False, "the server at LAB_MODEL_URL did not answer "
                                 "GET /models with a model list, so check that LAB_MODEL_URL "
                                 "points at the model server")


def test_model_passes_without_a_name_when_the_reply_is_a_model_list(
        serve: Callable[..., FakeModelServer]) -> None:
    server = serve(("lab-model",))
    assert doctor.check_model({"LAB_MODEL_URL": server.url}) == doctor.Check("model", True)


def test_model_does_not_follow_a_redirect_off_loopback(
        serve: Callable[..., FakeModelServer]) -> None:
    target = serve()
    redirecting = serve(redirect_to=f"{target.url}/models")
    check = doctor.check_model({"LAB_MODEL_URL": redirecting.url})
    assert not check.ok
    assert target.requests == []


def test_model_ignores_a_proxy_set_in_the_environment(
        serve: Callable[..., FakeModelServer], monkeypatch: pytest.MonkeyPatch) -> None:
    server = serve(("lab-model",))
    for name in ("no_proxy", "NO_PROXY"):   # a list that names 127.0.0.1 would hide the proxy
        monkeypatch.delenv(name, raising=False)
    for name in ("http_proxy", "HTTP_PROXY"):
        monkeypatch.setenv(name, "http://192.0.2.1:3128")
    check = doctor.check_model({"LAB_MODEL_URL": server.url, "LAB_MODEL_NAME": "lab-model"})
    assert check == doctor.Check("model", True)
    assert server.requests == ["/v1/models"]


# ------------------------------------------------------------ disk


def test_disk_passes_when_the_volume_has_room(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "MIN_FREE_BYTES", 1)
    assert doctor.check_disk(db) == doctor.Check("disk", True)


def test_disk_fails_below_the_minimum(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = SimpleNamespace(total=10**10, used=9 * 10**9, free=10**9)
    monkeypatch.setattr(doctor.shutil, "disk_usage", lambda path: fake)
    check = doctor.check_disk(db)
    assert not check.ok and "1.0 GB is free" in check.message and "5 GB" in check.message


# ------------------------------------------------------------ backup


def test_backup_passes_when_the_newest_backup_is_recent(tmp_path: Path) -> None:
    (tmp_path / f"lab-{stamp(1)}.manifest.json").write_text("{}")
    assert doctor.check_backup({"LAB_BACKUP_DIR": str(tmp_path)}) == doctor.Check("backup", True)
    assert doctor.check_backup({}) == doctor.Check("backup", True, "not configured")


def test_backup_fails_when_the_newest_backup_is_older_than_36_hours(tmp_path: Path) -> None:
    (tmp_path / f"lab-{stamp(40)}.manifest.json").write_text("{}")
    (tmp_path / f"lab-{stamp(50)}.manifest.json").write_text("{}")
    check = doctor.check_backup({"LAB_BACKUP_DIR": str(tmp_path)})
    assert not check.ok and "hours old" in check.message


def test_backup_fails_when_the_newest_backup_is_dated_in_the_future(tmp_path: Path) -> None:
    name = f"lab-{stamp(-2)}.manifest.json"
    (tmp_path / name).write_text("{}")
    assert doctor.check_backup({"LAB_BACKUP_DIR": str(tmp_path)}) == doctor.Check(
        "backup", False, f"the newest backup {name} is dated in the future, so check the clock")


# ------------------------------------------------------------ selftest


def test_selftest_passes_when_the_last_run_passed(db: Path) -> None:
    record(db, "selftest", {"ok": True, "checks": []})
    assert doctor.check_selftest(db) == doctor.Check("selftest", True)


def test_selftest_fails_when_the_last_run_failed(db: Path) -> None:
    record(db, "selftest", {"ok": True, "checks": []})
    record(db, "selftest", {"ok": False, "checks": []})
    check = doctor.check_selftest(db)
    assert not check.ok and "last selftest failed" in check.message


def test_selftest_asks_for_a_first_run_when_none_is_recorded(db: Path) -> None:
    assert doctor.check_selftest(db) == doctor.Check(
        "selftest", False, "no selftest has been recorded yet, so run lab selftest")


# ------------------------------------------------------------ audit_chain


def test_audit_chain_passes_on_an_untouched_log(db: Path) -> None:
    record(db, "note", {"n": 1})
    record(db, "note", {"n": 2})
    assert doctor.check_audit_chain(db) == doctor.Check("audit_chain", True)


def test_audit_chain_fails_when_a_row_was_edited(db: Path) -> None:
    record(db, "note", {"n": 1})
    record(db, "note", {"n": 2})
    raw = sqlite3.connect(db)
    raw.execute("DROP TRIGGER events_no_update")   # the append-only guard, bypassed for the test
    raw.execute("UPDATE events SET detail = '{\"n\": 99}' WHERE id = (SELECT MIN(id) FROM events)")
    raw.commit()
    raw.close()
    check = doctor.check_audit_chain(db)
    assert not check.ok and "audit chain breaks" in check.message


# ------------------------------------------------------------ the host checks


@dataclass
class FakeRunner:
    """A command runner that answers the same way for every command and records each one."""
    returncode: int = 0
    output: str = ""
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, argv: Sequence[str]) -> doctor.Command:
        self.calls.append(list(argv))
        return doctor.Command(self.returncode, self.output)


def launchd_knows(*loaded: str) -> Callable[[Sequence[str]], doctor.Command]:
    """A runner that answers like launchctl print for the labels in ``loaded``."""
    def run(argv: Sequence[str]) -> doctor.Command:
        label = argv[-1].removeprefix("system/")
        if label in loaded:
            return doctor.Command(0, f"{label} = {{\n}}\n")
        return doctor.Command(113, f'Bad request.\nCould not find service "{label}" in domain '
                              "for system")
    return run


PYPROJECT_TEXT = '[project]\nrequires-python = ">=3.13"\ndependencies = ["cryptography>=45"]\n'


def write_pyproject(tmp_path: Path) -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text(PYPROJECT_TEXT)
    return path


def write_plists(folder: Path, *labels: str) -> None:
    folder.mkdir()
    for label in labels:
        (folder / f"{label}.plist").write_text("")


def test_versions_reports_python_and_the_dependencies_as_info(tmp_path: Path) -> None:
    check = doctor.check_versions(write_pyproject(tmp_path), python="3.13.2",
                                  installed={"cryptography": "45.1.0"}.get)
    assert check == doctor.Check("versions", True, "python 3.13.2, cryptography 45.1.0")


def test_versions_fails_when_python_is_older_than_pyproject_requires(tmp_path: Path) -> None:
    check = doctor.check_versions(write_pyproject(tmp_path), python="3.12.9",
                                  installed={"cryptography": "45.1.0"}.get)
    assert not check.ok and not check.skipped
    assert check.line().startswith("FAIL versions:") and "3.13" in check.message


def test_versions_reports_a_missing_dependency_without_failing(tmp_path: Path) -> None:
    check = doctor.check_versions(write_pyproject(tmp_path), python="3.13.0",
                                  installed=lambda _: None)
    assert check == doctor.Check("versions", True, "python 3.13.0, cryptography not installed")


def test_versions_skips_when_pyproject_is_missing(tmp_path: Path) -> None:
    check = doctor.check_versions(tmp_path / "pyproject.toml", python="3.13.0",
                                  installed=lambda _: None)
    assert check.skipped and check.ok
    assert check.line().startswith("skip versions:") and "no pyproject.toml" in check.message


def test_lab_account_passes_when_the_owner_is_not_an_admin(tmp_path: Path) -> None:
    runner = FakeRunner(output="staff everyone localaccounts")
    check = doctor.check_lab_account(tmp_path / "lab.db", system="darwin", runner=runner,
                                     owner_name=lambda _: "lab")
    assert check == doctor.Check("lab_account", True, "lab")
    assert runner.calls == [["id", "-Gn", "lab"]]


@pytest.mark.parametrize("group", ["admin", "wheel"])
def test_lab_account_fails_when_the_owner_is_in_an_admin_group(tmp_path: Path,
                                                               group: str) -> None:
    runner = FakeRunner(output=f"staff {group} everyone")
    check = doctor.check_lab_account(tmp_path / "lab.db", system="darwin", runner=runner,
                                     owner_name=lambda _: "lab")
    assert not check.ok and not check.skipped
    assert f"{group} group" in check.message and "must not be an admin" in check.message


def test_lab_account_fails_when_the_owner_has_no_account(tmp_path: Path) -> None:
    runner = FakeRunner()
    check = doctor.check_lab_account(tmp_path / "lab.db", system="darwin", runner=runner,
                                     owner_name=lambda _: None)
    assert check.line().startswith("FAIL lab_account:") and runner.calls == []


def test_lab_account_fails_when_id_cannot_read_the_owner(tmp_path: Path) -> None:
    runner = FakeRunner(returncode=1, output="id: ghost: no such user")
    check = doctor.check_lab_account(tmp_path / "lab.db", system="darwin", runner=runner,
                                     owner_name=lambda _: "ghost")
    assert check.line().startswith("FAIL lab_account:")
    assert "check the account exists" in check.message


def test_lab_account_skips_off_macos_without_running_a_command(tmp_path: Path) -> None:
    runner = FakeRunner()
    check = doctor.check_lab_account(tmp_path / "lab.db", system="linux", runner=runner,
                                     owner_name=lambda _: "lab")
    assert check.skipped and check.ok and runner.calls == []


def test_lab_account_skips_when_the_data_folder_is_missing(tmp_path: Path) -> None:
    check = doctor.check_lab_account(tmp_path / "missing" / "lab.db", system="darwin",
                                     runner=FakeRunner(), owner_name=lambda _: "lab")
    assert check.skipped and check.line().startswith("skip lab_account:")


def test_permissions_passes_when_no_one_else_can_write_the_folder_or_the_file(
        db: Path) -> None:
    os.chmod(db.parent, 0o755)
    os.chmod(db, 0o644)
    assert doctor.check_permissions(db) == doctor.Check("permissions", True)


def test_permissions_fails_when_the_group_can_write_the_data_folder(db: Path) -> None:
    os.chmod(db.parent, 0o775)
    os.chmod(db, 0o644)
    check = doctor.check_permissions(db)
    assert not check.ok and "the data folder" in check.message and "chmod go-w" in check.message


def test_permissions_fails_when_others_can_write_the_database_file(db: Path) -> None:
    os.chmod(db.parent, 0o755)
    os.chmod(db, 0o646)
    check = doctor.check_permissions(db)
    assert check.line().startswith("FAIL permissions:") and "the database file" in check.message


def test_permissions_skips_when_there_is_no_database(tmp_path: Path) -> None:
    check = doctor.check_permissions(tmp_path / "lab.db")
    assert check.skipped and check.line().startswith("skip permissions:")


def test_services_passes_when_every_homelab_job_is_loaded(tmp_path: Path) -> None:
    folder = tmp_path / "LaunchDaemons"
    write_plists(folder, "com.homelab.lab", "com.homelab.model")
    (folder / "com.apple.other.plist").write_text("")
    runner = FakeRunner(returncode=0)
    check = doctor.check_services(folder, system="darwin", runner=runner)
    assert check == doctor.Check("services", True, "2 com.homelab jobs loaded")
    assert runner.calls == [["launchctl", "print", "system/com.homelab.lab"],
                            ["launchctl", "print", "system/com.homelab.model"]]


def test_services_fails_and_names_a_job_that_is_not_loaded(tmp_path: Path) -> None:
    folder = tmp_path / "LaunchDaemons"
    write_plists(folder, "com.homelab.lab", "com.homelab.model")
    check = doctor.check_services(folder, system="darwin",
                                  runner=launchd_knows("com.homelab.lab"))
    assert check.line().startswith("FAIL services:") and "com.homelab.model" in check.message
    assert "com.homelab.lab" not in check.message


def test_services_skips_rather_than_fails_when_launchctl_needs_admin_rights(
        tmp_path: Path) -> None:
    folder = tmp_path / "LaunchDaemons"
    write_plists(folder, "com.homelab.lab")
    runner = FakeRunner(returncode=1, output="Operation not permitted")
    check = doctor.check_services(folder, system="darwin", runner=runner)
    assert check.skipped and check.ok and "sudo launchctl print" in check.message


def test_services_skips_off_macos_without_running_a_command(tmp_path: Path) -> None:
    folder = tmp_path / "LaunchDaemons"
    write_plists(folder, "com.homelab.lab")
    runner = FakeRunner()
    check = doctor.check_services(folder, system="linux", runner=runner)
    assert check.skipped and runner.calls == []


def test_services_skips_when_no_homelab_plist_is_installed(tmp_path: Path) -> None:
    folder = tmp_path / "LaunchDaemons"
    write_plists(folder, "com.apple.other")
    runner = FakeRunner()
    check = doctor.check_services(folder, system="darwin", runner=runner)
    assert check.skipped and "no com.homelab plist" in check.message and runner.calls == []


def test_services_skips_when_the_launchd_folder_is_missing(tmp_path: Path) -> None:
    check = doctor.check_services(tmp_path / "missing", system="darwin", runner=FakeRunner())
    assert check.skipped and check.line().startswith("skip services:")


def test_run_command_returns_the_output_of_a_program() -> None:
    result = doctor.run_command([sys.executable, "-c", "print('ok')"])
    assert result == doctor.Command(0, "ok\n")


def test_run_command_reports_a_missing_program_instead_of_raising() -> None:
    assert doctor.run_command(["/nonexistent/lab-doctor-probe"]) == doctor.Command(-1, "")


# ------------------------------------------------------------ the command


def test_doctor_does_not_create_a_missing_database(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "lab.db"
    assert main(["--db", str(missing), "doctor"]) == 1
    assert not missing.exists()
    assert list(tmp_path.iterdir()) == []
    assert "FAIL database" in capsys.readouterr().out


def test_doctor_leaves_the_database_file_byte_for_byte_unchanged(db: Path, capsys) -> None:
    before = db.read_bytes()
    assert main(["--db", str(db), "doctor"]) == 1
    capsys.readouterr()
    assert db.read_bytes() == before


def test_a_fresh_database_with_nothing_configured_fails_and_lists_every_check(
        db: Path, capsys) -> None:
    assert main(["--db", str(db), "doctor"]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert [line.split()[1].rstrip(":") for line in lines] == NAMES
    assert all(line.startswith(("ok ", "FAIL ", "skip ")) for line in lines)
    assert lines[0] == "ok database"
    assert lines[2] == "ok model: not configured"
    assert lines[4] == "ok backup: not configured"
    assert lines[1].startswith("FAIL operator_key:")
    assert lines[5].startswith("FAIL selftest:") and "run lab selftest" in lines[5]
    assert lines[7].startswith("skip versions:")
    assert lines[8].startswith("skip lab_account:")
    assert lines[9] == "ok permissions"
    assert lines[10].startswith("skip services:")


def test_doctor_exits_zero_when_every_check_passes(
        db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    key = operator_public_key(tmp_path)
    os.chmod(key, 0o644)
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / f"lab-{stamp(1)}.manifest.json").write_text("{}")
    record(db, "selftest", {"ok": True, "checks": []})
    monkeypatch.setattr(doctor, "MIN_FREE_BYTES", 1)
    monkeypatch.setenv("LAB_OPERATOR_PUBKEY", str(key))
    monkeypatch.setenv("LAB_BACKUP_DIR", str(backups))
    assert main(["--db", str(db), "doctor"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[:7] == [
        "ok database", "ok operator_key", "ok model: not configured", "ok disk", "ok backup",
        "ok selftest", "ok audit_chain"]
    assert [line.split(":")[0] for line in lines[7:]] == [
        "skip versions", "skip lab_account", "ok permissions", "skip services"]
