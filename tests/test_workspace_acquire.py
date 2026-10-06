"""workspace.acquire: a repository enters a workspace only from a signed source (ADR 0008).

Every test that copies uses a real git repository made in the test's own
folder as the operator's mirror, and the real git on the runner. The claims:

* a source is usable only when its entry verifies against the operator key;
* the call parks for a person, and the approval is bound to the signed entry;
* only an exact commit id is accepted, and that commit is what lands;
* nothing of the source comes along that could run code or point outside:
  no hooks, no remote, no symlink, a config git.read accepts;
* every acquisition leaves one provenance row and taints the task;
* a refusal or a failed copy leaves nothing in the workspace;
* the repo.read handler parks, runs only with the operator's signature, and
  reaches no other tool.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest

from lab import broker as broker_module
from lab import cli, handlers, loop, sources
from lab import operator as op
from lab.authority import TOOL_LEGS, AgentCapability, Leg, check, held_legs
from lab.broker import (
    NON_IDEMPOTENT,
    TOOL_EFFECTS,
    TOOL_SCHEMAS,
    TOOL_TIERS,
    ApprovalRequired,
    ExecutionBroker,
    ExecutionContext,
    InvalidParams,
    PermanentFailure,
    ToolNotAllowed,
    ToolResult,
    validate_params,
)
from lab.handlers import repo_read
from lab.journal import OperationJournal
from lab.origin import Origin, SourceType
from lab.policy import PolicyEngine, Tier
from lab.queue import TaskQueue
from lab.sources import SourceConfigError, SourceRefused, SourceSpec
from lab.supervisor import Supervisor, SupervisorConfig
from lab.supervisor import main as supervisor_main
from lab.toolcorpus import NOT_IN_CORPUS

GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.org",
         "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false", *args],
        cwd=cwd, check=True, capture_output=True, text=True, env=GIT_ENV).stdout.strip()


class Mirror:
    """The operator's mirror: a real repository with two commits."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir(parents=True)
        git(path, "init", "-q")
        (path / "README.md").write_text("first\n")
        (path / "src").mkdir()
        (path / "src" / "main.py").write_text("print('hello')\n")
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", "first commit")
        self.first = git(path, "rev-parse", "HEAD")
        (path / "README.md").write_text("second\n")
        git(path, "commit", "-q", "-am", "second commit")
        self.second = git(path, "rev-parse", "HEAD")

    def commit(self, message: str) -> str:
        git(self.path, "add", "-A")
        git(self.path, "commit", "-q", "-m", message)
        return git(self.path, "rev-parse", "HEAD")


@pytest.fixture()
def mirror(tmp_path: Path) -> Mirror:
    return Mirror(tmp_path / "mirror" / "project")


@pytest.fixture()
def keys(tmp_path: Path) -> tuple[Any, Any, Path]:
    private, public = op.generate(tmp_path / "operator")
    return op.load_private(private), op.load_public(public), public


def signed(keys: tuple[Any, Any, Path], mirror: Mirror, name: str = "project",
           by: str = "roshan") -> SourceSpec:
    return sources.sign_source(keys[0], SourceSpec(name=name, path=str(mirror.path)), by)


# ------------------------------------------------------------- broker harness


@pytest.fixture()
def queue(tmp_path: Path) -> TaskQueue:
    with TaskQueue(tmp_path / "lab.db") as q:
        for task_id in ("t1", "t2"):
            q._conn.execute("INSERT INTO tasks (id, title) VALUES (?, ?)", (task_id, task_id))
        yield q


@pytest.fixture()
def broker(tmp_path: Path, queue: TaskQueue) -> ExecutionBroker:
    contexts = {}
    while (task := queue.lease()) is not None:
        assert task.lease is not None
        contexts[task.id] = ExecutionContext(task.id, "test", task.attempts, task.lease)
    b = ExecutionBroker(workspace_root=tmp_path / "workspaces",
                        policy=PolicyEngine(queue._conn), leases=queue.owns_lease,
                        journal=OperationJournal(queue._conn))
    b.test_contexts = contexts  # type: ignore[attr-defined]
    return b


def open_ws(broker: ExecutionBroker, keys: tuple[Any, Any, Path], *specs: SourceSpec,
            granted: set[str] | None = None, tools: set[str] | None = None) -> Path:
    broker.set_sources(sources.SourceRegistry({s.name: s for s in specs}, keys[1]))
    return broker.open_workspace(
        "t1", tools or {"workspace.acquire", "git.log"},
        repo_sources=granted if granted is not None else {s.name for s in specs}).root


def submit(broker: ExecutionBroker, tool: str = "workspace.acquire", task: str = "t1",
           **params: Any) -> ToolResult:
    return broker.session(broker.test_contexts[task]).submit(  # type: ignore[attr-defined]
        tool, **params)


