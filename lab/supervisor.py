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
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self

from lab.queue import Task, TaskQueue

log = logging.getLogger("lab.supervisor")

# A handler takes the leased task and returns a JSON-serialisable result.
Handler = Callable[[Task], Awaitable[dict]]


class HandlerError(RuntimeError):
    """Raised by a handler to signal an ordinary, retryable failure."""


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
    owner: str | None = None


@dataclass
class SupervisorStats:
    leased: int = 0
    succeeded: int = 0
    failed: int = 0
    recovered: dict[str, int] = field(default_factory=dict)


class Supervisor:
    """Runs tasks from the queue under explicit concurrency limits."""

    def __init__(self, config: SupervisorConfig) -> None:
        self.config = config
        self.queue = TaskQueue(config.db_path, owner=config.owner)
        self.stats = SupervisorStats()
        self._handlers: dict[str, Handler] = {}
        self._heavy = asyncio.Semaphore(config.heavy_slots)
        self._light = asyncio.Semaphore(config.light_slots)
        self._stopping = asyncio.Event()
        self._in_flight: set[asyncio.Task] = set()

    # --------------------------------------------------------- handlers

    def register(self, agent_kind: str, handler: Handler) -> None:
        self._handlers[agent_kind] = handler

    def _handler_for(self, task: Task) -> Handler | None:
        return self._handlers.get(task.agent_kind or "", None)

    def _slot_for(self, task: Task) -> asyncio.Semaphore:
        """Heavy tasks contend for the single inference slot."""
        weight = task.payload.get("weight", "light")
        return self._heavy if weight == "heavy" else self._light

    # ------------------------------------------------------------- loop

    async def run(self, *, max_tasks: int | None = None) -> SupervisorStats:
        """Run until stopped, or until ``max_tasks`` have been leased.

        ``max_tasks`` exists so tests and one-shot runs terminate; the
        always-on deployment leaves it None and relies on stop().
        """
        self.stats.recovered = self.queue.recover()
        if any(self.stats.recovered.values()):
            log.warning("recovered from unclean shutdown: %s",
                        self.stats.recovered)

        while not self._stopping.is_set():
            if max_tasks is not None and self.stats.leased >= max_tasks:
                break

            task = self.queue.lease(ttl_seconds=self.config.lease_ttl_seconds)
            if task is None:
                if max_tasks is not None and not self._in_flight:
                    break
                await self._sleep_or_stop(self.config.idle_poll_seconds)
                continue

            self.stats.leased += 1
            runner = asyncio.create_task(self._run_task(task))
            self._in_flight.add(runner)
            runner.add_done_callback(self._in_flight.discard)

        if self._in_flight:
            await asyncio.gather(*self._in_flight, return_exceptions=True)
        return self.stats

    async def _sleep_or_stop(self, seconds: float) -> None:
        """Sleep, but wake immediately if asked to stop."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)

    async def _run_task(self, task: Task) -> None:
        handler = self._handler_for(task)
        if handler is None:
            # Not retryable: no amount of waiting grows a handler.
            self.queue.cancel(task.id,
                              f"no handler for agent_kind={task.agent_kind!r}")
            self.stats.failed += 1
            log.error("no handler for task %s (kind=%s)",
                      task.id, task.agent_kind)
            return

        async with self._slot_for(task):
            self.queue.start(task.id)
            try:
                result = await handler(task)
            except asyncio.CancelledError:
                # Shutdown mid-task. Leave it leased so recovery decides,
                # rather than guessing here whether it is safe to replay.
                raise
            except Exception as exc:
                self.queue.fail(task.id, f"{type(exc).__name__}: {exc}")
                self.stats.failed += 1
                log.exception("task %s failed", task.id)
            else:
                self.queue.succeed(task.id, result)
                self.stats.succeeded += 1

    def stop(self) -> None:
        self._stopping.set()

    def close(self) -> None:
        self.queue.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
