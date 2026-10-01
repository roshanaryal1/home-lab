"""An active skill's scripts run only in the container, through the broker (#255).

``skill.run`` takes a skill name and a script path. It resolves the active
version only, refuses everything else before anything starts, installs the
verified version into a fresh directory in the task's workspace and runs the
script through ``ContainerExecutor``. Most tests use a fake runtime that
records every command line. The test at the bottom runs a real container and
skips unless the host is a Mac with the `container` CLI and a pinned image in
``LAB_CONTAINER_IMAGE``.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import stat
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from lab import container as containers
from lab import operator as op
from lab import toolcorpus
from lab.artifacts import ArtifactStore
from lab.authority import TOOL_LEGS, AgentCapability, AuthorityViolation, Leg, check, held_legs
from lab.broker import (
    MAX_SKILL_ARGS,
    MAX_SKILL_OUTPUT_CHARS,
    NON_IDEMPOTENT,
    SKILL_RUN_PREFIX,
    TOOL_EFFECTS,
    TOOL_SCHEMAS,
    TOOL_TIERS,
    ApprovalRequired,
    ExecutionBroker,
    ExecutionContext,
    ToolResult,
    ToolSession,
)
from lab.container import (
    AppleContainerRuntime,
    ContainerConfig,
    ContainerExecutor,
    ContainerUnavailable,
    Outcome,
)
from lab.handlers import CONTAINER_IMAGE_ENV, configure_skill_runner
from lab.journal import OperationJournal
from lab.origin import Origin, SourceType
from lab.policy import PolicyEngine, Tier
from lab.queue import TaskQueue
from lab.skillstore import SkillStore, SkillStoreError
from lab.supervisor import Supervisor, SupervisorConfig

CLI = "/usr/local/bin/container"
DIGEST = "sha256:" + "cd" * 32
IMAGE = f"docker.io/library/alpine:3.22@{DIGEST}"
SCRIPT = "#!/bin/sh\necho \"args: $*\"\necho ran > /work/out.txt\n"
OnRun = Callable[[list[str], float, threading.Event | None], Outcome]


class FakeRuntime:
    """Records every CLI call. ``on_run`` decides what the run returns."""

    def __init__(self, *, cli: str | None = CLI, on_run: OnRun | None = None) -> None:
        self._cli = cli
        self.on_run = on_run or (lambda argv, timeout, cancel: Outcome(0, b"ok\n"))
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
        return Outcome(0)

    def verbs(self) -> list[str]:
        return [c[1] for c in self.calls]

    def run_argv(self) -> list[str]:
        return next(c for c in self.calls if c[1] == "run")


def make_skill(root: Path, body: str = "v1", *, scripts: dict[str, str] | None = None,
               notes: bool = True) -> Path:
    directory = root / "summarise"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: summarise\ndescription: Summarises documents\n---\n{body}\n")
    if notes:
        (directory / "notes.txt").write_text("not a program\n")
    for rel, text in (scripts if scripts is not None else {"scripts/run.sh": SCRIPT}).items():
        path = directory / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return directory


class Lab:
    """A queue, a skill store, a broker with a fake container, one leased task."""

    def __init__(self, tmp_path: Path, *, runtime: Any = None,
                 configured: bool = True, public_key: Any = None,
                 image: str = IMAGE) -> None:
        self.tmp = tmp_path
        self.q = TaskQueue(tmp_path / "lab.db")
        self.artifacts = ArtifactStore(tmp_path / "artifacts", self.q._conn)
        self.store = SkillStore(self.q._conn, self.artifacts)
        self.runtime = runtime or FakeRuntime()
        self.root = tmp_path / "workspaces"
        self.root.mkdir()
        self.policy = PolicyEngine(self.q._conn, public_key)
        self.journal = OperationJournal(self.q._conn)
        executor = ContainerExecutor(self.runtime, self.root, ContainerConfig(image=image))
        self.broker = ExecutionBroker(
            self.root, policy=self.policy, leases=self.q.owns_lease, journal=self.journal,
            skills=self.store if configured else None,
            container=executor if configured else None)
        self.task_id = self.q.add_task("run a skill", origin=Origin(SourceType.OPERATOR))
        task = self.q.lease()
        assert task is not None and task.lease is not None
        self.ws = self.broker.open_workspace(self.task_id, {"skill.run"})
        self.session = self.broker.session(
            ExecutionContext(self.task_id, "test", task.attempts, task.lease))

    def submit(self, version_body: str = "v1", **kw: Any) -> int:
        vid = self.store.submit(make_skill(self.tmp / "src", version_body, **kw),
                                "approve", "learner")
        return vid

    def active(self, version_body: str = "v1", **kw: Any) -> int:
        vid = self.submit(version_body, **kw)
        self.store.promote(vid, "roshan")
        return vid

    def approvals(self) -> int:
        return int(self.q._conn.execute("SELECT count(*) FROM approvals").fetchone()[0])

    def tainted(self) -> bool:
        return bool(self.q._conn.execute("SELECT tainted FROM tasks WHERE id = ?",
                                         (self.task_id,)).fetchone()[0])

    def run_dirs(self) -> list[str]:
        return [p.name for p in self.ws.root.iterdir() if p.name.startswith(SKILL_RUN_PREFIX)]


@pytest.fixture()
def lab(tmp_path: Path):
    world = Lab(tmp_path)
    yield world
    world.q.close()


def approved(session: ToolSession, policy: PolicyEngine, **params: Any) -> ToolResult:
    """Ask once, have a person grant that exact call, then make it."""
    with pytest.raises(ApprovalRequired) as asked:
        session.submit("skill.run", **params)
    policy.grant(asked.value.approval_id, decided_by="roshan")
    return session.submit("skill.run", **params)


def options(argv: list[str]) -> tuple[list[tuple[str, str | None]], str, list[str]]:
    """Split a run command line into (option, value) pairs, image and command."""
    pairs: list[tuple[str, str | None]] = []
    i = 2
    while argv[i].startswith("-"):
        if argv[i] == "--read-only":
            pairs.append((argv[i], None))
            i += 1
        else:
            pairs.append((argv[i], argv[i + 1]))
            i += 2
    return pairs, argv[i], argv[i + 1:]


def assert_refused(lab: Lab, result: ToolResult, match: str) -> None:
    """Refused before anything started: no approval asked, no CLI call, no install."""
    assert not result.ok and result.error is not None and match in result.error, result.error
    assert lab.runtime.calls == []
    assert lab.approvals() == 0
    assert lab.run_dirs() == []


# ------------------------------------------------------------- the refusals


@pytest.mark.safety
def test_skill_run_is_off_unless_a_container_is_configured(tmp_path: Path) -> None:
    world = Lab(tmp_path, configured=False)
    world.active()
    result = world.session.submit("skill.run", skill="summarise", script="scripts/run.sh")
    assert_refused(world, result, "skill.run is off")
    assert "LAB_CONTAINER_IMAGE" in (result.error or "")
    world.q.close()


@pytest.mark.safety
def test_a_candidate_version_never_runs(lab: Lab) -> None:
    lab.submit()
    result = lab.session.submit("skill.run", skill="summarise", script="scripts/run.sh")
    assert_refused(lab, result, "no active version")


@pytest.mark.safety
def test_a_rejected_version_never_runs(lab: Lab) -> None:
    vid = lab.submit()
    lab.store.reject(vid, "roshan", "no")
    result = lab.session.submit("skill.run", skill="summarise", script="scripts/run.sh")
    assert_refused(lab, result, "no active version")


@pytest.mark.safety
def test_a_rolled_back_version_never_runs(lab: Lab) -> None:
    """v2 adds a script. After a rollback to v1 only v1 counts, so v2's script
    is refused even though its files are still in the store."""
    v1 = lab.active("one")
    lab.store.mark_known_good(v1, "roshan", "test run 1")
    lab.active("two", scripts={"scripts/run.sh": SCRIPT, "scripts/new.sh": SCRIPT})
    lab.store.rollback("summarise", "roshan")
    assert lab.store.active("summarise")["id"] == v1
    result = lab.session.submit("skill.run", skill="summarise", script="scripts/new.sh")
    assert_refused(lab, result, "is not a file of summarise v1")


@pytest.mark.safety
def test_a_script_not_in_the_manifest_is_refused(lab: Lab) -> None:
    lab.active()
    (lab.ws.root / "planted.sh").write_text(SCRIPT)
    for script in ("scripts/other.sh", "planted.sh"):
        result = lab.session.submit("skill.run", skill="summarise", script=script)
        assert_refused(lab, result, "is not a file of summarise v1")


@pytest.mark.safety
def test_a_file_that_is_not_executable_in_the_version_is_refused(lab: Lab) -> None:
    lab.active()
    for script in ("notes.txt", "SKILL.md"):
        result = lab.session.submit("skill.run", skill="summarise", script=script)
        assert_refused(lab, result, "is not executable in summarise v1")


@pytest.mark.safety
@pytest.mark.parametrize("script", ["../summarise/scripts/run.sh", "/work/scripts/run.sh",
                                    "scripts/../../x", "", ".", "scripts\\run.sh",
                                    "/etc/passwd"])
def test_a_script_path_that_escapes_is_refused(lab: Lab, script: str) -> None:
    lab.active()
    result = lab.session.submit("skill.run", skill="summarise", script=script)
    assert_refused(lab, result, "PathEscape")


@pytest.mark.safety
@pytest.mark.parametrize("name", ["../summarise", "Summarise", "a/b", "x" * 65])
def test_a_name_that_is_not_a_skill_name_is_refused(lab: Lab, name: str) -> None:
    lab.active()
    result = lab.session.submit("skill.run", skill=name, script="scripts/run.sh")
    assert_refused(lab, result, "is not a skill name")


@pytest.mark.safety
def test_a_version_whose_stored_files_fail_verification_is_refused(lab: Lab) -> None:
    vid = lab.active()
    sha = json.loads(lab.store.get(vid)["manifest"])["scripts/run.sh"][0]
    blob = lab.artifacts.blob_path(sha)
    os.chmod(blob, 0o600)
    blob.write_bytes(b"#!/bin/sh\ncurl https://evil.example\n")
    result = lab.session.submit("skill.run", skill="summarise", script="scripts/run.sh")
    assert_refused(lab, result, "no longer verify")


@pytest.mark.safety
def test_no_container_runtime_means_refuse_never_a_fallback(tmp_path: Path) -> None:
    world = Lab(tmp_path, runtime=FakeRuntime(cli=None))
    world.active()
    result = world.session.submit("skill.run", skill="summarise", script="scripts/run.sh")
    assert_refused(world, result, "no container runtime")
    world.q.close()


@pytest.mark.safety
def test_an_image_not_pinned_by_digest_is_refused(tmp_path: Path) -> None:
    world = Lab(tmp_path, image="docker.io/library/alpine:3.22")
    world.active()
    result = world.session.submit("skill.run", skill="summarise", script="scripts/run.sh")
    assert_refused(world, result, "pinned by sha256 digest")
    world.q.close()


def test_a_skill_with_tier_never_does_not_run(lab: Lab) -> None:
    vid = lab.store.submit(make_skill(lab.tmp / "src"), "never", "learner")
    lab.store.promote(vid, "roshan")
    result = lab.session.submit("skill.run", skill="summarise", script="scripts/run.sh")
    assert_refused(lab, result, "has tier never")


def test_arguments_are_bounded(lab: Lab) -> None:
    lab.active()
    for args in (["a"] * (MAX_SKILL_ARGS + 1), ["x" * 5000]):
        result = lab.session.submit("skill.run", skill="summarise", script="scripts/run.sh",
                                    args=args)
        assert_refused(lab, result, "InvalidParams")
    result = lab.session.submit("skill.run", skill="summarise", script="scripts/run.sh",
                                args=["ok", 3])
    assert_refused(lab, result, "InvalidParams")


@pytest.mark.safety
def test_a_container_check_that_fails_at_run_time_still_refuses(tmp_path: Path) -> None:
    """The executor's own checks are the last word: here its workspace root is
    not the broker's, so it refuses the mount and nothing is started."""
    world = Lab(tmp_path)
    world.active()
    other = tmp_path / "elsewhere"
    other.mkdir()
    world.broker.set_skill_runner(world.store, ContainerExecutor(
        world.runtime, other, ContainerConfig(image=IMAGE)))
    result = approved(world.session, world.policy, skill="summarise", script="scripts/run.sh")
    assert not result.ok and "refusing to run without a container" in (result.error or "")
    assert "run" not in world.runtime.verbs()
    assert world.run_dirs() == []
    world.q.close()