def approved(broker: ExecutionBroker, **params: Any) -> ToolResult:
    with pytest.raises(ApprovalRequired) as asked:
        submit(broker, **params)
    assert broker.policy is not None
    broker.policy.grant(asked.value.approval_id, decided_by="operator")
    return submit(broker, **params)


def approvals(queue: TaskQueue) -> int:
    return int(queue._conn.execute("SELECT count(*) FROM approvals").fetchone()[0])


def provenance(queue: TaskQueue) -> list[Any]:
    return sources.acquisitions(queue._conn)


def leftovers(root: Path) -> list[str]:
    return sorted(p.name for p in root.iterdir())


# ---------------------------------------------------------------- the tool


def test_the_tool_is_approve_tier_journaled_and_untrusted_input() -> None:
    assert TOOL_TIERS["workspace.acquire"] is Tier.APPROVE
    assert TOOL_EFFECTS["workspace.acquire"] == NON_IDEMPOTENT
    assert TOOL_LEGS["workspace.acquire"] == frozenset({Leg.UNTRUSTED_INPUT})
    assert set(TOOL_SCHEMAS["workspace.acquire"]) == {"source", "revision", "dir"}
    assert "workspace.acquire" in NOT_IN_CORPUS
    with pytest.raises(InvalidParams, match="unknown"):
        validate_params("workspace.acquire", {"source": "a", "revision": "b", "url": "c"})


@pytest.mark.safety
def test_a_copy_waits_for_a_person_and_the_approval_shows_the_exact_source(
        broker: ExecutionBroker, queue: TaskQueue, keys: tuple[Any, Any, Path],
        mirror: Mirror) -> None:
    spec = signed(keys, mirror)
    root = open_ws(broker, keys, spec)
    with pytest.raises(ApprovalRequired) as asked:
        submit(broker, source="project", revision=mirror.first)
    assert leftovers(root) == [] and provenance(queue) == []
    intent = json.loads(queue._conn.execute(
        "SELECT intent FROM approvals WHERE id = ?", (asked.value.approval_id,)).fetchone()[0])
    assert intent["params"] == {"source": "project", "revision": mirror.first}
    assert intent["preconditions"]["source_sha256"] == spec.digest()


@pytest.mark.safety
def test_a_source_signed_again_needs_a_new_approval(
        broker: ExecutionBroker, keys: tuple[Any, Any, Path], mirror: Mirror) -> None:
    open_ws(broker, keys, signed(keys, mirror))
    with pytest.raises(ApprovalRequired) as asked:
        submit(broker, source="project", revision=mirror.first)
    assert broker.policy is not None
    broker.policy.grant(asked.value.approval_id, decided_by="operator")
    # The operator signs the entry again (here under another name): the grant
    # was for the old entry and no longer fits.
    broker.set_sources(sources.SourceRegistry(
        {"project": signed(keys, mirror, by="someone else")}, keys[1]))
    with pytest.raises(ApprovalRequired):
        submit(broker, source="project", revision=mirror.first)


@pytest.mark.safety
def test_the_exact_commit_lands_detached_with_its_provenance(
        broker: ExecutionBroker, queue: TaskQueue, keys: tuple[Any, Any, Path],
        mirror: Mirror) -> None:
    spec = signed(keys, mirror)
    root = open_ws(broker, keys, spec)
    result = approved(broker, source="project", revision=mirror.first, dir="code")
    assert result.ok, result.error
    copy = root / "code"
    assert (copy / "README.md").read_text() == "first\n"      # not the later commit
    assert git(copy, "rev-parse", "HEAD") == mirror.first
    detached = subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=copy, env=GIT_ENV,
                              capture_output=True, check=False)
    assert detached.returncode == 1                           # HEAD names no branch
    assert result.detail["tree"] == git(mirror.path, "rev-parse", f"{mirror.first}^{{tree}}")

    (row,) = provenance(queue)
    assert (row["task_id"], row["source"], row["revision"], row["directory"]) == (
        "t1", "project", mirror.first, "code")
    assert row["source_sha256"] == spec.digest() and row["signed_by"] == "roshan"
    assert row["source_path"] == str(mirror.path) and row["workspace"] == root.name
    assert row["tree"] == result.detail["tree"] and result.detail["provenance"] == row["id"]
    event = queue._conn.execute(
        "SELECT detail FROM events WHERE kind = 'workspace_acquired'").fetchone()[0]
    assert json.loads(event)["revision"] == mirror.first
    assert queue._conn.execute("SELECT tainted FROM tasks WHERE id = 't1'").fetchone()[0] == 1
    assert [p.name for p in root.iterdir()] == ["code"]        # no staging left behind

    # git.read's own checks accept the copy, and git.log reads it.
    log = submit(broker, "git.log", repo="code")
    assert log.ok and "first commit" in log.detail["output"]
    assert "second commit" not in log.detail["output"]


