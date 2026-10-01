"""MCP servers through the broker (M6c, #256).

Every test talks to a real child process, ``tests/fixtures/fake_mcp_server.py``,
over real pipes. Seatbelt does not exist on the Linux runner, so the broker is
given a launcher that starts the fake server the same way the Seatbelt launcher
does (no shell, minimal environment, its own process group) without the
profile. The Seatbelt launcher itself is tested with a stand-in for
``sandbox-exec`` that records the profile it is handed.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from lab import cli, mcp, sandbox
from lab import operator as op
from lab.authority import TOOL_LEGS, AgentCapability, AuthorityViolation, Leg, check, held_legs
from lab.broker import (
    NON_IDEMPOTENT,
    TOOL_EFFECTS,
    TOOL_SCHEMAS,
    TOOL_TIERS,
    ApprovalRequired,
    ExecutionBroker,
    ExecutionContext,
    InvalidParams,
    OutcomeUnknown,
    ToolNotAllowed,
    ToolResult,
    validate_params,
)
from lab.journal import OperationJournal
from lab.policy import PolicyEngine, Tier
from lab.queue import TaskQueue
from lab.untrusted import validate_evidence

FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_mcp_server.py"
TOOLS = {  # what the honest fake server offers, as tools/list returns it
    "echo": {"name": "echo", "description": "Echo the text back.",
             "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
    "add": {"name": "add", "description": "Add two numbers.",
            "inputSchema": {"type": "object", "properties": {"a": {"type": "number"},
                                                             "b": {"type": "number"}}}},
}


class Launches:
    """A test launcher: what the Seatbelt launcher does, minus the profile."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path, bool, tuple[Path, ...]]] = []

    def __call__(self, argv: Sequence[str], workspace: Path, allow_network: bool,
                 readable: tuple[Path, ...]) -> subprocess.Popen[bytes]:
        self.calls.append((list(argv), workspace, allow_network, readable))
        return mcp.start_process(argv, workspace)


@pytest.fixture()
def keys(tmp_path: Path) -> tuple[Any, Any]:
    private, public = op.generate(tmp_path / "operator")
    return op.load_private(private), op.load_public(public)


def spec(mode: str = "normal", *, allow: Sequence[str] = ("echo", "add"),
         **kwargs: Any) -> mcp.ServerSpec:
    return mcp.ServerSpec(name="fake", argv=(sys.executable, str(FAKE), mode),
                          tools={t: mcp.tool_fingerprint(TOOLS[t]) for t in allow}, **kwargs)


def registry(keys: tuple[Any, Any], *specs: mcp.ServerSpec, sign: bool = True,
             launcher: Launches | None = None) -> mcp.McpRegistry:
    signed = [mcp.sign_server(keys[0], s, "roshan") if sign else s for s in specs]
    return mcp.McpRegistry({s.name: s for s in signed}, keys[1], launcher=launcher or Launches())


# ------------------------------------------------------------- broker harness


@pytest.fixture()
def queue(tmp_path: Path) -> TaskQueue:
    with TaskQueue(tmp_path / "lab.db") as q:
        for task_id in ("t1", "t2"):
            q._conn.execute("INSERT INTO tasks (id, title) VALUES (?, ?)", (task_id, task_id))
        yield q


@pytest.fixture()
def contexts(queue: TaskQueue) -> dict[str, ExecutionContext]:
    out = {}
    while (task := queue.lease()) is not None:
        assert task.lease is not None
        out[task.id] = ExecutionContext(task.id, "test", task.attempts, task.lease)
    return out


@pytest.fixture()
def broker(tmp_path: Path, queue: TaskQueue, contexts: dict[str, ExecutionContext]
           ) -> ExecutionBroker:
    b = ExecutionBroker(workspace_root=tmp_path / "workspaces",
                        policy=PolicyEngine(queue._conn), leases=queue.owns_lease,
                        journal=OperationJournal(queue._conn))
    b.test_contexts = contexts  # type: ignore[attr-defined]
    return b


def call(broker: ExecutionBroker, task: str, **params: Any) -> ToolResult:
    return broker.session(broker.test_contexts[task]).submit(  # type: ignore[attr-defined]
        "mcp.call", **params)


def approved(broker: ExecutionBroker, task: str, **params: Any) -> ToolResult:
    with pytest.raises(ApprovalRequired) as asked:
        call(broker, task, **params)
    assert broker.policy is not None
    broker.policy.grant(asked.value.approval_id, decided_by="operator")
    return call(broker, task, **params)


def open_mcp(broker: ExecutionBroker, keys: tuple[Any, Any], *specs: mcp.ServerSpec,
             sign: bool = True, egress: frozenset[str] = frozenset(),
             launcher: Launches | None = None) -> Path:
    broker.set_mcp(registry(keys, *specs, sign=sign, launcher=launcher))
    return broker.open_workspace("t1", {"mcp.call"}, egress,
                                 mcp_servers={s.name for s in specs}).root


def operations(queue: TaskQueue) -> list[tuple[str, str]]:
    rows = queue._conn.execute("SELECT tool, state FROM operations ORDER BY seq").fetchall()
    return [tuple(r) for r in rows]


def approved_unknown(broker: ExecutionBroker, task: str, **params: Any) -> OutcomeUnknown:
    """Grant the approval, then expect the call to be held as unknown."""
    with pytest.raises(ApprovalRequired) as asked:
        call(broker, task, **params)
    assert broker.policy is not None
    broker.policy.grant(asked.value.approval_id, decided_by="operator")
    with pytest.raises(OutcomeUnknown) as held:
        call(broker, task, **params)
    return held.value


