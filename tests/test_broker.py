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
    ExecutionContext,
    PathEscape,
    ToolNotAllowed,
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
def contexts(queue: TaskQueue) -> dict[str, ExecutionContext]:
    """Each fixture task leased, as the supervisor would, with its context."""
    out = {}
    while (task := queue.lease()) is not None:
        assert task.lease is not None
        out[task.id] = ExecutionContext(task.id, "test", task.attempts, task.lease)
    return out


@pytest.fixture()
def broker(tmp_path: Path, queue: TaskQueue, contexts) -> ExecutionBroker:
    b = ExecutionBroker(workspace_root=tmp_path / "workspaces",
                        policy=PolicyEngine(queue._conn), leases=queue.owns_lease)
    b.test_contexts = contexts  # type: ignore[attr-defined]
    return b


def call(broker: ExecutionBroker, task_id: str, tool: str, **params) -> ToolResult:
    """What a handler for ``task_id`` does: call through its own session."""
    return broker.session(broker.test_contexts[task_id]).submit(tool, **params)


def approved(broker: ExecutionBroker, task_id: str, tool: str, **params) -> ToolResult:
    """Call, have a human grant the exact call it asks about, call again."""
    with pytest.raises(ApprovalRequired) as asked:
        call(broker, task_id, tool, **params)
    assert broker.policy is not None
    broker.policy.grant(asked.value.approval_id, decided_by="operator")
    return call(broker, task_id, tool, **params)


# ---------------------------------------------------------- happy path


def test_write_then_read_inside_the_workspace(broker) -> None:
    broker.open_workspace("t1", {"fs.write", "fs.read"})
    w = call(broker, "t1", "fs.write", path="notes.txt", content="hello")
    assert w.ok

    r = call(broker, "t1", "fs.read", path="notes.txt")
    assert r.ok and r.detail["content"] == "hello"


def test_list_returns_workspace_contents(broker) -> None:
    broker.open_workspace("t1", {"fs.write", "fs.list"})
    call(broker, "t1", "fs.write", path="a.txt", content="x")
    call(broker, "t1", "fs.write", path="b.txt", content="y")

    result = call(broker, "t1", "fs.list", path=".")
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
    result = call(broker, "t1", "fs.write", path=escape, content="x")
    assert not result.ok
    assert "PathEscape" in result.error


def test_symlink_out_of_the_workspace_is_refused(broker, tmp_path) -> None:
    """Textual '..' checking is not enough; confinement is by resolved path."""
    secret = tmp_path / "secret.txt"
    secret.write_text("do not read me")

    ws = broker.open_workspace("t1", {"fs.read"})
    (ws.root / "link.txt").symlink_to(secret)

    result = call(broker, "t1", "fs.read", path="link.txt")
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
    result = call(broker, "t1", "fs.write", path="x.txt", content="y")
    assert not result.ok
    assert "ToolNotAllowed" in result.error


def test_an_unknown_tool_is_refused(broker) -> None:
    broker.open_workspace("t1", {"fs.read"})
    result = call(broker, "t1", "shell.exec", cmd="rm -rf /")
    assert not result.ok
    assert "ToolNotAllowed" in result.error


def test_granting_an_unknown_tool_is_rejected_at_open(broker) -> None:
    with pytest.raises(ToolNotAllowed):
        broker.open_workspace("t1", {"fs.read", "shell.exec"})


def test_a_grant_does_not_leak_between_tasks(broker) -> None:
    broker.open_workspace("t1", {"fs.write"})
    broker.open_workspace("t2", {"fs.read"})

    result = call(broker, "t2", "fs.write", path="x.txt", content="y")
    assert not result.ok, "t2 was never granted fs.write"


def test_no_workspace_means_no_execution(broker) -> None:
    # t1 holds a live lease but its workspace was never opened.
    result = call(broker, "t1", "fs.read", path="x.txt")
    assert not result.ok


# ------------------------------------------------------------- quotas


def test_byte_ceiling_is_enforced(broker) -> None:
    ws = broker.open_workspace("t1", {"fs.write"})
    ws.max_bytes = 10

    assert call(broker, "t1", "fs.write", path="a", content="12345").ok
    over = call(broker, "t1", "fs.write", path="b", content="678901")
    assert not over.ok
    assert "QuotaExceeded" in over.error


def test_file_count_ceiling_is_enforced(broker) -> None:
    ws = broker.open_workspace("t1", {"fs.write"})
    ws.max_files = 1

    assert call(broker, "t1", "fs.write", path="a", content="x").ok
    over = call(broker, "t1", "fs.write", path="b", content="x")
    assert not over.ok
    assert "QuotaExceeded" in over.error


# ------------------------------------------------------------ teardown


def test_closing_destroys_the_workspace(broker) -> None:
    ws = broker.open_workspace("t1", {"fs.write"})
    call(broker, "t1", "fs.write", path="a.txt", content="x")
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
    call(broker, "t1", "fs.write", path="out/report.md", content="hi")

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
    result = approved(broker, "t1", "shell.run", argv=["/bin/echo", "hi"])
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
    result = approved(broker, "t1", "shell.run", argv=["/bin/cat", str(secret)])

    assert not result.ok
    assert "do not read me" not in str(result.detail)


