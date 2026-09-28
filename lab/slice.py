"""End to end: the first vertical slice, issue #39.

Observe a real signal, propose it, route it, draft it, stop. This is
the whole slice ADR 0005 committed to before #32 and #33 become general
services: prove one real path works before generalizing either half of
it.

Deliberately not wired into the supervisor's dispatch loop. That
generalization is #32/#33's job once this slice has actually run
against real data and been checked by a human. Run it directly:

    python -m lab.slice --repo roshanaryal1/home-lab

Every proposal this creates stops at "succeeded" with a draft in its
result. Nothing here ever reaches the publish tier; that boundary
matches issue #15 (secret broker, not built) exactly, so this slice
needs nothing from #15 to be complete.
"""

from __future__ import annotations

import argparse
import logging
from typing import Any

from lab.observe import ObservationError, observe_and_propose
from lab.queue import TaskQueue
from lab.route import draft, route_by_evidence_weight

log = logging.getLogger("lab.slice")


def process_proposals(queue: TaskQueue) -> list[tuple[str, dict[str, Any]]]:
    """Lease every queued proposal, route it, draft it, and stop there.

    Returns (task_id, result) pairs. ``Task`` does not surface the
    ``result`` column (it is written but never read back through the
    dataclass today), so the result is returned directly here rather
    than re-fetched through ``queue.get()``.

    A proposal never runs autonomously in the sense of reaching a
    publish action: the result is a draft, and the task ends at
    "succeeded", not at any state that implies something left the
    machine.
    """
    processed: list[tuple[str, dict[str, Any]]] = []
    while True:
        task = queue.lease(weight="light")
        if task is None:
            break
        token = task.lease
        assert token is not None
        if task.agent_kind != "proposal":
            # Not ours to process; put it back for whatever this queue
            # is really for. Never happens in the slice's own test
            # fixtures, but a shared queue is a shared queue.
            queue.fail(token, "not a proposal task, releasing")
            break

        queue.start(token)
        decision = route_by_evidence_weight(task.payload)
        draft_text = draft(task.payload, decision)
        result = {
            "route": decision.route,
            "evidence": decision.evidence,
            "draft": draft_text,
            "published": False,
        }
        queue.succeed(token, result=result)
        log.info("processed %s -> %s (stopped before publish)",
                 task.id, decision.route)
        processed.append((task.id, result))

    return processed


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True,
                         help="owner/repo to observe, e.g. roshanaryal1/home-lab")
    parser.add_argument("--db", default="lab.db",
                         help="SQLite path (default: lab.db)")
    args = parser.parse_args()

    with TaskQueue(args.db, owner="slice") as queue:
        try:
            proposed = observe_and_propose(queue, args.repo)
        except ObservationError as exc:
            log.error("observation failed: %s", exc)
            raise SystemExit(1) from exc

        print(f"proposed {len(proposed)} new task(s) from {args.repo}")

        processed = process_proposals(queue)
        print(f"drafted {len(processed)} proposal(s), all stopped before publish")

        for task_id, result in processed:
            print(f"\n--- {task_id} ---")
            print(f"route: {result.get('route')}")
            print(result.get("draft", ""))


if __name__ == "__main__":
    main()
