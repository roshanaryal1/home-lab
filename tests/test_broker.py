"""Tests for the execution broker. Issue #10.

Weighted toward escape attempts. A broker that only proves it can read a
file proves nothing; the value is entirely in what it refuses.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lab.broker import (
    TOOL_TIERS,
    ExecutionBroker,
    PathEscape,
    ToolNotAllowed,
    ToolRequest,
)
from lab.policy import Tier


@pytest.fixture()
def broker(tmp_path: Path) -> ExecutionBroker:
    return ExecutionBroker(workspace_root=tmp_path / "workspaces")


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


def test_shell_run_is_confined_to_the_workspace(broker, tmp_path) -> None:
    """A brokered command cannot read outside, even via a subprocess."""
    secret = tmp_path / "secret.txt"
    secret.write_text("do not read me")

    broker.open_workspace("t1", {"shell.run"})
    result = broker.submit(
        req("t1", "shell.run", argv=["/bin/cat", str(secret)])
    )
    assert not result.ok
    assert "do not read me" not in str(result.detail)


def test_shell_run_works_inside_the_workspace(broker) -> None:
    ws = broker.open_workspace("t1", {"shell.run"})
    (ws.root / "hello.txt").write_text("world")

    result = broker.submit(
        req("t1", "shell.run", argv=["/bin/cat", "hello.txt"])
    )
    assert result.ok
    assert "world" in result.detail["stdout"]


def test_shell_run_requires_the_approve_tier(broker) -> None:
    """Running a command is never autonomous."""
    assert TOOL_TIERS["shell.run"] is Tier.APPROVE


def test_shell_run_rejects_a_malformed_argv(broker) -> None:
    broker.open_workspace("t1", {"shell.run"})
    assert not broker.submit(req("t1", "shell.run", argv="rm -rf /")).ok