@pytest.mark.safety
def test_the_copy_points_nowhere_outside_and_runs_nothing(
        broker: ExecutionBroker, keys: tuple[Any, Any, Path], mirror: Mirror,
        tmp_path: Path) -> None:
    marker = tmp_path / "hook-ran"
    (mirror.path / ".gitattributes").write_text("*.txt filter=evil\n")
    (mirror.path / "notes.txt").write_text("hi\n")
    os.symlink("/etc/passwd", mirror.path / "passwd")
    head = mirror.commit("a hostile commit")
    # Set after that commit, so only a copy that obeys them could run them.
    hook = mirror.path / ".git" / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    git(mirror.path, "config", "core.fsmonitor", f"touch {marker}")
    git(mirror.path, "config", "filter.evil.smudge", f"touch {marker}")
    assert not marker.exists()

    root = open_ws(broker, keys, signed(keys, mirror))
    result = approved(broker, source="project", revision=head)
    assert result.ok, result.error
    copy = root / "repo"
    assert not marker.exists()
    assert not (copy / ".git" / "hooks").exists()
    # The symlink is a plain file holding the target's name, never a link.
    assert not (copy / "passwd").is_symlink()
    assert (copy / "passwd").read_text() == "/etc/passwd"
    config = (copy / ".git" / "config").read_text()
    assert "origin" not in config and str(mirror.path) not in config
    assert "fsmonitor" not in config and "filter" not in config
    assert "symlinks = false" in config


@pytest.mark.safety
def test_a_submodule_entry_and_odd_names_stay_inside_the_copy_and_fetch_nothing(
        broker: ExecutionBroker, keys: tuple[Any, Any, Path], mirror: Mirror,
        tmp_path: Path) -> None:
    """Built with git plumbing, because git itself will not commit the worst names (a
    ``.GIT`` or ``.git.`` or look-alike directory is refused at the index on this system)."""
    def put(mode: str, name: str, content: bytes | str) -> None:
        if mode == "160000":
            sha = str(content)
        else:
            data = content if isinstance(content, bytes) else content.encode()
            sha = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=mirror.path,
                                 input=data, capture_output=True, check=True,
                                 env=GIT_ENV).stdout.decode().strip()
        git(mirror.path, "update-index", "--add", "--cacheinfo", f"{mode},{sha},{name}")

    put("160000", "vendor/lib", "1" * 40)
    put("100644", ".gitmodules",
        '[submodule "lib"]\n\tpath = vendor/lib\n\turl = https://example.invalid/evil.git\n')
    put("100644", "..\\evil", "backslashes are just letters on this system\n")
    put("100644", "sub\\..\\x", "so is this\n")
    tree = git(mirror.path, "write-tree")
    head = git(mirror.path, "commit-tree", tree, "-p", mirror.second, "-m", "odd entries")
    git(mirror.path, "update-ref", "refs/heads/main", head)

    root = open_ws(broker, keys, signed(keys, mirror))
    result = approved(broker, source="project", revision=head)
    assert result.ok, result.error
    copy = root / "repo"
    # The submodule is an empty directory: nothing was fetched or initialised.
    assert (copy / "vendor" / "lib").is_dir() and list((copy / "vendor" / "lib").iterdir()) == []
    assert (copy / ".gitmodules").is_file() and not (copy / ".gitmodules").is_symlink()
    assert "submodule" not in (copy / ".git" / "config").read_text()
    assert not (copy / ".git" / "modules").exists()
    # Backslash names are single file names in the copy's top folder, not paths.
    assert (copy / "..\\evil").read_text().startswith("backslashes")
    assert (copy / "sub\\..\\x").is_file()
    # Nothing was written beside the copy.
    assert leftovers(root) == ["repo"]
    assert sorted(p.name for p in root.parent.iterdir()) == [root.name]


@pytest.mark.safety
@pytest.mark.parametrize("revision", ["main", "HEAD", "abc1234", "A" * 40, "1" * 64,
                                      "main~1", "--upload-pack=touch x"])
def test_only_a_full_commit_id_is_accepted_and_refused_before_anyone_is_asked(
        broker: ExecutionBroker, queue: TaskQueue, keys: tuple[Any, Any, Path],
        mirror: Mirror, revision: str) -> None:
    root = open_ws(broker, keys, signed(keys, mirror))
    result = submit(broker, source="project", revision=revision)
    assert not result.ok and "InvalidParams" in (result.error or "")
    assert approvals(queue) == 0 and leftovers(root) == []


