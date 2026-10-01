"""The disposable container tier for untrusted code (issue #181, ADR 0007).

Most tests use a fake runtime that records every command line, so they run
on any host and prove what the executor asks for: no network, the workspace
as the only mount, a pinned image, and a removal on every way out. The tests
at the bottom run a hostile script in a real Apple container; they skip
unless the host is a Mac with the `container` CLI and a pinned image named
in ``LAB_CONTAINER_IMAGE``.
"""

from __future__ import annotations

import getpass
import os
import platform
import stat
import subprocess
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from lab import container
from lab.container import (
    AppleContainerRuntime,
    ContainerConfig,
    ContainerExecutor,
    ContainerRuntime,
    ContainerUnavailable,
    Outcome,
)

CLI = "/usr/local/bin/container"
DIGEST = "sha256:" + "ab" * 32
IMAGE = f"docker.io/library/alpine:3.22@{DIGEST}"
HOSTILE = """#!/bin/sh
# Each probe prints LEAK:<what> only if it got through.
wget -q -T 3 -O /dev/null http://1.1.1.1/ 2>/dev/null && echo LEAK:http
command -v nc >/dev/null && nc -z -w 3 1.1.1.1 443 2>/dev/null && echo LEAK:tcp
command -v curl >/dev/null && curl -s -m 3 -o /dev/null https://example.com && echo LEAK:curl
command -v python3 >/dev/null && python3 -c \
  "import socket; socket.create_connection(('1.1.1.1', 53), 3)" 2>/dev/null && echo LEAK:socket
nslookup example.com >/dev/null 2>&1 && echo LEAK:dns
ls /Users >/dev/null 2>&1 && echo LEAK:users
touch /etc/lab-escape 2>/dev/null && echo LEAK:rootfs-write
echo "MOUNTS:$(grep -c virtiofs /proc/mounts)"
grep virtiofs /proc/mounts
echo "PASSWD-BEGIN"; cat /etc/passwd; echo "PASSWD-END"
echo written > /work/proof.txt
echo DONE
"""


class FakeRuntime:
    """Records every CLI call. ``on_run`` decides what the run returns."""

    def __init__(self, *, cli: str | None = CLI,
                 on_run: Callable[[list[str], float, threading.Event | None], Outcome]
                 | None = None,
                 delete_rc: int = 0, list_out: bytes = b"", list_rc: int = 0) -> None:
        self._cli = cli
        self.on_run = on_run or (lambda argv, timeout, cancel: Outcome(0, b"ok\n"))
        self.delete_rc, self.list_out, self.list_rc = delete_rc, list_out, list_rc
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []

    def cli(self) -> str | None:
        return self._cli

    def execute(self, argv: Sequence[str], *, timeout: float,
                cancel: threading.Event | None = None) -> Outcome:
        argv = list(argv)
        self.calls.append(argv)
        self.timeouts.append(timeout)
        if argv[1] == "run":
            return self.on_run(argv, timeout, cancel)
        if argv[1] == "delete":
            return Outcome(self.delete_rc)
        return Outcome(self.list_rc, self.list_out)

    def verbs(self) -> list[str]:
        return [c[1] for c in self.calls]

    def run_argv(self) -> list[str]:
        return next(c for c in self.calls if c[1] == "run")


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    path = tmp_path / "workspaces"
    path.mkdir()
    return path


@pytest.fixture()
def workspace(root: Path) -> Path:
    ws = root / "task-t1-0001"
    ws.mkdir()
    (ws / "hostile.sh").write_text(HOSTILE)
    return ws


def executor(runtime: ContainerRuntime, root: Path, image: str = IMAGE,
             **limits: int) -> ContainerExecutor:
    return ContainerExecutor(runtime, root, ContainerConfig(image=image, **limits))


