"""Child process for the crash tests in test_races.py. Not a test module.

Two modes, both talking to a real database file:

    crash_child.py op <db> <operation> <kill_point> [non-idempotent]
        Lease (and start) a task, print ``armed``, wait for a line on
        stdin, then run one queue operation with a patched internal that
        SIGKILLs this process part-way through it.

    crash_child.py churn <db> <seed>
        Drive the queue with a random mix of operations until killed.
"""

from __future__ import annotations

import os
import random
import signal
import sys
from datetime import timedelta
from typing import Any

from lab.queue import LeaseLost, TaskQueue, TransitionError

OWNER = "crash-child"


def die(*_args: Any, **_kwargs: Any) -> None:
    os.kill(os.getpid(), signal.SIGKILL)


def run_operation(q: TaskQueue, operation: str, kill_point: str, *,
                  idempotent: bool) -> None:
    task_id = q.add_task("crash target", idempotent=idempotent, max_attempts=3)
    task = q.lease()
    assert task is not None and task.id == task_id and task.lease is not None
    token = task.lease

    if operation != "release_unstarted":
        q.start(token)
    if operation == "resume_after_approval":
        q.park_for_approval(token, "waiting for a human")

    # Patch only after set-up, so only the operation under test can die.
    if kill_point == "release_lease":
        TaskQueue._release_lease = die  # type: ignore[method-assign,assignment]
    elif kill_point == "record":
        TaskQueue._record = die  # type: ignore[method-assign,assignment]
    else:
        raise SystemExit(f"unknown kill point {kill_point!r}")

    print("armed", flush=True)
    sys.stdin.readline()

    if operation == "succeed":
        q.succeed(token, {"ok": True})
    elif operation == "fail":
        q.fail(token, "boom", retry_in=timedelta(0))
    elif operation == "park_for_approval":
        q.park_for_approval(token, "waiting for a human")
    elif operation == "hold_for_review":
        q.hold_for_review(token, "outcome unknown")
    elif operation == "release_unstarted":
        q.release_unstarted(token, "supervisor fault", retry_in=timedelta(0))
    elif operation == "cancel":
        q.cancel(task_id, "operator")
    elif operation == "recover":
        q.recover()
    elif operation == "resume_after_approval":
        q.resume_after_approval(task_id)
    else:
        raise SystemExit(f"unknown operation {operation!r}")
    print("survived", flush=True)


def churn(db: str, seed: int) -> None:
    rng = random.Random(seed)
    q = TaskQueue(db, owner=OWNER)
    print("ready", flush=True)
    ids: list[str] = []
    while True:
        ids.append(q.add_task(f"churn {len(ids)}", idempotent=rng.random() < 0.7,
                              max_attempts=rng.randint(1, 3)))
        task = q.lease(ttl_seconds=3600)
        if task is not None and task.lease is not None:
            token = task.lease
            try:
                if rng.random() < 0.15:
                    q.release_unstarted(token, "churn", retry_in=timedelta(0))
                    continue
                q.start(token)
                pick = rng.random()
                if pick < 0.4:
                    q.succeed(token, {"n": len(ids)})
                elif pick < 0.6:
                    q.fail(token, "churn failure", retry_in=timedelta(0))
                elif pick < 0.7:
                    q.park_for_approval(token, "churn approval")
                elif pick < 0.8:
                    q.hold_for_review(token, "churn hold")
                # else: leave it running, holding its lease
            except (LeaseLost, TransitionError):
                pass
        victim = rng.choice(ids)
        try:
            action = rng.random()
            if action < 0.15:
                q.cancel(victim, "churn cancel")
            elif action < 0.3:
                q.requeue_held(victim)
            elif action < 0.45:
                current = q.get(victim)
                if current is not None and current.state == "awaiting_approval":
                    q.resume_after_approval(victim)
        except TransitionError:
            pass


def main() -> None:
    mode, db = sys.argv[1], sys.argv[2]
    if mode == "op":
        q = TaskQueue(db, owner=OWNER)
        run_operation(q, sys.argv[3], sys.argv[4],
                      idempotent="non-idempotent" not in sys.argv[5:])
    elif mode == "churn":
        churn(db, int(sys.argv[3]))
    else:
        raise SystemExit(f"unknown mode {mode!r}")


if __name__ == "__main__":
    main()