@pytest.mark.safety
@pytest.mark.parametrize("directory", ["../out", ".git", "a/b", "/tmp/x", "", ".hidden",
                                       "x" * 65])
def test_the_directory_is_one_plain_name_and_refused_before_anyone_is_asked(
        broker: ExecutionBroker, queue: TaskQueue, keys: tuple[Any, Any, Path],
        mirror: Mirror, directory: str) -> None:
    open_ws(broker, keys, signed(keys, mirror))
    result = submit(broker, source="project", revision=mirror.first, dir=directory)
    assert not result.ok and "InvalidParams" in (result.error or "")
    assert approvals(queue) == 0


def test_a_directory_in_use_is_refused_before_anyone_is_asked(
        broker: ExecutionBroker, queue: TaskQueue, keys: tuple[Any, Any, Path],
        mirror: Mirror) -> None:
    root = open_ws(broker, keys, signed(keys, mirror))
    (root / "repo").mkdir()
    result = submit(broker, source="project", revision=mirror.first)
    assert not result.ok and "already exists" in (result.error or "")
    assert approvals(queue) == 0


def test_a_commit_the_source_does_not_hold_fails_and_leaves_nothing(
        broker: ExecutionBroker, queue: TaskQueue, keys: tuple[Any, Any, Path],
        mirror: Mirror) -> None:
    root = open_ws(broker, keys, signed(keys, mirror))
    result = approved(broker, source="project", revision="0" * 40)
    assert not result.ok and "is not a commit in source" in (result.error or "")
    assert leftovers(root) == [] and provenance(queue) == []


def test_a_source_that_is_not_a_repository_fails_and_leaves_nothing(
        broker: ExecutionBroker, queue: TaskQueue, keys: tuple[Any, Any, Path],
        tmp_path: Path) -> None:
    empty = tmp_path / "not-a-repo"
    empty.mkdir()
    spec = sources.sign_source(keys[0], SourceSpec(name="empty", path=str(empty)), "roshan")
    root = open_ws(broker, keys, spec)
    result = approved(broker, source="empty", revision="1" * 40)
    assert not result.ok and "AcquireFailed" in (result.error or "")
    assert leftovers(root) == [] and provenance(queue) == []


def test_a_repository_over_the_workspace_ceiling_is_refused_and_removed(
        broker: ExecutionBroker, queue: TaskQueue, keys: tuple[Any, Any, Path],
        mirror: Mirror) -> None:
    root = open_ws(broker, keys, signed(keys, mirror))
    broker._workspaces["t1"].max_files = 3
    result = approved(broker, source="project", revision=mirror.second)
    assert not result.ok and "QuotaExceeded" in (result.error or "")
    assert leftovers(root) == [] and provenance(queue) == []


@pytest.mark.safety
def test_a_copy_whose_provenance_cannot_be_written_is_removed(
        broker: ExecutionBroker, queue: TaskQueue, keys: tuple[Any, Any, Path],
        mirror: Mirror, monkeypatch: pytest.MonkeyPatch) -> None:
    root = open_ws(broker, keys, signed(keys, mirror))
    with pytest.raises(ApprovalRequired) as asked:
        submit(broker, source="project", revision=mirror.first)
    assert broker.policy is not None
    broker.policy.grant(asked.value.approval_id, decided_by="operator")

    def fail(*args: Any, **kwargs: Any) -> int:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(broker.policy, "record_acquisition", fail)
    with pytest.raises(sqlite3.OperationalError):
        submit(broker, source="project", revision=mirror.first)
    assert leftovers(root) == [] and provenance(queue) == []


@pytest.mark.safety
def test_a_stop_during_the_copy_leaves_nothing_and_is_a_known_failure(
        broker: ExecutionBroker, queue: TaskQueue, keys: tuple[Any, Any, Path],
        mirror: Mirror) -> None:
    root = open_ws(broker, keys, signed(keys, mirror))
    with pytest.raises(ApprovalRequired) as asked:
        submit(broker, source="project", revision=mirror.first)
    assert broker.policy is not None
    broker.policy.grant(asked.value.approval_id, decided_by="operator")
    job = broker._job_workspace_acquire(
        broker_module.ToolRequest("workspace.acquire",
                                  {"source": "project", "revision": mirror.first}, "t1"))
    broker.revoke()                 # an emergency stop while the copy runs
    result = job.finish(job.perform())
    assert not result.ok and "cancelled by a stop" in (result.error or "")
    assert leftovers(root) == [] and provenance(queue) == []


def test_the_copy_is_journaled_once(broker: ExecutionBroker, queue: TaskQueue,
                                    keys: tuple[Any, Any, Path], mirror: Mirror) -> None:
    open_ws(broker, keys, signed(keys, mirror))
    assert approved(broker, source="project", revision=mirror.first).ok
    rows = queue._conn.execute("SELECT tool, state FROM operations").fetchall()
    assert [tuple(r) for r in rows] == [("workspace.acquire", "confirmed")]