def options(argv: list[str]) -> tuple[list[tuple[str, str | None]], str, list[str]]:
    """Split a run command line into (option, value) pairs, image and command.

    Every option the executor uses takes a value except --read-only, so the
    image is the first word that is not an option or an option's value.
    """
    flags = {"--read-only"}
    pairs: list[tuple[str, str | None]] = []
    i = 2
    while argv[i].startswith("-"):
        if argv[i] in flags:
            pairs.append((argv[i], None))
            i += 1
        else:
            pairs.append((argv[i], argv[i + 1]))
            i += 2
    return pairs, argv[i], argv[i + 1:]


# ------------------------------------------------------- the command line


@pytest.mark.safety
def test_a_hostile_script_gets_no_network_and_no_other_mount(workspace, root) -> None:
    """The acceptance test on the fake: exactly the options we chose, and
    nothing that would give the guest the network or another host path."""
    runtime = FakeRuntime()
    executor(runtime, root).run(workspace, ["sh", "/work/hostile.sh"], task_id="t1")

    pairs, image, command = options(runtime.run_argv())
    names = [name for name, _ in pairs]
    assert ("--network", "none") in pairs
    assert names.count("--network") == 1
    volumes = [value for name, value in pairs if name in ("--volume", "-v")]
    assert volumes == [f"{workspace.resolve()}:/work"]
    assert set(names) <= {"--name", "--network", "--read-only", "--tmpfs", "--cpus",
                          "--memory", "--user", "--volume", "--workdir", "--env"}
    assert ("--user", "65534:65534") in pairs     # never root in the guest
    for forbidden in ("--mount", "-v", "--ssh", "--rosetta", "--publish", "-p",
                      "--dns", "--rm", "--detach", "-d", "--privileged"):
        assert forbidden not in names
    assert ("--tmpfs", "/tmp") in pairs           # memory-backed, not a host path
    assert ("--read-only", None) in pairs
    assert image == IMAGE
    assert command == ["sh", "/work/hostile.sh"]


@pytest.mark.safety
def test_network_none_is_always_present_whatever_the_caller_passes(workspace, root) -> None:
    """The tool's own default is network on (ADR 0007). No caller input
    reaches the option list, so nothing can turn it back on."""
    runtime = FakeRuntime()
    executor(runtime, root).run(workspace, ["--network", "default", "x"],
                                env={"LANG": "C.UTF-8"})
    pairs, _image, command = options(runtime.run_argv())
    assert [v for n, v in pairs if n == "--network"] == ["none"]
    assert command == ["--network", "default", "x"]   # after the image: the guest's argv


@pytest.mark.safety
@pytest.mark.parametrize("user", ["0:0", "0:1000", "1000:0", "root", "nobody", "1000",
                                  "1000:1000:1", "", None, 1000])
def test_the_guest_never_runs_as_root_or_a_named_user(workspace, root, user) -> None:
    runtime = FakeRuntime()
    with pytest.raises(ContainerUnavailable):
        ContainerExecutor(runtime, root, ContainerConfig(image=IMAGE, user=user)).run(
            workspace, ["true"])
    assert runtime.calls == []


def test_limits_and_workdir_are_on_the_command_line(workspace, root) -> None:
    runtime = FakeRuntime()
    executor(runtime, root, cpus=1, memory_mb=512).run(workspace, ["true"])
    pairs, _, _ = options(runtime.run_argv())
    assert ("--cpus", "1") in pairs and ("--memory", "512M") in pairs
    assert ("--workdir", "/work") in pairs


@pytest.mark.parametrize("limits", [{"cpus": 0}, {"cpus": 99}, {"memory_mb": 0},
                                    {"memory_mb": 4096}, {"cpus": True}])
def test_limits_beyond_the_ceiling_are_refused(workspace, root, limits) -> None:
    runtime = FakeRuntime()
    with pytest.raises(ContainerUnavailable):
        executor(runtime, root, **limits).run(workspace, ["true"])
    assert runtime.calls == []