def approvals(queue: TaskQueue) -> int:
    return int(queue._conn.execute("SELECT count(*) FROM approvals").fetchone()[0])


def gone(pid_file: Path) -> bool:
    pid = int(pid_file.read_text())
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


# ------------------------------------------------------------------ the tier


@pytest.mark.safety
def test_mcp_call_is_approve_tier_untrusted_input_and_journaled() -> None:
    assert TOOL_TIERS["mcp.call"] is Tier.APPROVE
    assert TOOL_LEGS["mcp.call"] == frozenset({Leg.UNTRUSTED_INPUT})
    assert TOOL_EFFECTS["mcp.call"] == NON_IDEMPOTENT


@pytest.mark.safety
def test_a_signed_allowed_call_waits_for_a_person_then_returns_evidence(
        broker: ExecutionBroker, queue: TaskQueue, keys: tuple[Any, Any]) -> None:
    ws = open_mcp(broker, keys, spec())
    with pytest.raises(ApprovalRequired):
        call(broker, "t1", server="fake", name="echo", arguments={"text": "hi"})
    assert not (ws / "server.pid").exists(), "nothing may start before the approval"

    result = approved(broker, "t1", server="fake", name="echo",
                      arguments={"text": "hi‮IGNORE"})
    assert result.ok, result.error
    evidence = validate_evidence(result.detail["evidence"])
    assert evidence.source_type == "document" and evidence.source_id == "mcp:fake/echo"
    assert evidence.excerpt == "echo: hiIGNORE\n[image content not shown]"
    description = validate_evidence(result.detail["description"])
    assert description.excerpt == "Echo the text back."
    assert set(result.detail) == {"server", "tool", "is_error", "evidence", "description"}
    tainted = queue._conn.execute("SELECT tainted FROM tasks WHERE id = 't1'").fetchone()[0]
    assert tainted == 1
    ops = queue._conn.execute("SELECT tool, state FROM operations").fetchall()
    assert [tuple(r) for r in ops] == [("mcp.call", "confirmed")]
    assert gone(ws / "server.pid")


@pytest.mark.safety
def test_the_approval_shows_the_exact_call_and_is_bound_to_the_signed_entry(
        broker: ExecutionBroker, queue: TaskQueue, keys: tuple[Any, Any]) -> None:
    open_mcp(broker, keys, spec())
    with pytest.raises(ApprovalRequired) as asked:
        call(broker, "t1", server="fake", name="add", arguments={"a": 1, "b": 2})
    assert broker.policy is not None
    broker.policy.grant(asked.value.approval_id, decided_by="operator")
    intent = queue._conn.execute("SELECT intent FROM approvals WHERE id = ?",
                                 (asked.value.approval_id,)).fetchone()[0]
    entry = broker._mcp.spec("fake")  # type: ignore[union-attr]
    assert entry.digest() in intent and entry.tools["add"] in intent
    assert '"name":"add"' in intent.replace(" ", "")

    # The operator re-signs the same server: the old approval no longer fits.
    broker.set_mcp(registry(keys, spec(timeout_seconds=20.0)))
    with pytest.raises(ApprovalRequired):
        call(broker, "t1", server="fake", name="add", arguments={"a": 1, "b": 2})


@pytest.mark.safety
def test_the_add_tool_runs_after_approval(broker: ExecutionBroker,
                                          keys: tuple[Any, Any]) -> None:
    open_mcp(broker, keys, spec())
    result = approved(broker, "t1", server="fake", name="add", arguments={"a": 2, "b": 3})
    assert result.ok and result.detail["evidence"]["excerpt"] == "5.0"


def test_a_tool_error_is_a_failed_result_with_its_text_as_evidence(
        broker: ExecutionBroker, keys: tuple[Any, Any]) -> None:
    open_mcp(broker, keys, spec())
    result = approved(broker, "t1", server="fake", name="echo", arguments={"text": "fail"})
    assert not result.ok and result.detail["is_error"] is True
    assert result.detail["evidence"]["excerpt"] == "it failed"


# ---------------------------------------------------------------- signatures


@pytest.mark.safety
def test_an_unsigned_server_is_refused_before_anyone_is_asked(
        broker: ExecutionBroker, queue: TaskQueue, keys: tuple[Any, Any]) -> None:
    launcher = Launches()
    open_mcp(broker, keys, spec(), sign=False, launcher=launcher)
    result = call(broker, "t1", server="fake", name="echo", arguments={})
    assert not result.ok and "unsigned" in (result.error or "")
    assert approvals(queue) == 0 and launcher.calls == []


@pytest.mark.safety
def test_a_signature_from_another_key_is_refused(tmp_path: Path, keys: tuple[Any, Any],
                                                 broker: ExecutionBroker,
                                                 queue: TaskQueue) -> None:
    other, _ = op.generate(tmp_path / "not-the-operator")
    forged = mcp.sign_server(op.load_private(other), spec(), "roshan")
    launcher = Launches()
    broker.set_mcp(mcp.McpRegistry({"fake": forged}, keys[1], launcher=launcher))
    broker.open_workspace("t1", {"mcp.call"}, mcp_servers={"fake"})
    result = call(broker, "t1", server="fake", name="echo", arguments={})
    assert not result.ok and "bad signature" in (result.error or "")
    assert approvals(queue) == 0 and launcher.calls == []


