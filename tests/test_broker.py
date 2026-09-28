"""Tests for the execution broker. Issue #10.

Weighted toward escape attempts. A broker that only proves it can read a
file proves nothing; the value is entirely in what it refuses.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lab.broker import (
    TOOL_TIERS,
    ApprovalRequired,
    ExecutionBroker,
    PathEscape,
    ToolNotAllowed,
    ToolRequest,
    ToolResult,
)
from lab.policy import PolicyEngine, Tier
from lab.queue import TaskQueue
from lab.sandbox import available as _sandbox_available


@pytest.fixture()
def queue(tmp_path: Path) -> TaskQueue:
    with TaskQueue(tmp_path / "lab.db") as q:
        # Fixed ids so the tests can name them; audit rows reference tasks.
        for task_id in ("t1", "t2"):
            q._conn.execute("INSERT INTO tasks (id, title) VALUES (?, ?)",
                            (task_id, task_id))
        yield q


@pytest.fixture()
def broker(tmp_path: Path, queue: TaskQueue) -> ExecutionBroker:
    return ExecutionBroker(workspace_root=tmp_path / "workspaces",
                           policy=PolicyEngine(queue._conn))


def approved(broker: ExecutionBroker, request: ToolRequest) -> ToolResult:
    """Submit, have a human grant the exact call it asks about, resubmit."""
    with pytest.raises(ApprovalRequired) as asked:
        broker.submit(request)
    assert broker.policy is not None
    broker.policy.grant(asked.value.approval_id, decided_by="operator")
    return broker.submit(request)


def req(broker_task: str, tool: str, **params) -> ToolRequest:
    return ToolRequest(tool=tool, params=params, task_id=broker_task,
                       worker="w1")


# ---------------------------------------------------------- happy path


def test_write_then_read_inside_the_workspace(broker) -> None:
    broker.open_workspace("t1", {"fs.write", "fs.read"})
    w = broker.submit(req("t1", "fs.write", path="notes.txt", content="hello"))
    assert w.ok

    r = broker.submit(req("t1", "fs.read", path="notes.txt"))
    assert r.ok and r.detail["content"] == "hello"


def test_list_returns_workspace_contents(broker) -> None:
    broker.open_workspace("t1", {"fs.write", "fs.list"})
    broker.submit(req("t1", "fs.write", path="a.txt", content="x"))
    broker.submit(req("t1", "fs.write", path="b.txt", content="y"))

    result = broker.submit(req("t1", "fs.list", path="."))
    assert result.detail["entries"] == ["a.txt", "b.txt"]


# ------------------------------------------------------- path escapes


@pytest.mark.parametrize("escape", [
    "../outside.txt",
    "../../etc/passwd",
    "subdir/../../outside.txt",
    "/etc/passwd",
])
def test_path_traversal_is_refused(broker, escape) -> None:
    broker.open_workspace("t1", {"fs.read", "fs.write"})
    result = broker.submit(req("t1", "fs.write", path=escape, content="x"))
    assert not result.ok
    assert "PathEscape" in result.error


def test_symlink_out_of_the_workspace_is_refused(broker, tmp_path) -> None:
    """Textual '..' checking is not enough; confinement is by resolved path."""
    secret = tmp_path / "secret.txt"
    secret.write_text("do not read me")

    ws = broker.open_workspace("t1", {"fs.read"})
    (ws.root / "link.txt").symlink_to(secret)

    result = broker.submit(req("t1", "fs.read", path="link.txt"))
    assert not result.ok
    assert "PathEscape" in result.error


def test_workspace_resolve_raises_directly(broker) -> None:
    ws = broker.open_workspace("t1", {"fs.read"})
    with pytest.raises(PathEscape):
        ws.resolve("../../../etc/hosts")


# --------------------------------------------------------- tool grants


def test_a_tool_without_a_grant_is_refused(broker) -> None:
    """Default deny. Existing is not the same as being permitted."""
    broker.open_workspace("t1", {"fs.read"})
    result = broker.submit(req("t1", "fs.write", path="x.txt", content="y"))
    assert not result.ok
    assert "ToolNotAllowed" in result.error


def test_an_unknown_tool_is_refused(broker) -> None:
    broker.open_workspace("t1", {"fs.read"})
    result = broker.submit(req("t1", "shell.exec", cmd="rm -rf /"))
    assert not result.ok
    assert "ToolNotAllowed" in result.error


def test_granting_an_unknown_tool_is_rejected_at_open(broker) -> None:
    with pytest.raises(ToolNotAllowed):
        broker.open_workspace("t1", {"fs.read", "shell.exec"})


def test_a_grant_does_not_leak_between_tasks(broker) -> None:
    broker.open_workspace("t1", {"fs.write"})
    broker.open_workspace("t2", {"fs.read"})

    result = broker.submit(req("t2", "fs.write", path="x.txt", content="y"))
    assert not result.ok, "t2 was never granted fs.write"


def test_no_workspace_means_no_execution(broker) -> None:
    result = broker.submit(req("ghost", "fs.read", path="x.txt"))
    assert not result.ok


# ------------------------------------------------------------- quotas


def test_byte_ceiling_is_enforced(broker) -> None:
    ws = broker.open_workspace("t1", {"fs.write"})
    ws.max_bytes = 10

    assert broker.submit(req("t1", "fs.write", path="a", content="12345")).ok
    over = broker.submit(req("t1", "fs.write", path="b", content="678901"))
    assert not over.ok
    assert "QuotaExceeded" in over.error


def test_file_count_ceiling_is_enforced(broker) -> None:
    ws = broker.open_workspace("t1", {"fs.write"})
    ws.max_files = 1

    assert broker.submit(req("t1", "fs.write", path="a", content="x")).ok
    over = broker.submit(req("t1", "fs.write", path="b", content="x"))
    assert not over.ok
    assert "QuotaExceeded" in over.error


# ------------------------------------------------------------ teardown


def test_closing_destroys_the_workspace(broker) -> None:
    ws = broker.open_workspace("t1", {"fs.write"})
    broker.submit(req("t1", "fs.write", path="a.txt", content="x"))
    root = ws.root
    assert root.exists()

    broker.close_workspace("t1")
    assert not root.exists(), "a task's workspace must not outlive it"


def test_workspaces_are_not_shared(broker) -> None:
    a = broker.open_workspace("t1", {"fs.write"})
    b = broker.open_workspace("t2", {"fs.write"})
    assert a.root != b.root


# -------------------------------------------------------------- audit


def test_manifest_records_what_the_task_produced(broker) -> None:
    broker.open_workspace("t1", {"fs.write"})
    broker.submit(req("t1", "fs.write", path="out/report.md", content="hi"))

    m = broker.manifest("t1")
    assert m["file_count"] == 1
    assert "out/report.md" in m["entries"]
    assert m["granted_tools"] == ["fs.write"]


def test_manifest_is_stable_json(broker) -> None:
    broker.open_workspace("t1", {"fs.write"})
    assert broker.manifest_json("t1") == broker.manifest_json("t1")


# ---------------------------------------------------------- tool tiers


def test_destructive_tools_require_approval_tier(broker) -> None:
    """The tier comes from the tool, not from the task's own claim."""
    assert TOOL_TIERS["fs.delete"] is Tier.APPROVE
    assert TOOL_TIERS["fs.write"] is Tier.NOTIFY
    assert TOOL_TIERS["fs.read"] is Tier.AUTONOMOUS