# ---------------------------------------------------------------- signatures


@pytest.mark.safety
def test_an_unsigned_source_is_refused_before_anyone_is_asked(
        broker: ExecutionBroker, queue: TaskQueue, keys: tuple[Any, Any, Path],
        mirror: Mirror) -> None:
    root = open_ws(broker, keys, SourceSpec(name="project", path=str(mirror.path)))
    result = submit(broker, source="project", revision=mirror.first)
    assert not result.ok and "unsigned" in (result.error or "")
    assert approvals(queue) == 0 and leftovers(root) == []


@pytest.mark.safety
def test_an_entry_pointed_elsewhere_after_signing_is_refused(
        broker: ExecutionBroker, queue: TaskQueue, keys: tuple[Any, Any, Path],
        mirror: Mirror) -> None:
    entry = signed(keys, mirror)
    moved = SourceSpec(name="project", path="/etc", signed_by=entry.signed_by,
                       signature=entry.signature)
    open_ws(broker, keys, moved)
    result = submit(broker, source="project", revision=mirror.first)
    assert not result.ok and "bad signature" in (result.error or "")
    assert approvals(queue) == 0


@pytest.mark.safety
def test_a_signature_from_another_key_or_no_key_is_refused(
        tmp_path: Path, keys: tuple[Any, Any, Path], mirror: Mirror) -> None:
    other, _ = op.generate(tmp_path / "other")
    entry = sources.sign_source(op.load_private(other),
                                SourceSpec(name="project", path=str(mirror.path)), "roshan")
    assert sources.SourceRegistry({"project": entry}, keys[1]).state("project") \
        == "bad signature"
    assert sources.SourceRegistry({"project": signed(keys, mirror)}, None).state("project") \
        == "no operator key"
    with pytest.raises(SourceRefused, match="no operator key"):
        sources.SourceRegistry({"project": signed(keys, mirror)}, None).verified("project")
    with pytest.raises(SourceRefused, match="no repository source"):
        sources.SourceRegistry({}, keys[1]).spec("nope")
    assert sources.SourceRegistry({}, keys[1]).state("nope") == "unknown"
    with pytest.raises(SourceConfigError, match="who is signing"):
        sources.sign_source(keys[0], SourceSpec(name="project", path=str(mirror.path)), " ")


@pytest.mark.safety
def test_a_source_the_task_does_not_hold_is_refused(
        broker: ExecutionBroker, queue: TaskQueue, keys: tuple[Any, Any, Path],
        mirror: Mirror) -> None:
    open_ws(broker, keys, signed(keys, mirror), granted=set())
    result = submit(broker, source="project", revision=mirror.first)
    assert not result.ok and "no grant for repository source" in (result.error or "")
    assert approvals(queue) == 0


def test_unknown_sources_cannot_be_granted_and_none_configured_refuses(
        broker: ExecutionBroker, queue: TaskQueue, keys: tuple[Any, Any, Path],
        mirror: Mirror) -> None:
    with pytest.raises(ToolNotAllowed, match="unknown repository sources"):
        broker.open_workspace("t1", {"workspace.acquire"}, repo_sources={"project"})
    broker.open_workspace("t1", {"workspace.acquire"})
    result = submit(broker, source="project", revision=mirror.first)
    assert not result.ok and "no repository sources are configured" in (result.error or "")
    assert approvals(queue) == 0


@pytest.mark.parametrize(("name", "path"), [
    ("ok", "relative/path"), ("ok", "/a/../b"), ("ok", "/a/b/"), ("ok", "/a\0b"),
    ("bad name!", "/a"), ("", "/a"),
])
def test_a_malformed_entry_is_refused(name: str, path: str) -> None:
    with pytest.raises(SourceConfigError):
        SourceSpec(name=name, path=path)


@pytest.mark.parametrize("content", [
    "{}", "not json", '[{"name": "a"}]', '[{"name": "a", "path": "/a", "url": "x"}]',
    '[{"name": "a", "path": "/a"}, {"name": "a", "path": "/b"}]', '["a"]',
])
def test_a_malformed_sources_file_is_refused(tmp_path: Path, content: str) -> None:
    path = tmp_path / "sources.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(SourceConfigError):
        sources.load_sources(path)
    with pytest.raises(SourceConfigError, match="cannot read"):
        sources.load_sources(tmp_path / "missing.json")


def test_entries_round_trip_through_the_file(tmp_path: Path, keys: tuple[Any, Any, Path],
                                             mirror: Mirror) -> None:
    entry = signed(keys, mirror)
    path = tmp_path / "sources.json"
    path.write_text(json.dumps([entry.as_entry()]), encoding="utf-8")
    loaded = sources.load_sources(path)
    assert loaded == {"project": entry}
    assert sources.verify_source(keys[1], loaded["project"])