@pytest.mark.safety
@pytest.mark.parametrize("edit", [
    {"argv": (sys.executable, str(FAKE), "changed")},
    {"tools": {"echo": "0" * 64}},
    {"egress_hosts": frozenset({"example.org"})},
    {"read_paths": ("/Users",)},
    {"timeout_seconds": 120.0},
    {"signed_by": "someone-else"},
])
def test_any_edit_after_signing_voids_the_signature(keys: tuple[Any, Any],
                                                    edit: dict[str, Any]) -> None:
    import dataclasses
    signed = mcp.sign_server(keys[0], spec(), "roshan")
    assert mcp.verify_server(keys[1], signed)
    edited = dataclasses.replace(signed, **edit)
    assert not mcp.verify_server(keys[1], edited)
    assert mcp.McpRegistry({"fake": edited}, keys[1]).state("fake") == "bad signature"


@pytest.mark.safety
def test_without_an_operator_key_no_server_can_be_called(keys: tuple[Any, Any]) -> None:
    signed = mcp.sign_server(keys[0], spec(), "roshan")
    reg = mcp.McpRegistry({"fake": signed}, None)
    assert reg.state("fake") == "no operator key"
    with pytest.raises(mcp.McpRefused, match="no operator key"):
        reg.verified("fake")
    assert reg.state("other") == "unknown"
    with pytest.raises(mcp.McpRefused):
        reg.spec("other")


def test_a_signature_needs_a_signer(keys: tuple[Any, Any]) -> None:
    with pytest.raises(mcp.McpConfigError):
        mcp.sign_server(keys[0], spec(), "  ")
    import dataclasses
    unsigned_by = dataclasses.replace(mcp.sign_server(keys[0], spec(), "roshan"), signed_by="")
    assert not mcp.verify_server(keys[1], unsigned_by)


# ----------------------------------------------------------------- allowlist


@pytest.mark.safety
def test_a_tool_off_the_signed_allowlist_is_refused_before_anyone_is_asked(
        broker: ExecutionBroker, queue: TaskQueue, keys: tuple[Any, Any]) -> None:
    launcher = Launches()
    open_mcp(broker, keys, spec(), launcher=launcher)
    result = call(broker, "t1", server="fake", name="wipe", arguments={})
    assert not result.ok and "not on the signed allowlist" in (result.error or "")
    assert approvals(queue) == 0 and launcher.calls == []


@pytest.mark.safety
def test_a_task_without_the_server_grant_is_refused(
        broker: ExecutionBroker, queue: TaskQueue, keys: tuple[Any, Any]) -> None:
    broker.set_mcp(registry(keys, spec()))
    broker.open_workspace("t1", {"mcp.call"})
    result = call(broker, "t1", server="fake", name="echo", arguments={})
    assert not result.ok and "no grant for MCP server" in (result.error or "")
    assert approvals(queue) == 0


@pytest.mark.safety
def test_a_task_without_the_tool_grant_is_refused(broker: ExecutionBroker,
                                                  keys: tuple[Any, Any]) -> None:
    broker.set_mcp(registry(keys, spec()))
    broker.open_workspace("t1", {"fs.read"}, mcp_servers={"fake"})
    result = call(broker, "t1", server="fake", name="echo", arguments={})
    assert not result.ok and "ToolNotAllowed" in (result.error or "")


def test_unknown_servers_cannot_be_granted_and_none_configured_refuses(
        broker: ExecutionBroker, keys: tuple[Any, Any]) -> None:
    with pytest.raises(ToolNotAllowed, match="unknown MCP servers"):
        broker.open_workspace("t1", {"mcp.call"}, mcp_servers={"fake"})
    broker.open_workspace("t1", {"mcp.call"})
    result = call(broker, "t1", server="fake", name="echo", arguments={})
    assert not result.ok and "no MCP servers are configured" in (result.error or "")


@pytest.mark.safety
def test_a_changed_tool_is_refused_until_the_operator_signs_again(
        broker: ExecutionBroker, keys: tuple[Any, Any]) -> None:
    ws = open_mcp(broker, keys, spec("changed"))
    result = approved(broker, "t1", server="fake", name="echo", arguments={"text": "hi"})
    assert not result.ok and result.detail["refused"] is True
    assert "changed since the operator signed it" in (result.error or "")
    assert "ssh" not in json.dumps(result.detail) + (result.error or "")
    assert gone(ws / "server.pid")
    # The other allowed tool did not change and still works.
    assert approved(broker, "t1", server="fake", name="add", arguments={"a": 1}).ok


@pytest.mark.safety
def test_an_added_tool_cannot_be_called_and_the_rest_still_can(
        broker: ExecutionBroker, keys: tuple[Any, Any]) -> None:
    open_mcp(broker, keys, spec("added"))
    refused = call(broker, "t1", server="fake", name="exfiltrate", arguments={})
    assert not refused.ok and "not on the signed allowlist" in (refused.error or "")
    assert approved(broker, "t1", server="fake", name="echo", arguments={"text": "x"}).ok


def test_a_tool_the_server_stopped_offering_is_refused(keys: tuple[Any, Any],
                                                       tmp_path: Path) -> None:
    gone_tool = spec(allow=("echo",))
    gone_tool = mcp.ServerSpec(name="fake", argv=gone_tool.argv,
                               tools={"echo": gone_tool.tools["echo"], "vanished": "a" * 64})
    reg = registry(keys, gone_tool)
    with pytest.raises(mcp.McpRefused, match="no longer offers"):
        reg.call("fake", "vanished", {}, tmp_path)


# -------------------------------------------------------------------- limits