def test_an_install_that_fails_is_refused_and_cleaned_up(lab: Lab, monkeypatch) -> None:
    lab.active()

    def broken(name: str, dest: Path) -> Any:
        (dest / "partial").write_text("x")
        raise SkillStoreError("installed files do not match the recorded content hash")

    monkeypatch.setattr(lab.store, "install", broken)
    result = approved(lab.session, lab.policy, skill="summarise", script="scripts/run.sh")
    assert not result.ok and "did not install" in (result.error or "")
    assert lab.runtime.calls == [] and lab.run_dirs() == []


# ----------------------------------------------------------- a run, and after


@pytest.mark.safety
def test_a_run_uses_the_pinned_image_no_network_and_the_workspace_mount(lab: Lab) -> None:
    lab.active()
    seen: dict[str, Any] = {}

    def on_run(argv: list[str], timeout: float, cancel: threading.Event | None) -> Outcome:
        _, _, command = options(argv)
        host = lab.ws.root / command[0].removeprefix("/work/")
        seen["script"] = host.read_text()
        seen["executable"] = os.access(host, os.X_OK)
        seen["cancel"] = cancel
        return Outcome(0, b"args: a b\n")

    lab.runtime.on_run = on_run
    result = approved(lab.session, lab.policy, skill="summarise", script="scripts/run.sh",
                      args=["a", "b"])
    assert result.ok, result.error
    pairs, image, command = options(lab.runtime.run_argv())
    assert image == IMAGE and image.endswith(DIGEST)
    assert ("--network", "none") in pairs
    volumes = [value for name, value in pairs if name == "--volume"]
    assert volumes == [f"{lab.ws.root.resolve()}:/work"]
    assert command[0].startswith(f"/work/{SKILL_RUN_PREFIX}")
    assert command[0].endswith("/summarise/scripts/run.sh") and command[1:] == ["a", "b"]
    assert seen["script"] == SCRIPT and seen["executable"], "the verified version ran"
    assert isinstance(seen["cancel"], threading.Event)
    assert lab.runtime.verbs() == ["run", "delete"]
    assert lab.run_dirs() == [], "the installed copy is removed after the run"
    assert result.detail["version"] == 1 and result.detail["removed"] is True
    assert result.detail["content_sha256"] == lab.store.active("summarise")["content_sha256"]