def test_every_tool_declares_a_tier(broker) -> None:
    """A tool with no tier would be an ungated hole."""
    for name, tier in TOOL_TIERS.items():
        assert isinstance(tier, Tier), f"{name} has no valid tier"


# ------------------------------------------------- brokered shell, #17


def test_shell_run_refuses_without_os_isolation(broker, monkeypatch) -> None:
    """Fail closed on a host that cannot confine.

    Runs everywhere, and is the only shell.run test that does. CI runs on
    Linux, where Seatbelt does not exist, so this is where the
    fail-closed path is actually exercised rather than assumed.
    """
    from lab import sandbox
    monkeypatch.setattr(sandbox, "available", lambda: False)
    broker.open_workspace("t1", {"shell.run"})
    result = approved(broker, req("t1", "shell.run", argv=["/bin/echo", "hi"]))
    assert not result.ok
    assert "refusing to run unconfined" in result.error


needs_sandbox = pytest.mark.skipif(
    not _sandbox_available(),
    reason="real confinement needs macOS Seatbelt",
)


@needs_sandbox
def test_shell_run_is_confined_to_the_workspace(broker, tmp_path) -> None:
    """A brokered command cannot read outside, even via a subprocess."""
    secret = tmp_path / "secret.txt"
    secret.write_text("do not read me")

    broker.open_workspace("t1", {"shell.run"})
    result = approved(broker,
        req("t1", "shell.run", argv=["/bin/cat", str(secret)])
    )
    assert not result.ok
    assert "do not read me" not in str(result.detail)


@needs_sandbox
def test_shell_run_works_inside_the_workspace(broker) -> None:
    ws = broker.open_workspace("t1", {"shell.run"})
    (ws.root / "hello.txt").write_text("world")

    result = approved(broker,
        req("t1", "shell.run", argv=["/bin/cat", "hello.txt"])
    )
    assert result.ok
    assert "world" in result.detail["stdout"]


def test_shell_run_requires_the_approve_tier(broker) -> None:
    """Running a command is never autonomous."""
    assert TOOL_TIERS["shell.run"] is Tier.APPROVE


@needs_sandbox
def test_shell_run_rejects_a_malformed_argv(broker) -> None:
    broker.open_workspace("t1", {"shell.run"})
    assert not approved(broker, req("t1", "shell.run", argv="rm -rf /")).ok


# ------------------------------------------------ item 1.1, R01 (#43)
#
# submit() used to check only the per-task allowlist, then run the tool.
# fs.delete ran with no approval and the policy object was never called.