@pytest.mark.safety
def test_only_allowlisted_environment_reaches_the_guest(workspace, root, monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    runtime = FakeRuntime()
    executor(runtime, root).run(workspace, ["env"], env={"TZ": "UTC"})
    pairs, _, _ = options(runtime.run_argv())
    env = dict(v.split("=", 1) for n, v in pairs if n == "--env" and v)
    assert set(env) == {"TZ", "HOME", "TMPDIR", "PATH"}
    assert env["HOME"] == "/work" and env["TZ"] == "UTC"
    assert "ghp_secret" not in " ".join(runtime.run_argv())


@pytest.mark.safety
@pytest.mark.parametrize("env", [{"GITHUB_TOKEN": "x"}, {"AWS_SECRET_ACCESS_KEY": "x"},
                                 {"LANG": "C\nX=1"}, {"lang": "C"}, {"HOME": "/"}])
def test_other_environment_is_refused_not_dropped(workspace, root, env) -> None:
    runtime = FakeRuntime()
    with pytest.raises(ContainerUnavailable, match="environment"):
        executor(runtime, root).run(workspace, ["env"], env=env)
    assert runtime.calls == []


def test_a_fixed_value_cannot_be_overridden(workspace, root) -> None:
    """HOME and PATH are fixed by the executor, and are not on the allowlist."""
    assert not {"HOME", "PATH", "TMPDIR"} & container.ENV_ALLOWLIST


# ------------------------------------------------------- fail closed


@pytest.mark.safety
@pytest.mark.parametrize("image", [
    "alpine:3.22", "alpine", "alpine:latest", "alpine@sha256:abc",
    "-v/:/host@" + DIGEST, "alpine@sha256:" + "AB" * 32, "",
])
def test_an_image_not_pinned_by_digest_is_refused(workspace, root, image) -> None:
    runtime = FakeRuntime()
    with pytest.raises(ContainerUnavailable, match="pinned"):
        executor(runtime, root, image=image).run(workspace, ["true"])
    assert runtime.calls == []


@pytest.mark.parametrize("image", [IMAGE, f"alpine@{DIGEST}",
                                   f"registry.local:5000/team/py:3.13-slim@{DIGEST}"])
def test_pinned_images_are_accepted(image) -> None:
    assert container.check_image(image) == image


@pytest.mark.safety
def test_a_workspace_outside_the_root_is_refused(tmp_path, root) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    runtime = FakeRuntime()
    for target in (outside, root, root / "..", Path("/")):
        with pytest.raises(ContainerUnavailable, match="not inside"):
            executor(runtime, root).run(target, ["true"])
    assert runtime.calls == []


@pytest.mark.safety
def test_a_symlinked_workspace_is_refused(tmp_path, root, workspace) -> None:
    """Even one that points inside the root: the mount must be the directory
    the broker made, not whatever a link points at."""
    runtime = FakeRuntime()
    inside = root / "task-link"
    inside.symlink_to(workspace)
    escape = root / "task-escape"
    escape.symlink_to(tmp_path)
    for link in (inside, escape):
        with pytest.raises(ContainerUnavailable, match="symlink"):
            executor(runtime, root).run(link, ["true"])
    assert runtime.calls == []


def test_a_workspace_under_a_symlinked_directory_is_refused(tmp_path, root) -> None:
    """A link in a parent component resolves outside the root."""
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "ws").mkdir(parents=True)
    (root / "hop").symlink_to(elsewhere)
    with pytest.raises(ContainerUnavailable, match="not inside"):
        executor(FakeRuntime(), root).run(root / "hop" / "ws", ["true"])


def test_a_missing_workspace_is_refused(root) -> None:
    with pytest.raises(ContainerUnavailable, match="does not exist"):
        executor(FakeRuntime(), root).run(root / "nope", ["true"])


@pytest.mark.parametrize("name", ["a:b", "a,b"])
def test_a_path_that_could_alter_the_mount_is_refused(root, name) -> None:
    ws = root / name
    ws.mkdir()
    with pytest.raises(ContainerUnavailable, match="mount"):
        executor(FakeRuntime(), root).run(ws, ["true"])