@pytest.mark.safety
def test_output_is_untrusted_cleaned_and_capped(lab: Lab) -> None:
    lab.active()
    big = "\x1b[31mred\x1b[0m ‮".encode() + b"y" * (MAX_SKILL_OUTPUT_CHARS + 10)
    lab.runtime.on_run = lambda argv, timeout, cancel: Outcome(0, big, b"warn\x07\n")
    assert not lab.tainted()
    result = approved(lab.session, lab.policy, skill="summarise", script="scripts/run.sh")
    assert result.detail["untrusted"] is True
    assert lab.tainted(), "the task holds untrusted input once it reads the output"
    out = result.detail["stdout"]
    assert "\x1b" not in out and "‮" not in out and out.startswith("[31mred")
    assert len(out) == MAX_SKILL_OUTPUT_CHARS and result.detail["truncated"] is True
    assert result.detail["stderr"] == "warn\n"


@pytest.mark.safety
def test_the_container_is_removed_after_a_failing_script(lab: Lab) -> None:
    lab.active()
    lab.runtime.on_run = lambda argv, timeout, cancel: Outcome(3, b"", b"boom\n")
    result = approved(lab.session, lab.policy, skill="summarise", script="scripts/run.sh")
    assert not result.ok and result.error == "boom" and result.detail["returncode"] == 3
    assert lab.runtime.verbs() == ["run", "delete"] and lab.run_dirs() == []


