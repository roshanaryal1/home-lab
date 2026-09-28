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
from typing import Self

from lab.policy import Decision, PolicyEngine
from lab.queue import LeaseLost, LeaseToken, Task, TaskQueue

log = logging.getLogger("lab.supervisor")

# A handler takes the leased task and returns a JSON-serialisable result.
Handler = Callable[[Task], Awaitable[dict]]


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

    def resolved_owner(self) -> str:
        return self.owner or f"supervisor@{socket.gethostname()}"


@dataclass
class SupervisorStats:
    leased: int = 0
    succeeded: int = 0
    failed: int = 0
    denied: int = 0
    awaiting_approval: int = 0
    recovered: dict[str, int] = field(default_factory=dict)


class Supervisor:
    """Runs tasks from the queue under explicit concurrency limits."""

    def __init__(self, config: SupervisorConfig) -> None:
        self.config = config
        self.queue = TaskQueue(config.db_path, owner=config.resolved_owner())
        self.policy = PolicyEngine(self.queue._conn)
        self.stats = SupervisorStats()
        self._handlers: dict[str, Handler] = {}
        self._stopping = asyncio.Event()
        self._max_tasks: int | None = None

    # --------------------------------------------------------- handlers

    def register(self, agent_kind: str, handler: Handler) -> None:
        self._handlers[agent_kind] = handler

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
        workers = [
            asyncio.create_task(self._worker("heavy"))
            for _ in range(self.config.heavy_slots)
        ] + [
            asyncio.create_task(self._worker("light"))
            for _ in range(self.config.light_slots)
        ]
        await asyncio.gather(*workers, return_exceptions=True)
        return self.stats

    def _may_lease(self) -> bool:
        """Budget check, shared across workers so max_tasks is a total."""
        if self._max_tasks is None:
            return True
        return self.stats.leased < self._max_tasks

    async def _worker(self, weight: str) -> None:
        """One slot. Leases only work it can run, then runs it."""
        while not self._stopping.is_set():
            if not self._may_lease():
                return

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
            await self._run_task(task)

    async def _sleep_or_stop(self, seconds: float) -> None:
        """Sleep, but wake immediately if asked to stop."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)

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
                log.error("lost lease on %s while still running it", token.task_id)
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
        try:
            result = await handler(task)
        except asyncio.CancelledError:
            # Shutdown mid-task. Leave it leased so recovery decides,
            # rather than guessing here whether it is safe to replay.
            raise
        except Exception as exc:
            try:
                self.queue.fail(token, f"{type(exc).__name__}: {exc}")
                self.stats.failed += 1
            except LeaseLost:
                log.error("cannot record failure of %s: lease lost", task.id)
            log.exception("task %s failed", task.id)
        else:
            try:
                self.queue.succeed(token, result)
                self.stats.succeeded += 1
            except LeaseLost:
                # Another supervisor may already have re-run this. Do not
                # write a result we no longer have the right to write.
                log.error("cannot record success of %s: lease lost", task.id)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

    def stop(self) -> None:
        self._stopping.set()

    def close(self) -> None:
        self.queue.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