@needs_sandbox
def test_shell_run_works_inside_the_workspace(broker) -> None:
    ws = broker.open_workspace("t1", {"shell.run"})
    (ws.root / "hello.txt").write_text("world")

    result = approved(broker, "t1", "shell.run", argv=["/bin/cat", "hello.txt"])

    assert result.ok
    assert "world" in result.detail["stdout"]


def test_shell_run_requires_the_approve_tier(broker) -> None:
    """Running a command is never autonomous."""
    assert TOOL_TIERS["shell.run"] is Tier.APPROVE


@needs_sandbox
def test_shell_run_rejects_a_malformed_argv(broker) -> None:
    broker.open_workspace("t1", {"shell.run"})
    assert not approved(broker, "t1", "shell.run", argv="rm -rf /").ok


# ------------------------------------------------ item 1.1, R01 (#43)
#
# submit() used to check only the per-task allowlist, then run the tool.
# fs.delete ran with no approval and the policy object was never called.


def test_r01_allowed_delete_without_approval_does_not_run(broker, queue) -> None:
    ws = broker.open_workspace("t1", {"fs.write", "fs.delete"})
    assert call(broker, "t1", "fs.write", path="keep.txt", content="x").ok

    with pytest.raises(ApprovalRequired) as asked:
        call(broker, "t1", "fs.delete", path="keep.txt")

    assert (ws.root / "keep.txt").read_text() == "x", "the effect must not happen"
    row = queue._conn.execute(
        "SELECT state, intent FROM approvals WHERE id = ?",
        (asked.value.approval_id,),
    ).fetchone()
    assert row["state"] == "pending"
    assert '"tool":"fs.delete"' in row["intent"] and "keep.txt" in row["intent"]


def test_policy_is_consulted_on_every_call(broker, monkeypatch) -> None:
    calls: list[tuple[str, Tier]] = []
    real = broker.policy.authorize_tool

    def spy(task_id, tool, params, tier, preconditions=None):
        calls.append((tool, tier))
        return real(task_id, tool, params, tier, preconditions)

    monkeypatch.setattr(broker.policy, "authorize_tool", spy)
    broker.open_workspace("t1", {"fs.write", "fs.read", "fs.list"})
    call(broker, "t1", "fs.write", path="a", content="1")
    call(broker, "t1", "fs.read", path="a")
    call(broker, "t1", "fs.list")
    assert calls == [("fs.write", Tier.NOTIFY), ("fs.read", Tier.AUTONOMOUS),
                     ("fs.list", Tier.AUTONOMOUS)]


def test_a_granted_call_runs_once_and_only_as_approved(broker) -> None:
    ws = broker.open_workspace("t1", {"fs.write", "fs.delete"})
    for name in ("a.txt", "b.txt"):
        call(broker, "t1", "fs.write", path=name, content="x")

    assert approved(broker, "t1", "fs.delete", path="a.txt").ok
    assert not (ws.root / "a.txt").exists()

    # The approval named a.txt. It does not stretch to b.txt, and it is spent.
    with pytest.raises(ApprovalRequired):
        call(broker, "t1", "fs.delete", path="b.txt")
    assert (ws.root / "b.txt").exists()
    call(broker, "t1", "fs.write", path="a.txt", content="again")
    with pytest.raises(ApprovalRequired):
        call(broker, "t1", "fs.delete", path="a.txt")


def test_asking_twice_opens_one_request(broker, queue) -> None:
    broker.open_workspace("t1", {"fs.delete"})
    ids = set()
    for _ in range(3):
        with pytest.raises(ApprovalRequired) as asked:
            call(broker, "t1", "fs.delete", path="x")
        ids.add(asked.value.approval_id)
    assert len(ids) == 1
    n = queue._conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]
    assert n == 1


def test_no_policy_engine_means_nothing_runs(tmp_path, queue, contexts) -> None:
    broker = ExecutionBroker(workspace_root=tmp_path / "ws", leases=queue.owns_lease)
    ws = broker.open_workspace("t1", {"fs.write", "fs.read"})
    result = broker.session(contexts["t1"]).submit("fs.write", path="a", content="x")
    assert not result.ok and "PolicyUnavailable" in result.error
    assert not (ws.root / "a").exists()


def test_a_failed_audit_write_refuses_the_call(broker, queue) -> None:
    ws = broker.open_workspace("t1", {"fs.write"})
    queue._conn.execute(
        "CREATE TRIGGER no_audit BEFORE INSERT ON events "
        "BEGIN SELECT RAISE(ABORT, 'audit disk full'); END"
    )
    result = call(broker, "t1", "fs.write", path="a", content="x")
    assert not result.ok and "PolicyUnavailable" in result.error
    assert not (ws.root / "a").exists()


def test_never_tier_is_refused_even_when_allowed(broker, monkeypatch) -> None:
    monkeypatch.setitem(TOOL_TIERS, "fs.read", Tier.NEVER)
    broker.open_workspace("t1", {"fs.read"})
    result = call(broker, "t1", "fs.read", path="a")
    assert not result.ok and "PolicyDenied" in result.error