@pytest.mark.safety
def test_the_container_is_removed_after_a_timeout_and_the_timeout_is_clamped(
        lab: Lab) -> None:
    lab.active()
    lab.runtime.on_run = lambda argv, timeout, cancel: Outcome(-9, timed_out=True)
    result = approved(lab.session, lab.policy, skill="summarise", script="scripts/run.sh",
                      timeout=10_000)
    assert not result.ok and result.detail["timed_out"] is True
    assert lab.runtime.timeouts[0] == containers.MAX_TIMEOUT_SECONDS
    assert lab.runtime.verbs() == ["run", "delete"] and lab.run_dirs() == []


@pytest.mark.safety
def test_the_container_is_removed_when_the_runtime_raises(lab: Lab) -> None:
    lab.active()

    def explode(argv: list[str], timeout: float, cancel: threading.Event | None) -> Outcome:
        raise RuntimeError("the CLI crashed")

    lab.runtime.on_run = explode
    with pytest.raises(ApprovalRequired) as asked:
        lab.session.submit("skill.run", skill="summarise", script="scripts/run.sh")
    lab.policy.grant(asked.value.approval_id, decided_by="roshan")
    with pytest.raises(RuntimeError, match="the CLI crashed"):
        lab.session.submit("skill.run", skill="summarise", script="scripts/run.sh")
    assert lab.runtime.verbs() == ["run", "delete"] and lab.run_dirs() == []
    states = [r[0] for r in lab.q._conn.execute("SELECT state FROM operations")]
    assert states == ["uncertain"], "an unknown outcome is held, never replayed"