@pytest.mark.safety
def test_an_oversize_message_from_the_server_is_refused_and_the_process_killed(
        broker: ExecutionBroker, queue: TaskQueue, keys: tuple[Any, Any]) -> None:
    # The oversize reply came after tools/call was sent: the tool may have run.
    ws = open_mcp(broker, keys, spec("huge"))
    held = approved_unknown(broker, "t1", server="fake", name="echo", arguments={})
    assert f"over {mcp.MAX_MESSAGE_BYTES} bytes" in str(held.__cause__)
    assert operations(queue) == [("mcp.call", "uncertain")]
    assert gone(ws / "server.pid")


@pytest.mark.safety
def test_output_without_a_newline_cannot_grow_past_the_cap(keys: tuple[Any, Any],
                                                           tmp_path: Path) -> None:
    reg = registry(keys, spec("flood"))
    started = time.monotonic()
    with pytest.raises(mcp.McpOutcomeUnknown, match=r"McpRefused.*over"):
        reg.call("fake", "echo", {}, tmp_path)
    assert time.monotonic() - started < 20
    assert gone(tmp_path / "server.pid")


@pytest.mark.safety
def test_oversize_arguments_are_refused_before_policy() -> None:
    with pytest.raises(InvalidParams):
        validate_params("mcp.call", {"server": "fake", "name": "echo",
                                     "arguments": {"text": "x" * (mcp.MAX_ARGUMENT_BYTES + 1)}})
    with pytest.raises(InvalidParams):
        validate_params("mcp.call", {"server": "fake", "name": "echo", "arguments": ["a"]})
    with pytest.raises(InvalidParams):
        validate_params("mcp.call", {"server": "fake", "name": "echo",
                                     "arguments": {"x": float("nan")}})
    validate_params("mcp.call", {"server": "fake", "name": "echo", "arguments": {"text": "x"}})
    assert set(TOOL_SCHEMAS["mcp.call"]) == {"server", "name", "arguments"}


def test_the_registry_checks_arguments_itself(keys: tuple[Any, Any]) -> None:
    reg = registry(keys, spec())
    with pytest.raises(mcp.McpRefused, match="JSON object"):
        reg.check_call("fake", "echo", ["not", "an", "object"])
    with pytest.raises(mcp.McpRefused, match="over"):
        reg.check_call("fake", "echo", {"t": "x" * (mcp.MAX_ARGUMENT_BYTES + 1)})


@pytest.mark.safety
def test_a_timeout_after_dispatch_is_held_as_unknown_and_kills_the_server(
        broker: ExecutionBroker, queue: TaskQueue, keys: tuple[Any, Any]) -> None:
    ws = open_mcp(broker, keys, spec("hang", timeout_seconds=3.0))
    started = time.monotonic()
    held = approved_unknown(broker, "t1", server="fake", name="echo", arguments={})
    assert time.monotonic() - started < 15
    assert isinstance(held.__cause__, mcp.McpOutcomeUnknown)
    assert isinstance(held.__cause__.__cause__, mcp.McpTimeout)
    assert (ws / "calling").exists(), "the server was inside the tool call"
    assert operations(queue) == [("mcp.call", "uncertain")]
    assert gone(ws / "server.pid")

    # A retry is never run blindly: the same call is held again, no new server.
    (ws / "server.pid").unlink()
    broker._seq.clear()            # as if a new lease repeated the call
    approved_unknown(broker, "t1", server="fake", name="echo", arguments={})
    assert not (ws / "server.pid").exists()


@pytest.mark.safety
def test_a_stall_in_the_tool_list_is_an_ordinary_refusal(
        broker: ExecutionBroker, queue: TaskQueue, keys: tuple[Any, Any]) -> None:
    # tools/call was never sent, so nothing can have happened.
    ws = open_mcp(broker, keys, spec("stall_list", timeout_seconds=2.0))
    result = approved(broker, "t1", server="fake", name="echo", arguments={})
    assert not result.ok and result.detail["timed_out"] is True
    assert (ws / "listing").exists() and not (ws / "calling").exists()
    assert operations(queue) == [("mcp.call", "confirmed")]
    assert gone(ws / "server.pid")


@pytest.mark.safety
def test_a_cancel_after_dispatch_is_held_as_unknown_and_kills_the_server(
        broker: ExecutionBroker, queue: TaskQueue, keys: tuple[Any, Any]) -> None:
    ws = open_mcp(broker, keys, spec("hang"))

    def stop_when_calling() -> None:
        deadline = time.monotonic() + 20
        while not (ws / "calling").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        broker.cancel_running("t1")

    stopper = threading.Thread(target=stop_when_calling)
    with pytest.raises(ApprovalRequired) as asked:
        call(broker, "t1", server="fake", name="echo", arguments={})
    assert broker.policy is not None
    broker.policy.grant(asked.value.approval_id, decided_by="operator")
    stopper.start()
    started = time.monotonic()
    with pytest.raises(OutcomeUnknown) as held:
        call(broker, "t1", server="fake", name="echo", arguments={})
    stopper.join()
    assert time.monotonic() - started < 15, "cancel must not wait for the timeout"
    assert isinstance(held.value.__cause__, mcp.McpOutcomeUnknown)
    assert isinstance(held.value.__cause__.__cause__, mcp.McpCancelled)
    assert operations(queue) == [("mcp.call", "uncertain")]
    assert gone(ws / "server.pid")