@pytest.mark.parametrize("tool", ["registry", "manifest", "_tool_fs_read", "fs.nope"])
def test_only_registered_tools_dispatch(broker, tool) -> None:
    broker.open_workspace("t1", {"fs.read"})
    result = call(broker, "t1", tool)
    assert not result.ok and "ToolNotAllowed" in result.error


def test_every_decision_is_audited(broker, queue) -> None:
    broker.open_workspace("t1", {"fs.write", "fs.delete"})
    call(broker, "t1", "fs.write", path="a", content="x")
    with pytest.raises(ApprovalRequired):
        call(broker, "t1", "fs.delete", path="a")
    kinds = [r[0] for r in queue._conn.execute(
        "SELECT kind FROM events WHERE task_id = 't1' AND kind LIKE 'tool_%' ORDER BY id")]
    assert kinds == ["tool_allow", "tool_needs_approval"]


# ------------------------------------------------ item 1.4 (#49)
#
# An approval binds an immutable intent: task, tool, arguments, the
# workspace state it will act on, and the policy version. Consumption is
# one UPDATE that re-checks grant state, expiry and the intent hash.


def _grant_pending(broker, task_id: str, tool: str, **params) -> str:
    with pytest.raises(ApprovalRequired) as asked:
        call(broker, task_id, tool, **params)
    broker.policy.grant(asked.value.approval_id, decided_by="operator")
    return asked.value.approval_id


def _consumed(queue, approval_id: str) -> bool:
    return queue._conn.execute("SELECT consumed_at FROM approvals WHERE id = ?",
                               (approval_id,)).fetchone()[0] is not None


def test_workspace_change_after_review_voids_the_grant(broker, queue) -> None:
    ws = broker.open_workspace("t1", {"fs.write", "fs.delete"})
    call(broker, "t1", "fs.write", path="a.txt", content="reviewed")
    approval_id = _grant_pending(broker, "t1", "fs.delete", path="a.txt")

    call(broker, "t1", "fs.write", path="a.txt", content="swapped after review")
    with pytest.raises(ApprovalRequired) as again:
        call(broker, "t1", "fs.delete", path="a.txt")
    assert again.value.approval_id != approval_id
    assert not _consumed(queue, approval_id)
    assert (ws.root / "a.txt").exists()


def test_policy_version_change_voids_the_grant(broker, queue, monkeypatch) -> None:
    from lab import policy as policy_mod
    broker.open_workspace("t1", {"fs.write", "fs.delete"})
    call(broker, "t1", "fs.write", path="a.txt", content="x")
    approval_id = _grant_pending(broker, "t1", "fs.delete", path="a.txt")
    monkeypatch.setattr(policy_mod, "POLICY_VERSION", "next")
    with pytest.raises(ApprovalRequired):
        call(broker, "t1", "fs.delete", path="a.txt")
    assert not _consumed(queue, approval_id)


def test_expiry_at_the_moment_of_use_is_refused(broker, queue) -> None:
    ws = broker.open_workspace("t1", {"fs.write", "fs.delete"})
    call(broker, "t1", "fs.write", path="a.txt", content="x")
    approval_id = _grant_pending(broker, "t1", "fs.delete", path="a.txt")
    queue._conn.execute("UPDATE approvals SET expires_at = datetime('now', '-1 second') "
                        "WHERE id = ?", (approval_id,))
    with pytest.raises(ApprovalRequired):
        call(broker, "t1", "fs.delete", path="a.txt")
    assert not _consumed(queue, approval_id)
    assert (ws.root / "a.txt").exists()


def test_two_consumers_cannot_reserve_one_intent(tmp_path, broker, queue) -> None:
    broker.open_workspace("t1", {"fs.write", "fs.delete"})
    call(broker, "t1", "fs.write", path="a.txt", content="x")
    approval_id = _grant_pending(broker, "t1", "fs.delete", path="a.txt")
    wanted = queue._conn.execute("SELECT action_hash FROM approvals WHERE id = ?",
                                 (approval_id,)).fetchone()[0]
    with TaskQueue(tmp_path / "lab.db") as other:
        rival = PolicyEngine(other._conn)
        results = [broker.policy._consume(approval_id, wanted),
                   rival._consume(approval_id, wanted)]
    assert results == [True, False]


def test_the_stored_intent_is_what_was_hashed(broker, queue) -> None:
    import json

    from lab.policy import POLICY_VERSION, intent_hash
    broker.open_workspace("t1", {"fs.delete"})
    with pytest.raises(ApprovalRequired) as asked:
        call(broker, "t1", "fs.delete", path="a.txt")
    row = queue._conn.execute("SELECT intent, action_hash FROM approvals WHERE id = ?",
                              (asked.value.approval_id,)).fetchone()
    intent = json.loads(row["intent"])
    assert intent_hash(intent) == row["action_hash"]
    assert intent["tool"] == "fs.delete" and intent["params"] == {"path": "a.txt"}
    assert intent["policy_version"] == POLICY_VERSION
    assert set(intent["preconditions"]) == {"workspace_sha256"}