def _waiting_runtime() -> tuple[FakeRuntime, threading.Event]:
    started = threading.Event()

    def on_run(argv: list[str], timeout: float, cancel: threading.Event | None) -> Outcome:
        started.set()
        assert cancel is not None
        if cancel.wait(5):
            return Outcome(-15, cancelled=True)
        return Outcome(0, b"never stopped\n")

    return FakeRuntime(on_run=on_run), started


async def _wait_for(event: threading.Event) -> None:
    for _ in range(500):
        if event.is_set():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the script never started")


@pytest.mark.safety
@pytest.mark.asyncio
async def test_an_emergency_stop_reaches_a_running_script(tmp_path: Path) -> None:
    runtime, started = _waiting_runtime()
    world = Lab(tmp_path, runtime=runtime)
    world.active()
    params = {"skill": "summarise", "script": "scripts/run.sh"}
    with pytest.raises(ApprovalRequired) as asked:
        await world.session.submit_async("skill.run", **params)
    world.policy.grant(asked.value.approval_id, decided_by="roshan")
    call = asyncio.ensure_future(world.session.submit_async("skill.run", **params))
    await _wait_for(started)
    world.broker.revoke()
    result = await asyncio.wait_for(call, timeout=5)
    assert not result.ok and result.detail["cancelled"] is True
    assert runtime.verbs() == ["run", "delete"] and world.run_dirs() == []
    assert world.broker._shell_cancels == {}
    world.q.close()


@pytest.mark.safety
@pytest.mark.asyncio
async def test_cancelling_the_task_reaches_a_running_script(tmp_path: Path) -> None:
    runtime, started = _waiting_runtime()
    world = Lab(tmp_path, runtime=runtime)
    world.active()
    params = {"skill": "summarise", "script": "scripts/run.sh"}
    with pytest.raises(ApprovalRequired) as asked:
        await world.session.submit_async("skill.run", **params)
    world.policy.grant(asked.value.approval_id, decided_by="roshan")
    call = asyncio.ensure_future(world.session.submit_async("skill.run", **params))
    await _wait_for(started)
    world.broker.cancel_running(world.task_id)
    result = await asyncio.wait_for(call, timeout=5)
    assert result.detail["cancelled"] is True and runtime.verbs() == ["run", "delete"]
    assert world.tainted()
    world.q.close()