def test_r01_allowed_delete_without_approval_does_not_run(broker, queue) -> None:
    ws = broker.open_workspace("t1", {"fs.write", "fs.delete"})
    assert broker.submit(req("t1", "fs.write", path="keep.txt", content="x")).ok

    with pytest.raises(ApprovalRequired) as asked:
        broker.submit(req("t1", "fs.delete", path="keep.txt"))

    assert (ws.root / "keep.txt").read_text() == "x", "the effect must not happen"
    row = queue._conn.execute(
        "SELECT state, reason FROM approvals WHERE id = ?",
        (asked.value.approval_id,),
    ).fetchone()
    assert row["state"] == "pending"
    assert '"tool":"fs.delete"' in row["reason"] and "keep.txt" in row["reason"]


def test_policy_is_consulted_on_every_call(broker, monkeypatch) -> None:
    calls: list[tuple[str, Tier]] = []
    real = broker.policy.authorize_tool

    def spy(task_id, tool, params, tier):
        calls.append((tool, tier))
        return real(task_id, tool, params, tier)

    monkeypatch.setattr(broker.policy, "authorize_tool", spy)
    broker.open_workspace("t1", {"fs.write", "fs.read", "fs.list"})
    broker.submit(req("t1", "fs.write", path="a", content="1"))
    broker.submit(req("t1", "fs.read", path="a"))
    broker.submit(req("t1", "fs.list"))
    assert calls == [("fs.write", Tier.NOTIFY), ("fs.read", Tier.AUTONOMOUS),
                     ("fs.list", Tier.AUTONOMOUS)]


def test_a_granted_call_runs_once_and_only_as_approved(broker) -> None:
    ws = broker.open_workspace("t1", {"fs.write", "fs.delete"})
    for name in ("a.txt", "b.txt"):
        broker.submit(req("t1", "fs.write", path=name, content="x"))

    assert approved(broker, req("t1", "fs.delete", path="a.txt")).ok
    assert not (ws.root / "a.txt").exists()

    # The approval named a.txt. It does not stretch to b.txt, and it is spent.
    with pytest.raises(ApprovalRequired):
        broker.submit(req("t1", "fs.delete", path="b.txt"))
    assert (ws.root / "b.txt").exists()
    broker.submit(req("t1", "fs.write", path="a.txt", content="again"))
    with pytest.raises(ApprovalRequired):
        broker.submit(req("t1", "fs.delete", path="a.txt"))


def test_asking_twice_opens_one_request(broker, queue) -> None:
    broker.open_workspace("t1", {"fs.delete"})
    ids = set()
    for _ in range(3):
        with pytest.raises(ApprovalRequired) as asked:
            broker.submit(req("t1", "fs.delete", path="x"))
        ids.add(asked.value.approval_id)
    assert len(ids) == 1
    n = queue._conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]
    assert n == 1


def test_no_policy_engine_means_nothing_runs(tmp_path) -> None:
    broker = ExecutionBroker(workspace_root=tmp_path / "ws")
    ws = broker.open_workspace("t1", {"fs.write", "fs.read"})
    result = broker.submit(req("t1", "fs.write", path="a", content="x"))
    assert not result.ok and "PolicyUnavailable" in result.error
    assert not (ws.root / "a").exists()


def test_a_failed_audit_write_refuses_the_call(broker, queue) -> None:
    ws = broker.open_workspace("t1", {"fs.write"})
    queue._conn.execute(
        "CREATE TRIGGER no_audit BEFORE INSERT ON events "
        "BEGIN SELECT RAISE(ABORT, 'audit disk full'); END"
    )
    result = broker.submit(req("t1", "fs.write", path="a", content="x"))
    assert not result.ok and "PolicyUnavailable" in result.error
    assert not (ws.root / "a").exists()


def test_never_tier_is_refused_even_when_allowed(broker, monkeypatch) -> None:
    monkeypatch.setitem(TOOL_TIERS, "fs.read", Tier.NEVER)
    broker.open_workspace("t1", {"fs.read"})
    result = broker.submit(req("t1", "fs.read", path="a"))
    assert not result.ok and "PolicyDenied" in result.error


@pytest.mark.parametrize("tool", ["registry", "manifest", "_tool_fs_read", "fs.nope"])
def test_only_registered_tools_dispatch(broker, tool) -> None:
    broker.open_workspace("t1", {"fs.read"})
    result = broker.submit(req("t1", tool))
    assert not result.ok and "ToolNotAllowed" in result.error


def test_every_decision_is_audited(broker, queue) -> None:
    broker.open_workspace("t1", {"fs.write", "fs.delete"})
    broker.submit(req("t1", "fs.write", path="a", content="x"))
    with pytest.raises(ApprovalRequired):
        broker.submit(req("t1", "fs.delete", path="a"))
    kinds = [r[0] for r in queue._conn.execute(
        "SELECT kind FROM events WHERE task_id = 't1' ORDER BY id")]
    assert kinds == ["tool_allow", "tool_needs_approval"]