@pytest.mark.safety
def test_a_revoked_broker_kills_a_running_server(keys: tuple[Any, Any],
                                                 tmp_path: Path) -> None:
    reg = registry(keys, spec("hang"))
    cancel = threading.Event()

    def revoke_when_calling() -> None:
        deadline = time.monotonic() + 20
        while not (tmp_path / "calling").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        cancel.set()

    threading.Thread(target=revoke_when_calling).start()
    with pytest.raises(mcp.McpOutcomeUnknown) as raised:
        reg.call("fake", "echo", {}, tmp_path, cancel=cancel)
    assert isinstance(raised.value.__cause__, mcp.McpCancelled)
    assert gone(tmp_path / "server.pid")


@pytest.mark.safety
def test_a_cancel_before_the_tool_call_is_sent_is_a_plain_cancel(
        keys: tuple[Any, Any], tmp_path: Path) -> None:
    reg = registry(keys, spec("stall_list"))
    cancel = threading.Event()

    def revoke_when_listing() -> None:
        deadline = time.monotonic() + 20
        while not (tmp_path / "listing").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        cancel.set()

    threading.Thread(target=revoke_when_listing).start()
    with pytest.raises(mcp.McpCancelled):
        reg.call("fake", "echo", {}, tmp_path, cancel=cancel)
    assert gone(tmp_path / "server.pid")


@pytest.mark.safety
def test_calls_per_task_are_capped(broker: ExecutionBroker, keys: tuple[Any, Any],
                                   monkeypatch: pytest.MonkeyPatch) -> None:
    import lab.broker as broker_mod
    monkeypatch.setattr(broker_mod, "MCP_MAX_CALLS_PER_TASK", 2)
    open_mcp(broker, keys, spec())
    assert approved(broker, "t1", server="fake", name="add", arguments={"a": 1}).ok
    assert approved(broker, "t1", server="fake", name="add", arguments={"a": 2}).ok
    third = call(broker, "t1", server="fake", name="add", arguments={"a": 3})
    assert not third.ok and "QuotaExceeded" in (third.error or "")


@pytest.mark.safety
def test_a_resumed_task_replays_its_calls_past_the_ceiling_but_runs_no_new_one(
        broker: ExecutionBroker, queue: TaskQueue, keys: tuple[Any, Any]) -> None:
    launches = Launches()
    open_mcp(broker, keys, spec(), launcher=launches)
    for i in range(mcp.MAX_CALLS_PER_TASK):
        assert approved(broker, "t1", server="fake", name="add", arguments={"a": i}).ok
    assert len(launches.calls) == mcp.MAX_CALLS_PER_TASK

    # The task parks on another approval, then resumes under a new lease and
    # its handler repeats the calls it already made.
    first = broker.test_contexts["t1"]  # type: ignore[attr-defined]
    queue.park_for_approval(first.lease, "waiting on something else")
    queue.resume_after_approval("t1")
    task = queue.lease()
    assert task is not None and task.id == "t1" and task.lease is not None
    broker.test_contexts["t1"] = ExecutionContext(  # type: ignore[attr-defined]
        "t1", "test", task.attempts, task.lease)

    # Approvals are single use, so the replay is asked for again like any call.
    replay = approved(broker, "t1", server="fake", name="add", arguments={"a": 0})
    assert replay.ok and replay.detail["replayed"] is True
    assert replay.detail["evidence"]["excerpt"] == "0.0"
    assert len(launches.calls) == mcp.MAX_CALLS_PER_TASK, "a replay starts no server"

    # A genuinely new call is still over the ceiling, and no one is asked.
    asked = approvals(queue)
    new = call(broker, "t1", server="fake", name="add", arguments={"a": 99})
    assert not new.ok and "QuotaExceeded" in (new.error or "")
    assert approvals(queue) == asked
    assert len(launches.calls) == mcp.MAX_CALLS_PER_TASK


@pytest.mark.safety
def test_a_replay_still_needs_the_server_grant_and_signature(
        broker: ExecutionBroker, keys: tuple[Any, Any]) -> None:
    open_mcp(broker, keys, spec())
    assert approved(broker, "t1", server="fake", name="add", arguments={"a": 1}).ok
    broker._seq.clear()            # as if a new lease repeated the call
    broker.set_mcp(registry(keys, spec(), sign=False))
    refused = call(broker, "t1", server="fake", name="add", arguments={"a": 1})
    assert not refused.ok and "unsigned" in (refused.error or "")


# ------------------------------------------------------------------ network


@pytest.mark.safety
def test_no_network_unless_the_signed_entry_and_the_task_both_allow_it(
        broker: ExecutionBroker, queue: TaskQueue, keys: tuple[Any, Any]) -> None:
    launcher = Launches()
    networked = spec(egress_hosts=frozenset({"api.example.org"}))
    open_mcp(broker, keys, networked, launcher=launcher)
    refused = call(broker, "t1", server="fake", name="echo", arguments={})
    assert not refused.ok and "egress list does not allow" in (refused.error or "")
    assert launcher.calls == [] and approvals(queue) == 0

    broker.close_workspace("t1")
    broker.open_workspace("t1", {"mcp.call"}, {"api.example.org"}, mcp_servers={"fake"})
    assert approved(broker, "t1", server="fake", name="echo", arguments={}).ok
    assert launcher.calls[-1][2] is True


@pytest.mark.safety
def test_a_server_without_egress_hosts_runs_with_network_off(
        broker: ExecutionBroker, keys: tuple[Any, Any]) -> None:
    launcher = Launches()
    ws = open_mcp(broker, keys, spec(), launcher=launcher,
                  egress=frozenset({"api.example.org"}))
    assert approved(broker, "t1", server="fake", name="echo", arguments={}).ok
    argv, workspace, allow_network, _ = launcher.calls[-1]
    assert allow_network is False and workspace == ws
    assert argv == [sys.executable, str(FAKE), "normal"]