def test_a_script_that_replaces_its_directory_with_a_link_cannot_redirect_cleanup(
        lab: Lab, tmp_path: Path) -> None:
    """The guest can write /work. Swapping the install directory for a symlink
    to somewhere outside must remove only the link."""
    lab.active()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")

    def swap(argv: list[str], timeout: float, cancel: threading.Event | None) -> Outcome:
        _, _, command = options(argv)
        run_dir = lab.ws.root / command[0].removeprefix("/work/").split("/", 1)[0]
        (lab.ws.root / "moved").mkdir()
        run_dir.rename(lab.ws.root / "moved" / "x")
        run_dir.symlink_to(outside)
        return Outcome(0)

    lab.runtime.on_run = swap
    assert approved(lab.session, lab.policy, skill="summarise", script="scripts/run.sh").ok
    assert (outside / "keep.txt").read_text() == "keep"
    assert lab.run_dirs() == []


# ------------------------------------------------- approval, journal, corpus


@pytest.mark.safety
def test_an_approve_tier_call_parks_for_a_signed_approval(tmp_path: Path) -> None:
    private, public = op.generate(tmp_path / "keys")
    world = Lab(tmp_path, public_key=op.load_public(public))
    world.active()
    params = {"skill": "summarise", "script": "scripts/run.sh"}
    with pytest.raises(ApprovalRequired) as asked:
        world.session.submit("skill.run", **params)
    assert world.runtime.calls == [] and world.run_dirs() == []
    # Granted without the operator key: refused at the gate, still parked.
    world.policy.grant(asked.value.approval_id, decided_by="roshan")
    with pytest.raises(ApprovalRequired) as again:
        world.session.submit("skill.run", **params)
    assert world.runtime.calls == []
    world.policy.grant(again.value.approval_id, decided_by="roshan",
                       signer=op.load_private(private))
    result = world.session.submit("skill.run", **params)
    assert result.ok and world.runtime.verbs() == ["run", "delete"]
    world.q.close()


@pytest.mark.safety
def test_a_grant_is_for_the_version_that_was_reviewed(lab: Lab) -> None:
    """A promotion between the review and the run voids the grant."""
    lab.active("one")
    params = {"skill": "summarise", "script": "scripts/run.sh"}
    with pytest.raises(ApprovalRequired) as asked:
        lab.session.submit("skill.run", **params)
    lab.policy.grant(asked.value.approval_id, decided_by="roshan")
    v2 = lab.submit("two")
    lab.store.promote(v2, "roshan")
    with pytest.raises(ApprovalRequired):
        lab.session.submit("skill.run", **params)
    assert lab.runtime.calls == []


def test_a_run_is_journaled_as_non_idempotent(lab: Lab) -> None:
    assert TOOL_TIERS["skill.run"] is Tier.APPROVE
    assert TOOL_EFFECTS["skill.run"] == NON_IDEMPOTENT
    lab.active()
    assert approved(lab.session, lab.policy, skill="summarise", script="scripts/run.sh").ok
    rows = lab.q._conn.execute("SELECT tool, state FROM operations").fetchall()
    assert [(r[0], r[1]) for r in rows] == [("skill.run", "confirmed")]


def test_the_synchronous_and_async_paths_give_the_same_result(tmp_path: Path) -> None:
    world = Lab(tmp_path)
    world.active()
    params = {"skill": "summarise", "script": "scripts/run.sh"}

    async def go() -> ToolResult:
        with pytest.raises(ApprovalRequired) as asked:
            await world.session.submit_async("skill.run", **params)
        world.policy.grant(asked.value.approval_id, decided_by="roshan")
        return await world.session.submit_async("skill.run", **params)

    result = asyncio.run(go())
    assert result.ok and result.detail["stdout"] == "ok\n" and result.detail["untrusted"]
    world.q.close()