@pytest.mark.safety
def test_a_missing_runtime_is_refused(workspace, root) -> None:
    """No container means no run, never a fallback to Seatbelt or the host."""
    runtime = FakeRuntime(cli=None)
    with pytest.raises(ContainerUnavailable, match="no container runtime"):
        executor(runtime, root).run(workspace, ["true"])
    assert runtime.calls == []


@pytest.mark.parametrize("command", [[], "sh -c 'curl x'", ["a\0b"], [1]])
def test_a_malformed_command_is_refused(workspace, root, command) -> None:
    runtime = FakeRuntime()
    with pytest.raises(ContainerUnavailable, match="command"):
        executor(runtime, root).run(workspace, command)
    assert runtime.calls == []


def test_bad_names_are_refused_by_the_builders(workspace) -> None:
    config = ContainerConfig(image=IMAGE)
    with pytest.raises(ContainerUnavailable):
        container.build_run_argv(CLI, "--rm", workspace, ["true"], config)
    with pytest.raises(ContainerUnavailable):
        container.build_remove_argv(CLI, "-a")


def test_names_are_unique_and_safe() -> None:
    names = {container.container_name("Task 7/../x") for _ in range(50)}
    assert len(names) == 50
    assert all(n.startswith("lab-task-7-x-") for n in names)
    assert container.container_name("!!!").startswith("lab-task-")


# ------------------------------------------- removal on every path (#223)


@pytest.mark.safety
def test_the_container_is_removed_after_a_normal_run(workspace, root) -> None:
    runtime = FakeRuntime()
    result = executor(runtime, root).run(workspace, ["true"], task_id="t1")
    assert result.ok and result.removed and result.stdout == "ok\n"
    assert runtime.verbs() == ["run", "delete"]
    assert runtime.calls[1] == [CLI, "delete", "--force", result.name]
    assert ["--name", result.name] == runtime.calls[0][2:4]


@pytest.mark.safety
def test_the_container_is_removed_after_a_failing_command(workspace, root) -> None:
    runtime = FakeRuntime(on_run=lambda a, t, c: Outcome(3, b"", b"boom"))
    result = executor(runtime, root).run(workspace, ["false"])
    assert not result.ok and result.returncode == 3 and result.stderr == "boom"
    assert result.removed and runtime.verbs() == ["run", "delete"]


@pytest.mark.safety
def test_the_container_is_removed_after_a_timeout(workspace, root) -> None:
    """Killing the CLI client does not stop the guest; the forced delete does."""
    runtime = FakeRuntime(on_run=lambda a, t, c: Outcome(-9, b"partial", timed_out=True))
    result = executor(runtime, root).run(workspace, ["sleep", "999"], timeout=10_000)
    assert result.timed_out and not result.ok and result.returncode == -1
    assert result.stderr == "timed out" and result.stdout == "partial"
    assert result.removed and runtime.verbs() == ["run", "delete"]
    assert runtime.timeouts[0] == container.MAX_TIMEOUT_SECONDS     # clamped


@pytest.mark.safety
def test_the_container_is_removed_when_the_run_raises(workspace, root) -> None:
    def explode(argv, timeout, cancel):
        raise OSError("the CLI vanished mid-run")
    runtime = FakeRuntime(on_run=explode)
    with pytest.raises(OSError, match="vanished"):
        executor(runtime, root).run(workspace, ["true"])
    assert runtime.verbs() == ["run", "delete"]