# ------------------------------------------------------------ authority


@pytest.mark.safety
def test_mcp_plus_a_credentialed_connector_breaks_the_rule_of_two() -> None:
    legs = held_legs(False, {"mcp.call", "connector.call"}, AgentCapability())
    with pytest.raises(AuthorityViolation):
        check(legs)
    check(held_legs(False, {"mcp.call", "fs.write"}, AgentCapability()))


# --------------------------------------------------------- the process itself


@pytest.mark.safety
def test_the_server_gets_no_shell_and_only_the_minimal_environment(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAB_SECRET_TOKEN", "do-not-leak")
    script = tmp_path / "env.py"
    script.write_text("import json, os, sys\njson.dump(dict(os.environ), sys.stdout)\n")
    proc = mcp.start_process([sys.executable, str(script)], tmp_path)
    out, _ = proc.communicate(timeout=30)
    seen = json.loads(out)
    wanted = sandbox.command_environment(tmp_path)
    # macOS adds a text-encoding variable, and Python may set LC_CTYPE. Nothing else.
    extra = {"__CF_USER_TEXT_ENCODING", "LC_CTYPE"}
    assert {k: v for k, v in seen.items() if k not in extra} == wanted
    assert "LAB_SECRET_TOKEN" not in out.decode()

    sleeper = mcp.start_process([sys.executable, "-c", "import time; time.sleep(60)"],
                                tmp_path)
    try:
        assert os.getpgid(sleeper.pid) == sleeper.pid != os.getpgid(0)
    finally:
        sleeper.kill()
        sleeper.wait()


@pytest.mark.safety
def test_the_seatbelt_launcher_refuses_when_there_is_no_sandbox(
        keys: tuple[Any, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "available", lambda: False)
    reg = mcp.McpRegistry({"fake": mcp.sign_server(keys[0], spec(), "roshan")}, keys[1])
    with pytest.raises(mcp.McpRefused, match="unconfined"):
        reg.call("fake", "echo", {}, tmp_path)
    assert not (tmp_path / "server.pid").exists()


@pytest.mark.safety
@pytest.mark.parametrize("hosts", [frozenset(), frozenset({"api.example.org"})])
def test_the_seatbelt_launcher_hands_the_profile_to_sandbox_exec(
        keys: tuple[Any, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        hosts: frozenset[str]) -> None:
    """A stand-in for sandbox-exec records its arguments, drops ``-p PROFILE``
    and runs the rest, so the whole path runs on Linux too."""
    record = tmp_path / "sandbox-exec.json"
    stand_in = tmp_path / "sandbox-exec"
    stand_in.write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        f"json.dump(sys.argv[1:], open({str(record)!r}, 'w'))\n"
        "os.execv(sys.argv[3], sys.argv[3:])\n")
    stand_in.chmod(stand_in.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(sandbox, "available", lambda: True)
    monkeypatch.setattr(sandbox, "SANDBOX_EXEC", str(stand_in))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    entry = mcp.sign_server(keys[0], spec(egress_hosts=hosts, read_paths=(str(tmp_path),)),
                            "roshan")
    reg = mcp.McpRegistry({"fake": entry}, keys[1])
    outcome = reg.call("fake", "echo", {"text": "sandboxed"}, workspace, egress_hosts=hosts)
    assert outcome.text.startswith("echo: sandboxed")
    flag, profile, *argv = json.loads(record.read_text())
    assert flag == "-p" and argv == [sys.executable, str(FAKE), "normal"]
    assert "(deny default)" in profile
    assert f'(allow file-read* file-write* (subpath "{workspace.resolve()}"))' in profile
    assert f'(allow file-read* (subpath "{tmp_path.resolve()}"))' in profile
    assert ("(allow network-outbound)" in profile) is bool(hosts)


# ------------------------------------------------------------------ protocol


def test_server_requests_are_refused_and_notifications_skipped(keys: tuple[Any, Any],
                                                              tmp_path: Path) -> None:
    outcome = registry(keys, spec("chatty")).call("fake", "echo", {"text": "a"}, tmp_path)
    assert outcome.text.startswith("echo: a")
    reply = json.loads((tmp_path / "server-request-reply.json").read_text())
    assert reply["id"] == "srv-1" and reply["error"]["code"] == -32601


@pytest.mark.safety
def test_a_json_rpc_error_passes_on_the_code_but_never_the_servers_words(
        keys: tuple[Any, Any], tmp_path: Path) -> None:
    with pytest.raises(mcp.McpProtocolError) as raised:
        registry(keys, spec("rpcerror")).call("fake", "echo", {}, tmp_path)
    assert "-32602" in str(raised.value) and "IGNORE" not in str(raised.value)


@pytest.mark.parametrize(("mode", "error", "match"), [
    # These two break while answering tools/call, so the tool may have run.
    ("garbage", mcp.McpOutcomeUnknown, r"McpProtocolError: .*not JSON"),
    ("exit", mcp.McpOutcomeUnknown, r"McpProtocolError: .*closed its output"),
    ("noversion", mcp.McpProtocolError, "protocol version"),
    # NaN in a tool list arrives before tools/call: an ordinary refusal.
    ("nan", mcp.McpProtocolError, "not JSON"),
    ("dupe", mcp.McpRefused, "twice"),
    ("many", mcp.McpRefused, "more than"),
    ("endless", mcp.McpRefused, "pages"),
])
def test_a_server_that_breaks_the_protocol_is_refused(
        keys: tuple[Any, Any], tmp_path: Path, mode: str, error: type[Exception],
        match: str) -> None:
    with pytest.raises(error, match=match):
        registry(keys, spec(mode)).call("fake", "echo", {}, tmp_path)
    assert gone(tmp_path / "server.pid")


def test_a_paged_tool_list_is_read_whole(keys: tuple[Any, Any], tmp_path: Path) -> None:
    outcome = registry(keys, spec("paged")).call("fake", "add", {"a": 1, "b": 1}, tmp_path)
    assert outcome.text == "2.0"


def test_a_program_that_cannot_start_is_an_error(keys: tuple[Any, Any],
                                                 tmp_path: Path) -> None:
    missing = mcp.ServerSpec(name="fake", argv=("/nonexistent/server",), tools={})
    with pytest.raises(mcp.McpError, match="cannot start"):
        registry(keys, missing).snapshot("fake", tmp_path)


def test_the_client_refuses_to_send_an_oversize_message(tmp_path: Path) -> None:
    proc = mcp.start_process([sys.executable, str(FAKE)], tmp_path)
    client = mcp.StdioClient(proc, deadline=time.monotonic() + 10, max_bytes=100)
    try:
        with pytest.raises(mcp.McpRefused, match="outgoing"):
            client.send({"text": "x" * 200})
    finally:
        client.close()


def test_result_text_names_what_it_does_not_show() -> None:
    assert mcp.result_text({"content": [{"type": "text", "text": "a"}, "odd",
                                        {"type": "audio"}]}) == (
        "a\n[unknown content not shown]\n[audio content not shown]")
    assert mcp.result_text({"structuredContent": {"b": 1}}) == '{"b":1}'
    assert mcp.result_text({}) == ""


# -------------------------------------------------------------------- config


def test_the_config_file_round_trips_and_is_strict(keys: tuple[Any, Any],
                                                   tmp_path: Path) -> None:
    signed = mcp.sign_server(keys[0], spec(egress_hosts=frozenset({"a.example.org"})),
                             "roshan")
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps([signed.as_entry()]))
    loaded = mcp.load_servers(path)
    assert loaded["fake"] == signed and mcp.verify_server(keys[1], loaded["fake"])

    for bad in ([dict(signed.as_entry(), extra=1)], {"not": "a list"},
                [signed.as_entry(), signed.as_entry()],
                [dict(signed.as_entry(), argv="python server.py")]):
        path.write_text(json.dumps(bad))
        with pytest.raises(mcp.McpConfigError):
            mcp.load_servers(path)
    with pytest.raises(mcp.McpConfigError):
        mcp.load_servers(tmp_path / "missing.json")


@pytest.mark.parametrize("kwargs", [
    {"name": "bad name"},
    {"argv": ()},
    {"argv": ("python3", "server.py")},
    {"argv": ("/bin/sh", "a\0b")},
    {"tools": {"bad tool!": "a" * 64}},
    {"tools": {"echo": "short"}},
    {"egress_hosts": frozenset({"10.0.0.1"})},
    {"read_paths": ("relative",)},
    {"timeout_seconds": 0},
    {"timeout_seconds": mcp.MAX_TIMEOUT_SECONDS + 1},
    {"timeout_seconds": True},
    {"signature": 5},
])
def test_a_bad_entry_is_refused(kwargs: dict[str, Any]) -> None:
    base: dict[str, Any] = {"name": "fake", "argv": ("/usr/bin/true",), "tools": {}}
    with pytest.raises(mcp.McpConfigError):
        mcp.ServerSpec(**(base | kwargs))


# ----------------------------------------------------------------------- CLI


@pytest.fixture()
def cli_setup(tmp_path: Path, keys: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch
              ) -> tuple[Path, Path, Path]:
    private, public = tmp_path / "operator" / "operator.key", tmp_path / "operator" / "operator.pub"
    monkeypatch.setattr(cli, "_mcp_launcher", lambda: Launches())
    servers = tmp_path / "mcp.json"
    return servers, private, public


def run_cli(servers: Path, public: Path | None, *args: str) -> int:
    pub = ["--operator-pubkey", str(public)] if public else []
    return cli.main(["mcp", "--servers", str(servers), *pub, *args])


def test_snapshot_prints_the_entry_to_sign_and_signs_it_with_the_key(
        cli_setup: tuple[Path, Path, Path], keys: tuple[Any, Any],
        capsys: pytest.CaptureFixture[str]) -> None:
    servers, private, public = cli_setup
    bare = mcp.ServerSpec(name="fake", argv=(sys.executable, str(FAKE), "normal"))
    servers.write_text(json.dumps([bare.as_entry()]))
    assert run_cli(servers, public, "snapshot", "fake") == 0
    shown = json.loads(capsys.readouterr().out)
    assert [t["name"] for t in shown["offered"]] == ["add", "echo", "wipe"]
    assert shown["entry"]["signature"] is None
    assert shown["entry"]["tools"]["echo"] == mcp.tool_fingerprint(TOOLS["echo"])

    assert run_cli(servers, public, "snapshot", "fake", "--allow", "echo",
                   "--key", str(private), "--by", "roshan") == 0
    entry = json.loads(capsys.readouterr().out)["entry"]
    assert list(entry["tools"]) == ["echo"]
    servers.write_text(json.dumps([entry]))
    assert mcp.verify_server(keys[1], mcp.load_servers(servers)["fake"])

    assert run_cli(servers, public, "snapshot", "fake", "--allow", "nope") == 1
    assert "does not offer" in capsys.readouterr().err


def test_list_shows_each_state_and_check_finds_drift(
        cli_setup: tuple[Path, Path, Path], keys: tuple[Any, Any],
        capsys: pytest.CaptureFixture[str]) -> None:
    servers, _, public = cli_setup
    good = mcp.sign_server(keys[0], spec(), "roshan")
    drifted = mcp.sign_server(keys[0], mcp.ServerSpec(
        name="drifted", argv=spec("changed").argv, tools=spec().tools), "roshan")
    unsigned = mcp.ServerSpec(name="unsigned", argv=spec().argv)
    servers.write_text(json.dumps([e.as_entry() for e in (good, drifted, unsigned)]))

    assert run_cli(servers, public, "list") == 0
    out = capsys.readouterr().out
    assert "fake" in out and "signed" in out and "unsigned" in out

    assert run_cli(servers, public, "list", "--check") == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("DRIFT drifted") and "changed ['echo']" in lines[0]
    assert lines[1].startswith("ok    fake") and "1 other tools not allowed" in lines[1]
    assert lines[2].startswith("FAIL  unsigned")

    servers.write_text(json.dumps([good.as_entry()]))
    assert run_cli(servers, public, "list", "--check") == 0
    assert capsys.readouterr().out.startswith("ok    fake")
    assert run_cli(servers, None, "list") == 0
    assert "no operator key" in capsys.readouterr().out


def test_list_with_no_servers_and_a_bad_file(cli_setup: tuple[Path, Path, Path],
                                             capsys: pytest.CaptureFixture[str]) -> None:
    servers, _, public = cli_setup
    servers.write_text("[]")
    assert run_cli(servers, public, "list", "--check") == 0
    assert "no MCP servers are configured" in capsys.readouterr().out
    servers.write_text("{")
    assert run_cli(servers, public, "list") == 1
    assert "cannot read" in capsys.readouterr().err


def test_the_cli_launcher_is_the_sandbox() -> None:
    assert cli._mcp_launcher() is mcp.seatbelt_launcher


# -------------------------------------------------------------------- async


@pytest.mark.safety
async def test_the_async_path_runs_the_server_off_the_loop(
        broker: ExecutionBroker, keys: tuple[Any, Any]) -> None:
    open_mcp(broker, keys, spec())
    session = broker.session(broker.test_contexts["t1"])  # type: ignore[attr-defined]
    with pytest.raises(ApprovalRequired) as asked:
        await session.submit_async("mcp.call", server="fake", name="add",
                                   arguments={"a": 4, "b": 4})
    assert broker.policy is not None
    broker.policy.grant(asked.value.approval_id, decided_by="operator")
    result = await session.submit_async("mcp.call", server="fake", name="add",
                                        arguments={"a": 4, "b": 4})
    assert result.ok and result.detail["evidence"]["excerpt"] == "8.0"


@pytest.mark.safety
async def test_the_async_path_holds_a_timeout_after_dispatch_as_unknown(
        broker: ExecutionBroker, queue: TaskQueue, keys: tuple[Any, Any]) -> None:
    open_mcp(broker, keys, spec("hang", timeout_seconds=2.0))
    session = broker.session(broker.test_contexts["t1"])  # type: ignore[attr-defined]
    with pytest.raises(ApprovalRequired) as asked:
        await session.submit_async("mcp.call", server="fake", name="echo", arguments={})
    assert broker.policy is not None
    broker.policy.grant(asked.value.approval_id, decided_by="operator")
    with pytest.raises(OutcomeUnknown):
        await session.submit_async("mcp.call", server="fake", name="echo", arguments={})
    assert operations(queue) == [("mcp.call", "uncertain")]


# --------------------------------------------------------------- supervisor


@pytest.mark.safety
async def test_the_supervisor_loads_signed_servers_and_grants_them_per_handler(
        tmp_path: Path, keys: tuple[Any, Any]) -> None:
    from lab.origin import Origin, SourceType
    from lab.queue import Task
    from lab.supervisor import Supervisor, SupervisorConfig

    servers = tmp_path / "mcp.json"
    servers.write_text(json.dumps([mcp.sign_server(keys[0], spec(), "roshan").as_entry()]))
    sup = Supervisor(SupervisorConfig(
        db_path=tmp_path / "lab.db", idle_poll_seconds=0.01, mcp_servers_file=servers,
        operator_public_key=tmp_path / "operator" / "operator.pub"))
    registry = sup.broker._mcp
    assert registry is not None and registry.state("fake") == "signed"
    registry._launcher = Launches()
    seen: list[ToolResult] = []

    async def handler(task: Task, tools: Any) -> dict[str, Any]:
        seen.append(tools.submit("mcp.call", server="fake", name="add",
                                 arguments={"a": 20, "b": 22}))
        return {"ok": seen[-1].ok}

    sup.register("mcp-user", handler, tools={"mcp.call"}, mcp_servers={"fake"})
    task_id = sup.queue.add_task("add", agent_kind="mcp-user",
                                 origin=Origin(SourceType.OPERATOR))
    await sup.run(max_tasks=1)
    assert sup.queue.get(task_id).state == "awaiting_approval"
    (pending,) = sup.policy.pending()
    sup.policy.grant(pending["id"], decided_by="operator", signer=keys[0])
    sup.stats.leased = 0
    await sup.run(max_tasks=1)
    assert seen[-1].ok and seen[-1].detail["evidence"]["excerpt"] == "42.0"
    tainted = sup.queue._conn.execute("SELECT tainted FROM tasks WHERE id = ?",
                                      (task_id,)).fetchone()[0]
    assert tainted == 1
    sup.close()