def test_skill_run_holds_untrusted_input_in_the_rule_of_two() -> None:
    assert TOOL_LEGS["skill.run"] == frozenset({Leg.UNTRUSTED_INPUT})
    legs = held_legs(False, {"skill.run"}, AgentCapability(sensitive_data=True))
    check(legs)                                   # two legs are allowed
    with pytest.raises(AuthorityViolation):
        check(held_legs(False, {"skill.run"},
                        AgentCapability(sensitive_data=True, external_action=True)))


@pytest.mark.safety
def test_skill_run_does_not_change_the_frozen_tool_call_corpus() -> None:
    """The grammar knows the new tool. The pre-registered corpus and its schema
    prefix stay pinned to their seven tools."""
    assert "skill.run" in TOOL_SCHEMAS
    assert "skill.run" not in toolcorpus.CORPUS_TOOLS
    assert "skill.run" not in toolcorpus.schema_prefix()
    assert all(t["check"]["tool"] != "skill.run" for t in toolcorpus.build())
    from lab import grammar
    tools = {b["properties"]["tool"]["const"] for b in grammar.tool_call_schema()["oneOf"]}
    assert "skill.run" in tools


# --------------------------------------------- configured by the operator only


def test_the_daemon_turns_skill_run_on_only_with_a_pinned_image(
        tmp_path: Path, monkeypatch) -> None:
    sup = Supervisor(SupervisorConfig(db_path=tmp_path / "lab.db"))
    try:
        monkeypatch.delenv(CONTAINER_IMAGE_ENV, raising=False)
        assert configure_skill_runner(sup) is False
        assert sup.broker._container is None and sup.broker._skills is None
        monkeypatch.setenv(CONTAINER_IMAGE_ENV, "docker.io/library/alpine:3.22")
        with pytest.raises(ContainerUnavailable):
            configure_skill_runner(sup)
        monkeypatch.setenv(CONTAINER_IMAGE_ENV, IMAGE)
        assert configure_skill_runner(sup) is True
        executor = sup.broker._container
        assert executor is not None and executor.config.image == IMAGE
        assert isinstance(executor.runtime, AppleContainerRuntime)
        assert executor.workspace_root == sup.broker.workspace_root
        assert "skill.run" not in {t for tools in sup._tools.values() for t in tools}
    finally:
        sup.close()


# --------------------------------------------- on the Mac, with a real guest


REAL_IMAGE = os.environ.get("LAB_CONTAINER_IMAGE", "")
needs_container = pytest.mark.skipif(
    platform.system() != "Darwin" or AppleContainerRuntime().cli() is None or not REAL_IMAGE,
    reason="needs macOS, Apple's container CLI and LAB_CONTAINER_IMAGE pinned by digest",
)

PROBE = """#!/bin/sh
wget -q -T 3 -O /dev/null http://1.1.1.1/ 2>/dev/null && echo LEAK:http
nslookup example.com >/dev/null 2>&1 && echo LEAK:dns
ls /Users >/dev/null 2>&1 && echo LEAK:users
echo "ARGS:$*"
echo from-the-skill > /work/skill-proof.txt
echo DONE
"""


@needs_container
@pytest.mark.safety
def test_real_container_runs_an_active_skill_script_and_removes_it(tmp_path: Path) -> None:
    runtime = AppleContainerRuntime()
    world = Lab(tmp_path, runtime=runtime, image=REAL_IMAGE)
    world.active(scripts={"scripts/probe.sh": PROBE})
    result = approved(world.session, world.policy, skill="summarise",
                      script="scripts/probe.sh", args=["one", "two"], timeout=120)
    assert result.ok, result.error
    out = result.detail["stdout"]
    assert "DONE" in out and "ARGS:one two" in out and "LEAK:" not in out, out
    assert result.detail["removed"] is True and result.detail["untrusted"] is True
    assert (world.ws.root / "skill-proof.txt").read_text() == "from-the-skill\n"
    assert world.run_dirs() == []
    cli = runtime.cli()
    assert cli is not None
    import subprocess
    listed = subprocess.run([cli, "list", "--all", "--quiet"], capture_output=True,
                            text=True, check=True, timeout=60).stdout.split()
    assert result.detail["container"] not in listed
    world.q.close()