@pytest.mark.safety
def test_the_container_is_removed_after_a_stop(workspace, root) -> None:
    """A stop sets the cancel flag from another thread (the broker's revoke or
    cancel_running); the run ends and the container is deleted."""
    started = threading.Event()

    def wait_for_stop(argv, timeout, cancel):
        started.set()
        assert cancel is not None and cancel.wait(5)
        return Outcome(-9, cancelled=True)

    runtime = FakeRuntime(on_run=wait_for_stop)
    cancel = threading.Event()
    results = []
    worker = threading.Thread(target=lambda: results.append(
        executor(runtime, root).run(workspace, ["sleep", "999"], cancel=cancel)))
    worker.start()
    assert started.wait(5)
    cancel.set()
    worker.join(5)
    (result,) = results
    assert result.cancelled and not result.ok and result.stderr == "cancelled"
    assert result.removed and runtime.verbs() == ["run", "delete"]


@pytest.mark.safety
def test_a_failed_removal_is_reported_not_hidden(workspace, root, caplog) -> None:
    runtime = FakeRuntime(delete_rc=1)
    runtime.list_out = b""     # set below once the name is known

    def still_there(argv, timeout, cancel):
        runtime.list_out = argv[3].encode() + b"\n"
        return Outcome(0, b"fine")

    runtime.on_run = still_there
    result = executor(runtime, root).run(workspace, ["true"])
    assert not result.ok and not result.removed
    assert "was not removed" in result.stderr
    assert runtime.verbs() == ["run", "delete", "delete", "list"]
    assert "could not be removed" in caplog.text


def test_a_container_that_never_started_counts_as_removed(workspace, root) -> None:
    """The delete fails because there is nothing to delete; the listing proves it."""
    runtime = FakeRuntime(on_run=lambda a, t, c: Outcome(1, b"", b"no such image"),
                          delete_rc=1, list_out=b"lab-other-123\n")
    result = executor(runtime, root).run(workspace, ["true"])
    assert result.removed and not result.ok
    assert runtime.verbs() == ["run", "delete", "delete", "list"]


def test_an_unreadable_listing_counts_as_not_removed(workspace, root) -> None:
    runtime = FakeRuntime(delete_rc=1, list_rc=1)
    result = executor(runtime, root).run(workspace, ["true"])
    assert not result.removed and not result.ok


def test_a_removal_that_raises_does_not_mask_the_result(workspace, root) -> None:
    class Flaky(FakeRuntime):
        def execute(self, argv, *, timeout, cancel=None):
            if list(argv)[1] == "delete" and self.verbs().count("delete") == 0:
                self.calls.append(list(argv))
                raise OSError("transient")
            return super().execute(argv, timeout=timeout, cancel=cancel)

    runtime = Flaky()
    result = executor(runtime, root).run(workspace, ["true"])
    assert result.ok and result.removed
    assert runtime.verbs() == ["run", "delete", "delete"]


# ------------------------------------- the real runtime against a stand-in CLI


@pytest.fixture()
def stand_in_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A shell script in place of `container`, so the real runtime's process
    handling (argument list, timeout, cancel) is exercised on any host."""
    log = tmp_path / "cli.log"
    script = tmp_path / "container"
    script.write_text(f"""#!/bin/sh
echo "$*" >> {log}
case "$1" in
  run) for a in "$@"; do [ "$a" = "hang" ] && exec sleep 30; done; echo "guest says hi" ;;
  list) echo "" ;;