# --------------------------------------------------------- the repo.read handler


def sources_file(path: Path, *entries: SourceSpec) -> Path:
    path.write_text(json.dumps([e.as_entry() for e in entries]), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def no_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(loop, "model_from_env", lambda db=None: None)
    monkeypatch.delenv(handlers.CONTAINER_IMAGE_ENV, raising=False)
    monkeypatch.delenv(handlers.MCP_SERVERS_ENV, raising=False)
    monkeypatch.delenv(handlers.REPO_SOURCES_ENV, raising=False)


def supervisor(tmp_path: Path, public: Path | None = None,
               repo_sources: Path | None = None) -> Supervisor:
    return Supervisor(SupervisorConfig(db_path=tmp_path / "lab.db", idle_poll_seconds=0.01,
                                       operator_public_key=public,
                                       repo_sources_file=repo_sources))


def test_the_handler_holds_the_copy_and_the_read_only_git_tools() -> None:
    order = [Tier.AUTONOMOUS, Tier.NOTIFY, Tier.APPROVE, Tier.NEVER]
    assert repo_read.TIER is max((TOOL_TIERS[t] for t in repo_read.TOOLS), key=order.index)
    assert set(repo_read.TOOLS) == {"workspace.acquire", "git.status", "git.log", "git.diff"}
    assert repo_read.REF.startswith("lab.handlers.")
    legs = held_legs(True, repo_read.TOOLS, AgentCapability())
    assert legs == frozenset({Leg.UNTRUSTED_INPUT})
    check(legs)


@pytest.mark.safety
def test_the_handler_is_registered_only_with_a_signed_sources_file(
        tmp_path: Path, keys: tuple[Any, Any, Path], mirror: Mirror) -> None:
    sup = supervisor(tmp_path)
    handlers.register_all(sup)
    assert repo_read.KIND not in sup._tools and sup.broker.sources_registry is None
    sup.close()

    path = sources_file(tmp_path / "sources.json", signed(keys, mirror))
    (tmp_path / "two").mkdir()
    sup = supervisor(tmp_path / "two", keys[2], path)
    handlers.register_all(sup)
    assert sup._tools[repo_read.KIND] == repo_read.TOOLS
    assert sup._source_grants[repo_read.KIND] == frozenset({"project"})
    assert sup._egress_hosts[repo_read.KIND] == frozenset()
    sup.close()


def test_an_empty_sources_file_registers_nothing(tmp_path: Path,
                                                 keys: tuple[Any, Any, Path]) -> None:
    sup = supervisor(tmp_path, keys[2], sources_file(tmp_path / "sources.json"))
    handlers.register_all(sup)
    assert repo_read.KIND not in sup._tools
    sup.close()


@pytest.mark.safety
def test_an_unsigned_entry_stops_registration(tmp_path: Path, keys: tuple[Any, Any, Path],
                                              mirror: Mirror) -> None:
    path = sources_file(tmp_path / "sources.json", signed(keys, mirror),
                        SourceSpec(name="extra", path="/srv/extra"))
    sup = supervisor(tmp_path, keys[2], path)
    with pytest.raises(SourceRefused, match="extra"):
        handlers.register_all(sup)
    assert repo_read.KIND not in sup._tools
    sup.close()


def test_the_daemon_reads_the_sources_file_from_the_environment(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert handlers.repo_sources_file_from_env() is None
    monkeypatch.setenv(handlers.REPO_SOURCES_ENV, "  ")
    assert handlers.repo_sources_file_from_env() is None
    monkeypatch.setenv(handlers.REPO_SOURCES_ENV, str(tmp_path / "sources.json"))
    assert handlers.repo_sources_file_from_env() == tmp_path / "sources.json"


@pytest.mark.safety
@pytest.mark.parametrize("content", ["unsigned", "not json"])
def test_the_daemon_refuses_to_start_on_an_unsigned_or_malformed_file(
        tmp_path: Path, keys: tuple[Any, Any, Path], monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str], content: str) -> None:
    path = tmp_path / "sources.json"
    if content == "unsigned":
        sources_file(path, SourceSpec(name="project", path="/srv/project"))
    else:
        path.write_text("[{", encoding="utf-8")
    monkeypatch.setenv(handlers.REPO_SOURCES_ENV, str(path))
    monkeypatch.setenv("LAB_OPERATOR_PUBKEY", str(keys[2]))
    monkeypatch.delenv("LAB_LOG_DIR", raising=False)
    started: list[bool] = []
    monkeypatch.setattr(Supervisor, "run", lambda self, **kw: started.append(True))
    assert supervisor_main(["--db", str(tmp_path / "lab.db")]) == 2
    assert started == []
    assert "supervisor:" in capsys.readouterr().err


async def run_once(sup: Supervisor) -> None:
    sup.stats.leased = 0
    await sup.run(max_tasks=1)


@pytest.mark.safety
@pytest.mark.asyncio
async def test_a_repo_task_parks_and_copies_nothing_until_the_operator_signs(
        tmp_path: Path, keys: tuple[Any, Any, Path], mirror: Mirror) -> None:
    sup = supervisor(tmp_path, keys[2],
                     sources_file(tmp_path / "sources.json", signed(keys, mirror)))
    handlers.register_all(sup)
    task_id = sup.queue.add_task("read a repo", agent_kind=repo_read.KIND, payload={
        "source": "project", "revision": mirror.first, "command": "log"},
        origin=Origin(SourceType.OPERATOR))

    await run_once(sup)
    assert sup.queue.get(task_id).state == "awaiting_approval"
    assert sources.acquisitions(sup.queue._conn) == []

    # A grant without the operator's signature is not honoured.
    (pending,) = sup.policy.pending()
    sup.policy.grant(pending["id"], decided_by="roshan")
    await run_once(sup)
    assert sup.queue.get(task_id).state == "awaiting_approval"
    assert sources.acquisitions(sup.queue._conn) == []

    (pending,) = sup.policy.pending()
    intent = json.loads(pending["intent"])
    assert intent["tool"] == "workspace.acquire"
    assert intent["params"]["revision"] == mirror.first
    sup.policy.grant(pending["id"], decided_by="roshan", signer=keys[0])
    await run_once(sup)
    task = sup.queue.get(task_id)
    assert task.state == "succeeded", task.last_error
    result = json.loads(sup.queue._conn.execute(
        "SELECT result FROM tasks WHERE id = ?", (task_id,)).fetchone()[0])
    assert "first commit" in result["output"] and "second commit" not in result["output"]
    assert result["untrusted"] is True
    (row,) = sources.acquisitions(sup.queue._conn, task_id)
    assert row["revision"] == mirror.first and result["provenance"] == row["id"]
    sup.close()


@pytest.mark.safety
@pytest.mark.asyncio
async def test_a_malformed_repo_payload_fails_before_anyone_is_asked(
        tmp_path: Path, keys: tuple[Any, Any, Path], mirror: Mirror) -> None:
    sup = supervisor(tmp_path, keys[2],
                     sources_file(tmp_path / "sources.json", signed(keys, mirror)))
    handlers.register_all(sup)
    task_id = sup.queue.add_task("bad", agent_kind=repo_read.KIND, payload={
        "source": "project", "revision": "main", "command": "log"},
        origin=Origin(SourceType.OPERATOR))
    await run_once(sup)
    task = sup.queue.get(task_id)
    assert task.state == "failed" and "InvalidParams" in (task.last_error or "")
    assert sup.policy.pending() == []
    sup.close()


OTHER_CALLS: dict[str, dict[str, Any]] = {
    "fs.read": {"path": "a"}, "fs.list": {}, "fs.write": {"path": "a", "content": "x"},
    "fs.delete": {"path": "a"}, "fs.search": {"pattern": "x"},
    "shell.run": {"argv": ["/bin/echo", "hi"]}, "net.fetch": {"url": "https://example.org/"},
    "net.summarize": {"url": "https://example.org/"},
    "connector.call": {"connector": "mail", "path": "/send"},
    "memory.propose": {"text": "x", "source": "y", "reason": "z"},
    "skill.run": {"skill": "summarise", "script": "scripts/run.sh"},
    "mcp.call": {"server": "fake", "name": "add", "arguments": {}},
}


@pytest.mark.safety
def test_the_handler_reaches_no_other_tool_and_no_other_source(
        tmp_path: Path, keys: tuple[Any, Any, Path], mirror: Mirror) -> None:
    assert set(OTHER_CALLS) == set(TOOL_TIERS) - repo_read.TOOLS
    sup = supervisor(tmp_path, keys[2],
                     sources_file(tmp_path / "sources.json", signed(keys, mirror)))
    handlers.register_all(sup)
    task_id = sup.queue.add_task("probe", agent_kind=repo_read.KIND,
                                 origin=Origin(SourceType.OPERATOR))
    task = sup.queue.lease()
    assert task is not None and task.lease is not None
    sup.broker.open_workspace(task_id, set(sup._tools[repo_read.KIND]),
                              repo_sources=sup._source_grants[repo_read.KIND])
    session = sup.broker.session(ExecutionContext(task_id, repo_read.KIND, task.attempts,
                                                  task.lease))
    for tool, params in OTHER_CALLS.items():
        result = session.submit(tool, **params)
        assert not result.ok and "ToolNotAllowed" in (result.error or ""), (tool, result)
    result = session.submit("workspace.acquire", source="elsewhere", revision=mirror.first)
    assert not result.ok and "ToolNotAllowed" in (result.error or "")
    assert sup.policy.pending() == []
    sup.close()


# --------------------------------------------- the handler code, in process


class FakeTools:
    def __init__(self, *results: ToolResult) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def submit(self, tool: str, **params: Any) -> ToolResult:
        self.calls.append((tool, params))
        return self.results.pop(0)


def fake_task(payload: dict[str, Any]) -> Any:
    return type("FakeTask", (), {"payload": payload})()


@pytest.mark.asyncio
async def test_repo_handler_in_process() -> None:
    payload = {"source": "project", "revision": "a" * 40, "command": "diff"}
    tools = FakeTools(ToolResult(True, "workspace.acquire", {"tree": "b" * 40,
                                                             "provenance": 7}),
                      ToolResult(True, "git.diff", {"output": "diff", "truncated": True}))
    out = await repo_read.read_source(fake_task(payload), tools)  # type: ignore[arg-type]
    assert out["tree"] == "b" * 40 and out["provenance"] == 7 and out["truncated"] is True
    assert tools.calls == [("workspace.acquire", {"source": "project", "revision": "a" * 40,
                                                  "dir": "repo"}),
                           ("git.diff", {"repo": "repo"})]

    tools = FakeTools(ToolResult(False, "workspace.acquire", error="AcquireFailed: no"))
    with pytest.raises(PermanentFailure, match=r"workspace\.acquire failed"):
        await repo_read.read_source(fake_task(payload), tools)  # type: ignore[arg-type]
    tools = FakeTools(ToolResult(True, "workspace.acquire", {}),
                      ToolResult(False, "git.diff", error="UnsafeRepository: no"))
    with pytest.raises(PermanentFailure, match="git diff"):
        await repo_read.read_source(fake_task(payload), tools)  # type: ignore[arg-type]


@pytest.mark.parametrize("payload", [
    {}, {"source": "p", "revision": "r"}, {"source": "p", "revision": "r", "command": "push"},
    {"source": "", "revision": "r", "command": "log"},
    {"source": "p", "revision": 1, "command": "log"},
    {"source": "p", "revision": "r", "command": "log", "dir": "../x"},
])
def test_repo_handler_payload_shapes(payload: dict[str, Any]) -> None:
    with pytest.raises(PermanentFailure, match="payload must be"):
        repo_read.check_payload(payload)


# --------------------------------------------------------------------- the CLI


def test_cli_signs_lists_and_shows_what_was_acquired(
        tmp_path: Path, keys: tuple[Any, Any, Path], mirror: Mirror,
        capsys: pytest.CaptureFixture[str]) -> None:
    private = tmp_path / "operator" / op.PRIVATE_NAME
    assert cli.main(["repo", "sign", "project", str(mirror.path), "--key", str(private),
                     "--by", "roshan"]) == 0
    entry = json.loads(capsys.readouterr().out)
    path = tmp_path / "sources.json"
    path.write_text(json.dumps([entry]), encoding="utf-8")
    common = ["repo", "--sources", str(path), "--operator-pubkey", str(keys[2])]
    assert cli.main([*common, "list"]) == 0
    assert "signed" in capsys.readouterr().out

    entry["path"] = "/etc"
    path.write_text(json.dumps([entry]), encoding="utf-8")
    assert cli.main([*common, "list"]) == 1
    assert "bad signature" in capsys.readouterr().out
    path.write_text("[]", encoding="utf-8")
    assert cli.main([*common, "list"]) == 0
    assert "no repository sources" in capsys.readouterr().out
    assert cli.main(["repo", "--sources", str(tmp_path / "missing.json"), "list"]) == 1
    assert "repo:" in capsys.readouterr().err

    db = tmp_path / "lab.db"
    assert cli.main(["--db", str(db), "repo", "acquired"]) == 1
    assert "No database" in capsys.readouterr().err
    with TaskQueue(db) as q:
        q._conn.execute("INSERT INTO tasks (id, title) VALUES ('t9', 't9')")
        sources.record(q._conn, "t9", signed(keys, mirror), mirror.first,
                       "c" * 40, "repo", "task-t9-abc")
    assert cli.main(["--db", str(db), "repo", "acquired", "--task", "t9"]) == 0
    out = capsys.readouterr().out
    assert f"project@{mirror.first}" in out and "task-t9-abc/repo" in out
    assert cli.main(["--db", str(db), "repo", "acquired", "--task", "other"]) == 0
    assert "no repository has been acquired" in capsys.readouterr().out
