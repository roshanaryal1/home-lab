"""Retry only when the outcome is known (item 1.7, #56).

Done when: an operation that succeeds remotely while its response is
lost is reconciled or held after restart, never sent twice; permanent
errors and exhausted budgets stop cleanly.

``sandbox.run`` is replaced by a fake whose side effect is a line in a
counter file, so "how many times did it really happen" is observable.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from lab import sandbox
from lab.broker import (
    ExecutionBroker,
    ExecutionContext,
    OutcomeUnknown,
    PermanentFailure,
    ToolSession,
)
from lab.cli import main as cli
from lab.journal import OperationJournal
from lab.policy import PolicyEngine
from lab.queue import Task, TaskQueue
from lab.supervisor import HandlerError, Supervisor, SupervisorConfig


@pytest.fixture()
def effects(tmp_path, monkeypatch) -> SimpleNamespace:
    """Every real 'send' appends a line to effects.path."""
    counter = tmp_path / "sent.log"
    counter.touch()
    state = {"lose_response": False}

    def fake_run(argv, workspace, **kw):
        with counter.open("a") as fh:
            fh.write(" ".join(argv) + "\n")
        if state["lose_response"]:
            state["lose_response"] = False
            raise ConnectionResetError("response lost after the effect")
        return sandbox.SandboxResult(ok=True, returncode=0, stdout="sent\n", stderr="")

    monkeypatch.setattr(sandbox, "run", fake_run)
    return SimpleNamespace(path=counter, state=state)


def _sent(effects: SimpleNamespace) -> int:
    return len(effects.path.read_text().splitlines())


def _sup(tmp_path: Path) -> Supervisor:
    # Same stable owner as the "crashed" queue below, as after a restart.
    return Supervisor(SupervisorConfig(db_path=tmp_path / "lab.db", idle_poll_seconds=0.01,
                                       owner="sup"))


def _approve_shell(sup: Supervisor) -> None:
    """shell.run is approve tier; these tests are about retries, not approval."""
    sup.broker._authorize = lambda request, ws: None  # type: ignore[method-assign]


async def _run(sup: Supervisor, n: int = 1) -> None:
    sup.stats.leased = 0
    await sup.run(max_tasks=n)


@pytest.mark.asyncio
async def test_a_lost_response_is_held_not_resent(tmp_path, effects) -> None:
    sup = _sup(tmp_path)
    _approve_shell(sup)

    async def send(task: Task, tools: ToolSession) -> dict:
        return {"ok": tools.submit("shell.run", argv=["/bin/echo", "email"]).ok}

    sup.register("mail", send, tools={"shell.run"})
    task_id = sup.queue.add_task("mail", agent_kind="mail", idempotent=True, max_attempts=3)
    effects.state["lose_response"] = True

    await _run(sup)
    assert sup.queue.get(task_id).state == "queued", "the author marked it idempotent"
    await _run(sup)
    task = sup.queue.get(task_id)
    assert task.state == "interrupted" and "may already have run" in task.last_error
    assert _sent(effects) == 1, "sent exactly once"
    (op,) = sup.journal.unresolved(task_id)
    assert op["state"] == "uncertain"
    sup.close()


@pytest.mark.asyncio
async def test_a_crash_mid_operation_is_held_then_reconciled(tmp_path, effects) -> None:
    """The process died after 'executing' was committed. After restart the
    task is held; a person says it happened; the rerun replays, not resends."""
    db = tmp_path / "lab.db"
    with TaskQueue(db, owner="sup") as q:
        task_id = q.add_task("mail", agent_kind="mail", idempotent=True)
        token = q.lease().lease
        q.start(token)
        # What the broker commits just before running the command, then the crash.
        import hashlib

        from lab.journal import operation_id
        from lab.policy import canonical
        params_sha = hashlib.sha256(
            canonical({"argv": ["/bin/echo", "email"]}).encode()).hexdigest()
        OperationJournal(q._conn).begin(operation_id(task_id, "shell.run", params_sha, 0),
                                        task_id, "shell.run", params_sha, 0)
        (tmp_path / "sent.log").write_text("/bin/echo email\n")  # it did happen

    sup = _sup(tmp_path)
    _approve_shell(sup)

    async def send(task: Task, tools: ToolSession) -> dict:
        result = tools.submit("shell.run", argv=["/bin/echo", "email"])
        return {"ok": result.ok, "replayed": result.detail.get("replayed", False)}

    sup.register("mail", send, tools={"shell.run"})
    await _run(sup)       # recovery requeues it (same owner), rerun finds 'executing'
    assert sup.queue.get(task_id).state == "interrupted"
    assert _sent(effects) == 1
    (op,) = sup.journal.unresolved(task_id)
    sup.close()

    assert cli(["--db", str(db), "ops"]) == 0
    assert cli(["--db", str(db), "resolve", op["id"][:12], "--happened", "--by", "roshan"]) == 0

    sup = _sup(tmp_path)
    _approve_shell(sup)
    sup.register("mail", send, tools={"shell.run"})
    await _run(sup)
    task = sup.queue.get(task_id)
    assert task.state == "succeeded"
    assert _sent(effects) == 1, "reconciled as done: never sent again"
    result = sup.queue._conn.execute("SELECT result FROM tasks WHERE id = ?",
                                     (task_id,)).fetchone()[0]
    assert '"replayed": true' in result
    sup.close()


@pytest.mark.asyncio
async def test_reconciled_as_not_happened_runs_again(tmp_path, effects) -> None:
    sup = _sup(tmp_path)
    _approve_shell(sup)

    async def send(task: Task, tools: ToolSession) -> dict:
        return {"ok": tools.submit("shell.run", argv=["/bin/echo", "email"]).ok}

    sup.register("mail", send, tools={"shell.run"})
    task_id = sup.queue.add_task("mail", agent_kind="mail", idempotent=True)
    effects.state["lose_response"] = True
    await _run(sup)
    await _run(sup)
    (op,) = sup.journal.unresolved(task_id)
    assert sup.journal.resolve(op["id"], happened=False, decided_by="roshan") == task_id
    assert sup.queue.requeue_held(task_id)
    await _run(sup)
    assert sup.queue.get(task_id).state == "succeeded"
    assert _sent(effects) == 2, "the person said it had not happened"
    sup.close()


@pytest.mark.asyncio
async def test_a_confirmed_operation_is_replayed_on_retry(tmp_path, effects) -> None:
    sup = _sup(tmp_path)
    _approve_shell(sup)
    runs = {"n": 0}

    async def send_then_flake(task: Task, tools: ToolSession) -> dict:
        runs["n"] += 1
        first = tools.submit("shell.run", argv=["/bin/echo", "email"])
        second = tools.submit("shell.run", argv=["/bin/echo", "email"])  # a 2nd, real send
        if runs["n"] == 1:
            raise HandlerError("transient failure after both sends")
        return {"replayed": [first.detail.get("replayed"), second.detail.get("replayed")]}

    sup.register("mail", send_then_flake, tools={"shell.run"})
    task_id = sup.queue.add_task("mail", agent_kind="mail", idempotent=True)
    await _run(sup)
    assert _sent(effects) == 2, "two identical calls in one run are two operations"
    await _run(sup)
    assert sup.queue.get(task_id).state == "succeeded"
    assert _sent(effects) == 2, "the retry replayed both, sent neither again"
    sup.close()


@pytest.mark.asyncio
async def test_waiting_for_approval_does_not_spend_retries(tmp_path) -> None:
    sup = _sup(tmp_path)
    runs = {"n": 0}

    async def needs_delete(task: Task, tools: ToolSession) -> dict:
        tools.submit("fs.delete", path="x")       # parks until approved
        runs["n"] += 1
        raise HandlerError("transient")

    sup.register("del", needs_delete, tools={"fs.delete"})
    task_id = sup.queue.add_task("del", agent_kind="del", idempotent=True, max_attempts=2)
    await _run(sup)
    (pending,) = sup.policy.pending()
    sup.policy.grant(pending["id"], decided_by="roshan")
    await _run(sup)
    task = sup.queue.get(task_id)
    assert task.attempts == 2 and task.executions == 1
    assert task.state == "queued", "one execution of two spent; parking spent none"
    sup.close()


@pytest.mark.asyncio
async def test_a_permanent_failure_is_not_retried(tmp_path) -> None:
    sup = _sup(tmp_path)

    async def bad(task: Task, tools: ToolSession) -> dict:
        raise PermanentFailure("invalid recipient")

    sup.register("x", bad)
    sup.register_reviewed("y", "lab.handlers.demo:reject_input")
    a = sup.queue.add_task("a", agent_kind="x", idempotent=True, max_attempts=5)
    b = sup.queue.add_task("b", agent_kind="y", idempotent=True, max_attempts=5)
    await _run(sup, 2)
    for task_id, why in ((a, "invalid recipient"), (b, "has no 'note'")):
        task = sup.queue.get(task_id)
        assert task.state == "failed" and "permanent" in task.last_error and why in task.last_error
    sup.close()


@pytest.mark.safety
def test_no_journal_means_no_non_idempotent_call(tmp_path, effects) -> None:
    with TaskQueue(tmp_path / "lab.db") as q:
        q._conn.execute("INSERT INTO tasks (id, title) VALUES ('t1', 't1')")
        task = q.lease()
        ctx = ExecutionContext(task.id, "t", task.attempts, task.lease)
        b = ExecutionBroker(tmp_path / "ws", policy=PolicyEngine(q._conn), leases=q.owns_lease)
        b._authorize = lambda request, ws: None  # type: ignore[method-assign]
        b.open_workspace("t1", {"shell.run", "fs.write"})
        tools = b.session(ctx)
        assert tools.submit("fs.write", path="a", content="x").ok, "idempotent: no journal needed"
        result = tools.submit("shell.run", argv=["/bin/echo", "x"])
        assert not result.ok and "no operation journal" in result.error
    assert _sent(effects) == 0


@pytest.mark.asyncio
async def test_a_command_cut_off_mid_run_is_recorded_uncertain(tmp_path, monkeypatch) -> None:
    started = asyncio.Event()
    loop = asyncio.get_running_loop()

    def slow_run(argv, workspace, **kw):
        loop.call_soon_threadsafe(started.set)
        import time
        time.sleep(0.5)
        return sandbox.SandboxResult(ok=True, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox, "run", slow_run)
    with TaskQueue(tmp_path / "lab.db") as q:
        q._conn.execute("INSERT INTO tasks (id, title) VALUES ('t1', 't1')")
        task = q.lease()
        journal = OperationJournal(q._conn)
        b = ExecutionBroker(tmp_path / "ws", policy=PolicyEngine(q._conn),
                            leases=q.owns_lease, journal=journal)
        b._authorize = lambda request, ws: None  # type: ignore[method-assign]
        b.open_workspace("t1", {"shell.run"})
        tools = b.session(ExecutionContext("t1", "t", 1, task.lease))
        call = asyncio.create_task(tools.submit_async("shell.run", argv=["/bin/echo"]))
        await started.wait()
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        (op,) = journal.unresolved("t1")
        assert op["state"] == "uncertain" and "CancelledError" in op["error"]
        # The retry: a new claim, so the same call maps to the same operation.
        from lab.queue import LeaseToken
        b._check_context = lambda ctx: None  # type: ignore[method-assign]
        retry = ExecutionContext("t1", "t", 2, LeaseToken("t1", "next-claim", 2, "x"))
        with pytest.raises(OutcomeUnknown):
            b.session(retry).submit("shell.run", argv=["/bin/echo"])
        await asyncio.sleep(0.6)   # let the worker thread finish before teardown
