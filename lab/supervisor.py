"""Asyncio supervisor: lease, dispatch, enforce concurrency, recover.

Step 2 of the build order. Section 5 of the reference architecture makes
the supervisor own task decomposition, priority, leases, model routing,
concurrency limits, approvals, retries, checkpointing, recovery and
audit events. This module implements the loop and the concurrency and
recovery parts; routing and approvals arrive with steps 3 and 8.

The concurrency limits are the load-bearing constraint on a 32 GB
machine: the study's unanimous finding was roughly one heavy inference
slot, with light work running alongside it. "100 logical agents" does
not mean 100 resident models. Those slots are enforced here with
semaphores rather than left to convention.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

from lab import control
from lab.artifacts import ArtifactError, ArtifactStore
from lab.authority import AgentCapability, AuthorityViolation, check, held_legs
from lab.broker import (
    ApprovalRequired,
    ExecutionBroker,
    ExecutionContext,
    OutcomeUnknown,
    PermanentFailure,
    ToolSession,
)
from lab.connectors import load_connectors
from lab.egress import EgressGateway, parse_allowlist, socket_transport, system_resolver
from lab.journal import OperationJournal
from lab.operator import load_public
from lab.policy import Decision, PolicyEngine
from lab.queue import LeaseLost, LeaseToken, PayloadTooLarge, Task, TaskQueue
from lab.vault import Vault
from lab.worker import check_reference, run_in_worker

log = logging.getLogger("lab.supervisor")

# A handler takes the leased task and a tool session bound to it, and
# returns a JSON-serialisable result. The session is the handler's only
# sanctioned way to touch anything; it cannot name another task.
Handler = Callable[[Task, ToolSession], Awaitable[dict[str, Any]]]


class HandlerError(RuntimeError):
    """Raised by a handler to signal an ordinary, retryable failure."""


class AlreadyRunning(RuntimeError):
    """Another supervisor on this host already holds the database."""


def acquire_singleton(db_path: str | Path) -> int:
    """Take the host-wide supervisor lock for ``db_path``, or refuse.

    Recovery reclaims leases by the stable owner name, which is only safe
    if no other live supervisor shares that name. Two supervisors started
    on one host would, so the second must refuse before it recovers
    anything (item 1.3, #55). flock is released by the kernel when the
    process dies, so a crash never leaves a stale lock behind.

    Returns the open descriptor; pass it to ``release_singleton``.
    """
    lock_path = f"{db_path}.supervisor.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise AlreadyRunning(
            f"another supervisor holds {lock_path}; refusing to start"
        ) from None
    return fd


def release_singleton(fd: int) -> None:
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


@dataclass
class SupervisorConfig:
    db_path: str | Path
    # One heavy inference slot. This is the spec's adjudicated default,
    # not an arbitrary number: a second concurrent heavy model does not
    # fit alongside the first in 32 GB of unified memory.
    heavy_slots: int = 1
    light_slots: int = 3
    lease_ttl_seconds: int = 300
    # How long to sleep when the queue is empty. Short enough to feel
    # responsive, long enough not to spin the CPU on an idle machine
    # that is meant to run 24/7.
    idle_poll_seconds: float = 1.0
    # Renew a lease at this fraction of its TTL. A third leaves two
    # chances to renew before a lease would expire.
    renew_fraction: float = 1 / 3
    # Owner is deliberately STABLE across restarts rather than a fresh
    # random id. Recovery reclaims leases owned by this same name, so a
    # supervisor that crashes and restarts can pick its own work back up
    # immediately instead of waiting out the TTL. A different supervisor
    # still cannot touch it until the lease actually expires.
    owner: str | None = None
    # Where per-task workspaces live. Defaults to a directory next to the
    # database, so a test's tmp_path keeps everything together.
    workspace_root: str | Path | None = None
    # Where the content-addressed artifact store lives (item 3.3). Next to
    # the database by default, so a backup of the directory holds both.
    artifact_root: str | Path | None = None
    # Wall-clock ceiling for one run of a handler (item 1.10, #16). A
    # handler in a worker process is killed with its process group when
    # it is reached; the task is failed with the reason recorded, and
    # retried only under the usual idempotency rule.
    task_timeout_seconds: float = 3600.0
    # Path to the operator's public key (item 4.5). When set, only
    # approvals signed with the matching private key are honoured. Falls
    # back to $LAB_OPERATOR_PUBKEY. Unset means approvals are not
    # signature-checked, which is acceptable only on dummy data.
    operator_public_key: str | Path | None = None
    # JSON list of connector definitions (lab.connectors.load_connectors).
    connectors_file: str | Path | None = None
    # A worker slot that fails this many times in a row stops and marks
    # the supervisor unhealthy, rather than spinning on a broken
    # dependency (item 1.8).
    max_consecutive_worker_errors: int = 5
    # On emergency stop, how long running handlers get to finish after
    # authority is revoked, before they are cancelled and killed.
    stop_grace_seconds: float = 2.0

    def resolved_owner(self) -> str:
        return self.owner or f"supervisor@{socket.gethostname()}"


@dataclass
class SupervisorStats:
    leased: int = 0
    succeeded: int = 0
    failed: int = 0
    denied: int = 0
    awaiting_approval: int = 0
    worker_errors: int = 0
    held_for_review: int = 0
    lease_losses: int = 0
    stopped: int = 0
    recovered: dict[str, int] = field(default_factory=dict)


class Supervisor:
    """Runs tasks from the queue under explicit concurrency limits."""

    def __init__(self, config: SupervisorConfig, *, egress_resolver: Any = system_resolver,
                 egress_transport: Any = socket_transport,
                 vault: Vault | None = None) -> None:
        # The two egress hooks exist so tests can supply a fake resolver
        # and transport. Production uses the defaults.
        self.config = config
        self.queue = TaskQueue(config.db_path, owner=config.resolved_owner())
        pubkey_path = config.operator_public_key or os.environ.get("LAB_OPERATOR_PUBKEY")
        self.policy = PolicyEngine(
            self.queue._conn, load_public(Path(pubkey_path)) if pubkey_path else None)
        if not self.policy.enforces_operator_signatures:
            log.warning("approvals are NOT signature-checked: set operator_public_key "
                        "or LAB_OPERATOR_PUBKEY before connecting real credentials")
        root = config.workspace_root or Path(config.db_path).parent / "workspaces"
        self.journal = OperationJournal(self.queue._conn)
        self.egress = EgressGateway(
            resolver=egress_resolver, transport=egress_transport,
            audit=lambda kind, detail: self.policy.audit(detail.get("task_id") or None,
                                                         kind, detail))
        self.broker = ExecutionBroker(Path(root), policy=self.policy,
                                      leases=self.queue.owns_lease, journal=self.journal,
                                      egress=self.egress, vault=vault or Vault())
        if config.connectors_file:
            for connector in load_connectors(Path(config.connectors_file)).values():
                self.broker.add_connector(connector)
        self.artifacts = ArtifactStore(
            config.artifact_root or Path(config.db_path).parent / "artifacts",
            self.queue._conn)
        self._tools: dict[str, frozenset[str]] = {}
        self._capabilities: dict[str, AgentCapability] = {}
        self._egress_hosts: dict[str, frozenset[str]] = {}
        self._connector_grants: dict[str, frozenset[str]] = {}
        self.stats = SupervisorStats()
        self._handlers: dict[str, Handler] = {}
        self._stopping = asyncio.Event()
        self._max_tasks: int | None = None
        # A handle on every running handler, so lease loss and emergency
        # stop can end the work itself, not only stop recording it.
        self._running: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._interrupted: dict[str, str] = {}
        self.unhealthy_reason: str | None = None

    @property
    def healthy(self) -> bool:
        return self.unhealthy_reason is None

    # --------------------------------------------------------- handlers

    def register(self, agent_kind: str, handler: Handler,
                 tools: frozenset[str] | set[str] = frozenset(), *,
                 sensitive_data: bool = False, external_action: bool = False,
                 egress_hosts: frozenset[str] | set[str] = frozenset(),
                 connectors: frozenset[str] | set[str] = frozenset()) -> None:
        """Register reviewed handler code and the tools it may request.

        The allowlist lives here, in trusted registration, not on the
        task: a task cannot widen the tools its handler is given. The
        two flags declare what the handler can reach beyond its tools
        (a secret, an outside effect) so the Rule of Two can be checked
        before it runs (item 4.1).
        """
        self._handlers[agent_kind] = handler
        self._tools[agent_kind] = frozenset(tools)
        self._capabilities[agent_kind] = AgentCapability(sensitive_data, external_action)
        self._egress_hosts[agent_kind] = parse_allowlist(egress_hosts)
        self._connector_grants[agent_kind] = frozenset(connectors)

    def register_reviewed(self, agent_kind: str, ref: str,
                          tools: frozenset[str] | set[str] = frozenset(), *,
                          sensitive_data: bool = False,
                          external_action: bool = False,
                          egress_hosts: frozenset[str] | set[str] = frozenset(),
                          connectors: frozenset[str] | set[str] = frozenset()) -> None:
        """Register a reviewed handler that runs in its own worker process.

        ``ref`` is ``lab.handlers.<module>:<function>``; anything else is
        refused here, before a task can ever select it. This is the path
        for real work (item 1.2): the handler gets no share of the
        supervisor's memory, environment, database or lease token.
        ``register()`` with an in-process callable remains for tests and
        for code that is part of the supervisor itself.
        """
        check_reference(ref)
        broker = self.broker

        async def in_worker(task: Task, session: ToolSession) -> dict[str, Any]:
            workspace = broker._workspace_for(task.id).root
            return await run_in_worker(ref, task, session, workspace=workspace)

        self.register(agent_kind, in_worker, tools, sensitive_data=sensitive_data,
                      external_action=external_action, egress_hosts=egress_hosts,
                      connectors=connectors)

    def _handler_for(self, task: Task) -> Handler | None:
        return self._handlers.get(task.agent_kind or "", None)

    # ------------------------------------------------------------- loop

    async def run(self, *, max_tasks: int | None = None) -> SupervisorStats:
        """Run a bounded worker pool until stopped or ``max_tasks`` leased.

        One coroutine per slot, each leasing only work of its own weight
        class. The pool size *is* the concurrency bound, so a task is never
        leased unless a worker is already free to run it. The previous
        design leased in a loop and spawned a coroutine per lease, which
        let the queue fill with leased-but-not-started work: measured at 19
        leased against 1 running slot, all of it needlessly exposed to a
        crash.

        ``max_tasks`` exists so tests and one-shot runs terminate; the
        always-on deployment leaves it None and relies on stop().
        """
        lock = acquire_singleton(self.config.db_path)
        try:
            return await self._run_locked(max_tasks)
        finally:
            release_singleton(lock)

    async def _run_locked(self, max_tasks: int | None) -> SupervisorStats:
        self.stats.recovered = self.queue.recover()
        if any(self.stats.recovered.values()):
            log.warning("recovered from unclean shutdown: %s",
                        self.stats.recovered)

        self._max_tasks = max_tasks
        watcher = asyncio.create_task(self._watch_control())
        workers = [
            asyncio.create_task(self._worker("heavy"))
            for _ in range(self.config.heavy_slots)
        ] + [
            asyncio.create_task(self._worker("light"))
            for _ in range(self.config.light_slots)
        ]
        outcomes = await asyncio.gather(*workers, return_exceptions=True)
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        for outcome in outcomes:
            if isinstance(outcome, BaseException) and not isinstance(
                    outcome, asyncio.CancelledError):
                # A worker slot died. Never absorbed silently (R07).
                self.unhealthy_reason = self.unhealthy_reason or (
                    f"worker slot crashed: {type(outcome).__name__}: {outcome}")
                log.error("worker slot crashed: %r", outcome)
        return self.stats

    async def _watch_control(self) -> None:
        """Obey the operator's mode switch (item 6.3).

        ``stopped`` runs the emergency stop once; ``draining`` ends the
        supervisor when nothing is in flight. ``paused`` needs no action
        here: workers simply do not lease.
        """
        while not self._stopping.is_set():
            try:
                mode = control.get(self.queue._conn).mode
            except Exception:
                log.exception("cannot read the control mode")
                mode = "running"
            if mode == "stopped":
                log.error("operator stop requested")
                await self.emergency_stop()
                return
            if mode == "draining" and not any(
                    not w.done() for w in self._running.values()):
                self._stopping.set()
                return
            await self._sleep_or_stop(self.config.idle_poll_seconds)

    def _leasing_paused(self) -> bool:
        try:
            return not control.get(self.queue._conn).leasing_allowed
        except Exception:
            log.exception("cannot read the control mode; not leasing")
            return True

    def _may_lease(self) -> bool:
        """Budget check, shared across workers so max_tasks is a total."""
        if self._max_tasks is None:
            return True
        return self.stats.leased < self._max_tasks

    async def _worker(self, weight: str) -> None:
        """One slot. Leases only work it can run, then runs it."""
        consecutive_errors = 0
        while not self._stopping.is_set():
            if not self._may_lease():
                return
            if self._leasing_paused():
                if self._max_tasks is not None:
                    return          # a one-shot run does not wait out a pause
                await self._sleep_or_stop(self.config.idle_poll_seconds)
                continue

            task = self.queue.lease(
                ttl_seconds=self.config.lease_ttl_seconds, weight=weight
            )
            if task is None:
                if self._max_tasks is not None and self.stats.leased == 0:
                    # One-shot run with nothing to do; do not spin.
                    await self._sleep_or_stop(self.config.idle_poll_seconds)
                    if self.queue.counts().get("queued", 0) == 0:
                        return
                    continue
                if self._max_tasks is not None:
                    return
                await self._sleep_or_stop(self.config.idle_poll_seconds)
                continue

            self.stats.leased += 1
            try:
                await self._run_task(task)
                consecutive_errors = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A supervisor-side error (policy store, audit write, a bug)
                # must not strand the task leased or kill the slot quietly.
                consecutive_errors += 1
                self.stats.worker_errors += 1
                log.exception("worker error on task %s", task.id)
                self._release_after_error(task, exc)
                if consecutive_errors >= self.config.max_consecutive_worker_errors:
                    self.unhealthy_reason = (
                        f"{weight} worker stopped after {consecutive_errors} "
                        f"consecutive errors; last: {type(exc).__name__}: {exc}")
                    log.error("%s", self.unhealthy_reason)
                    return

    async def _sleep_or_stop(self, seconds: float) -> None:
        """Sleep, but wake immediately if asked to stop."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)

    def _release_after_error(self, task: Task, exc: BaseException) -> None:
        token = task.lease
        with contextlib.suppress(Exception):
            self.queue.record_event(task.id, "worker_error",
                                    {"error": f"{type(exc).__name__}: {exc}"})
        if token is None:
            return
        reason = f"supervisor error: {type(exc).__name__}: {exc}"
        try:
            if not self.queue.owns_lease(token):
                return
            current = self.queue.get(task.id)
            if current is not None and current.state == "leased":
                self.queue.release_unstarted(token, reason)
            else:
                self.queue.fail(token, reason)
        except Exception:
            # The database itself may be what failed. The lease then expires
            # on its own and recovery decides, which is the safe default.
            log.exception("could not release %s after a worker error", task.id)

    def _interrupt(self, task_id: str, reason: str) -> None:
        """Cancel a running handler, remembering why."""
        work = self._running.get(task_id)
        if work is not None and not work.done():
            self._interrupted[task_id] = reason
            work.cancel()

    async def emergency_stop(self) -> None:
        """Stop everything now: revoke authority, drain briefly, then kill.

        Revoking comes first so that no tool call made during the grace
        period can have an effect. Handlers still running when the grace
        period ends are cancelled, which kills their worker processes.
        Their tasks stay leased for recovery to decide on restart.
        """
        self._stopping.set()
        self.broker.revoke()
        running = [w for w in self._running.values() if not w.done()]
        if running:
            await asyncio.wait(running, timeout=self.config.stop_grace_seconds)
        for task_id in list(self._running):
            self._interrupt(task_id, "emergency_stop")

    async def _renew_while_running(self, token: LeaseToken) -> None:
        """Heartbeat the lease until cancelled.

        Without this a task that outlives its TTL looks abandoned, and
        another supervisor reclaims and re-runs work that is still in
        progress. That was a measured duplicate-execution bug, not a
        theoretical one.
        """
        ttl = self.config.lease_ttl_seconds
        interval = max(0.05, ttl * self.config.renew_fraction)
        while True:
            await asyncio.sleep(interval)
            if not self.queue.renew_lease(token, ttl_seconds=ttl):
                log.error("lost lease on %s while still running it; stopping it",
                          token.task_id)
                # Stop the work, not only its result (item 1.8). Another
                # worker may already own the task.
                self._interrupt(token.task_id, "lease_lost")
                return

    async def _run_task(self, task: Task) -> None:
        token = task.lease
        assert token is not None, "_run_task needs a Task returned by lease()"
        handler = self._handler_for(task)
        if handler is None:
            # Not retryable: no amount of waiting grows a handler.
            self.queue.cancel(task.id,
                              f"no handler for agent_kind={task.agent_kind!r}")
            self.stats.failed += 1
            log.error("no handler for task %s (kind=%s)",
                      task.id, task.agent_kind)
            return

        # The Rule of Two comes first: a task holding untrusted input, a
        # secret and an outside effect never reaches the approval path,
        # because a human approval of one action does not make the
        # combination safe (item 4.1, ADR 0006).
        try:
            check(held_legs(task.tainted, self._tools.get(task.agent_kind or "", ()),
                            self._capabilities.get(task.agent_kind or "", AgentCapability())))
        except AuthorityViolation as exc:
            self.policy.audit(task.id, "authority_refused", {"reason": str(exc)})
            self.queue.cancel(task.id, f"denied by authority rule: {exc}")
            self.stats.denied += 1
            log.warning("task %s DENIED: %s", task.id, exc)
            return

        # The gate. Between lease and execute, never skipped, and it runs
        # before the handler is given anything. A model's output reaches
        # this code only as parameters to hash, never as instruction.
        verdict = self.policy.authorize(task)
        if not verdict.allowed:
            if verdict.decision is Decision.NEEDS_APPROVAL:
                self.policy.request_approval(task, verdict.reason)
                # Park, do not cancel. Cancelling is terminal, which made
                # granting an approval a silent no-op (issue #19).
                self.queue.park_for_approval(token, verdict.reason)
                self.stats.awaiting_approval += 1
                log.info("task %s parked, %s", task.id, verdict.reason)
            else:
                self.queue.cancel(task.id, f"denied by policy: {verdict.reason}")
                self.stats.denied += 1
                log.warning("task %s DENIED: %s", task.id, verdict.reason)
            return

        try:
            self.queue.start(token)
        except LeaseLost:
            log.error("lease on %s lost before start; not running it", task.id)
            return
        heartbeat = asyncio.create_task(self._renew_while_running(token))
        if task.id not in self.broker._workspaces:
            # Kept across a park, so a resumed task finds its files.
            self.broker.open_workspace(
                task.id, set(self._tools.get(task.agent_kind or "", ())),
                self._egress_hosts.get(task.agent_kind or "", frozenset()),
                self._connector_grants.get(task.agent_kind or "", frozenset()))
        ctx = ExecutionContext(task_id=task.id, agent_kind=task.agent_kind or "",
                               attempt=task.attempts, lease=token)
        ceiling = asyncio.timeout(self.config.task_timeout_seconds)
        work = asyncio.ensure_future(handler(task, self.broker.session(ctx)))
        self._running[task.id] = work
        try:
            async with ceiling:
                result = await work
        except asyncio.CancelledError:
            current = asyncio.current_task()
            reason = self._interrupted.pop(task.id, None)
            if (current is not None and current.cancelling()) or reason is None:
                # Shutdown mid-task. Leave it leased so recovery decides,
                # rather than guessing here whether it is safe to replay.
                raise
            if reason == "lease_lost":
                self.stats.lease_losses += 1
                self.queue.record_event(task.id, "stopped_on_lease_loss",
                                        {"lease_id": token.lease_id})
            else:
                self.stats.stopped += 1
                self.queue.record_event(task.id, "stopped", {"reason": reason})
            log.warning("task %s stopped: %s", task.id, reason)
        except TimeoutError as exc:
            if not ceiling.expired():
                # The handler's own timeout, not ours: an ordinary failure.
                self._record_failure(task, token, exc)
                return
            limit = self.config.task_timeout_seconds
            self.queue.record_event(task.id, "wall_clock_exceeded", {"limit_seconds": limit})
            try:
                self.queue.fail(token, f"wall-clock ceiling of {limit:g}s exceeded")
                self.stats.failed += 1
            except LeaseLost:
                log.error("cannot record timeout of %s: lease lost", task.id)
            log.error("task %s exceeded its %gs wall-clock ceiling", task.id, limit)
        except OutcomeUnknown as exc:
            # A retry reached an operation that may already have happened.
            # Never guess: hold the task until a person reconciles it (1.7).
            try:
                self.queue.hold_for_review(token, str(exc))
                self.stats.held_for_review += 1
            except LeaseLost:
                log.error("cannot hold %s: lease lost", task.id)
            log.warning("task %s held for reconciliation: %s", task.id, exc)
        except PermanentFailure as exc:
            try:
                self.queue.fail(token, f"permanent: {exc}", retry=False)
                self.stats.failed += 1
            except LeaseLost:
                log.error("cannot record failure of %s: lease lost", task.id)
            log.error("task %s failed permanently: %s", task.id, exc)
        except ApprovalRequired as exc:
            # A tool call inside the handler needs a human first (item
            # 1.1). The broker already opened the request for that exact
            # call; park so the lease is released while a human decides.
            # Granting it requeues the task and the call is re-submitted.
            try:
                self.queue.park_for_approval(
                    token, f"approval {exc.approval_id}: {exc}")
                self.stats.awaiting_approval += 1
                log.info("task %s parked on tool approval %s",
                         task.id, exc.approval_id)
            except LeaseLost:
                log.error("cannot park %s: lease lost", task.id)
        except Exception as exc:
            self._record_failure(task, token, exc)
        else:
            try:
                # Outputs are stored and described before success is
                # recorded, so no succeeded task points at a missing file.
                self._ingest_outputs(task)
                self.queue.succeed(token, result)
                self.stats.succeeded += 1
            except ArtifactError as exc:
                self._record_failure(task, token, exc)
            except PayloadTooLarge as exc:
                self._record_failure(task, token, exc)
            except LeaseLost:
                # Another supervisor may already have re-run this. Do not
                # write a result we no longer have the right to write.
                log.error("cannot record success of %s: lease lost", task.id)
        finally:
            self._running.pop(task.id, None)
            if not work.done():
                work.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await work
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            latest = self.queue.get(task.id)
            if latest is not None and latest.state in ("succeeded", "failed", "cancelled"):
                self.broker.close_workspace(task.id)

    def _ingest_outputs(self, task: Task) -> None:
        ws = self.broker._workspaces.get(task.id)
        if ws is None:
            return
        try:
            self.artifacts.ingest_workspace(ws, task.id, task.attempts)
        except OSError as exc:
            raise ArtifactError(f"cannot store outputs: {exc}") from exc

    def _record_failure(self, task: Task, token: LeaseToken, exc: BaseException) -> None:
        try:
            self.queue.fail(token, f"{type(exc).__name__}: {exc}")
            self.stats.failed += 1
        except LeaseLost:
            log.error("cannot record failure of %s: lease lost", task.id)
        log.exception("task %s failed", task.id)

    def stop(self) -> None:
        self._stopping.set()

    def close(self) -> None:
        self.queue.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