esac
""")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(container.platform, "system", lambda: "Darwin")
    return script, log


def test_the_real_runtime_runs_and_removes_through_an_argument_list(
        stand_in_cli, workspace, root) -> None:
    script, log = stand_in_cli
    runtime = AppleContainerRuntime(str(script))
    assert runtime.cli() == str(script)
    result = executor(runtime, root).run(workspace, ["sh", "-c", "echo $HOME; curl x"])
    assert result.ok and result.removed and result.stdout == "guest says hi\n"
    lines = log.read_text().splitlines()
    assert lines[0].startswith("run --name lab-") and "--network none" in lines[0]
    assert lines[1] == f"delete --force {result.name}"


@pytest.mark.safety
def test_the_real_runtime_removes_after_a_timeout(stand_in_cli, workspace, root) -> None:
    script, log = stand_in_cli
    runtime = AppleContainerRuntime(str(script))
    result = executor(runtime, root).run(workspace, ["hang"], timeout=0.5)
    assert result.timed_out and result.removed
    assert log.read_text().splitlines()[-1] == f"delete --force {result.name}"


def test_the_real_runtime_removes_after_a_stop(stand_in_cli, workspace, root) -> None:
    script, log = stand_in_cli
    cancel = threading.Event()
    timer = threading.Timer(0.3, cancel.set)
    timer.start()
    result = executor(AppleContainerRuntime(str(script)), root).run(
        workspace, ["hang"], timeout=20, cancel=cancel)
    timer.cancel()
    assert result.cancelled and result.removed
    assert log.read_text().splitlines()[-1] == f"delete --force {result.name}"


def test_the_real_runtime_is_absent_off_macos(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(container.platform, "system", lambda: "Linux")
    assert AppleContainerRuntime("/bin/sh").cli() is None


def test_the_real_runtime_needs_an_absolute_executable(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(container.platform, "system", lambda: "Darwin")
    assert AppleContainerRuntime("container").cli() is None
    plain = tmp_path / "container"
    plain.write_text("")
    assert AppleContainerRuntime(str(plain)).cli() is None
    monkeypatch.setattr(container.shutil, "which", lambda name: None)
    monkeypatch.setattr(container, "DEFAULT_CLI", str(tmp_path / "missing"))
    assert AppleContainerRuntime().cli() is None


# --------------------------------------------- on the Mac, with a real guest


def _real_runtime() -> AppleContainerRuntime | None:
    runtime = AppleContainerRuntime()
    return runtime if runtime.cli() else None


REAL_IMAGE = os.environ.get("LAB_CONTAINER_IMAGE", "")
needs_container = pytest.mark.skipif(
    platform.system() != "Darwin" or _real_runtime() is None or not REAL_IMAGE,
    reason="needs macOS, Apple's container CLI and LAB_CONTAINER_IMAGE pinned by digest",
)


def _listed(cli: str) -> str:
    return subprocess.run([cli, "list", "--all", "--quiet"], capture_output=True,
                          text=True, check=True, timeout=60).stdout


@needs_container
@pytest.mark.safety
def test_a_hostile_script_cannot_reach_the_network_or_the_host(workspace, root) -> None:
    runtime = AppleContainerRuntime()
    result = ContainerExecutor(runtime, root, ContainerConfig(image=REAL_IMAGE)).run(
        workspace, ["sh", "/work/hostile.sh"], timeout=120)
    assert result.removed, result.stderr
    assert "DONE" in result.stdout, result.stdout + result.stderr
    assert "LEAK:" not in result.stdout, result.stdout
    assert "MOUNTS:1" in result.stdout, result.stdout
    assert any(" /work " in line for line in result.stdout.splitlines()
               if "virtiofs" in line and not line.startswith("MOUNTS"))
    passwd = result.stdout.split("PASSWD-BEGIN", 1)[1].split("PASSWD-END", 1)[0]
    user = getpass.getuser()
    if user not in ("root", "nobody"):
        assert user not in passwd, "the host's accounts are visible in the guest"
    assert (workspace / "proof.txt").read_text() == "written\n"
    cli = runtime.cli()
    assert cli is not None and result.name not in _listed(cli).split()


@needs_container
@pytest.mark.safety
def test_a_setsid_survivor_ends_with_the_container(workspace, root) -> None:
    """#223 for untrusted code: a process that left its group is still inside
    the VM, and removing the container ends it."""
    (workspace / "detach.sh").write_text(
        "#!/bin/sh\n(setsid sh -c 'sleep 300' &) ; echo detached\n")
    runtime = AppleContainerRuntime()
    result = ContainerExecutor(runtime, root, ContainerConfig(image=REAL_IMAGE)).run(
        workspace, ["sh", "/work/detach.sh"], timeout=60)
    assert "detached" in result.stdout and result.removed
    cli = runtime.cli()
    assert cli is not None and result.name not in _listed(cli).split()
