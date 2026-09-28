"""Command line interface. Closes issue #18.

The approval gate built in #9 had no handle: `grant()` existed as a
Python method and nothing surfaced it, so the only way to approve
anything was to open a REPL and query SQLite by hand. A safety control
nobody can operate is an outage, not a control.

The command that matters most is `show`. Parameter binding exists so a
human approves one specific action after seeing its exact parameters.
If the human never sees them, binding them buys nothing, and approving
becomes rubber-stamping.

Usage:
    python3 -m lab.cli approvals
    python3 -m lab.cli show <id>
    python3 -m lab.cli approve <id> --by roshan [--minutes 15]
    python3 -m lab.cli deny <id> --by roshan [--reason "..."]
    python3 -m lab.cli tasks
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lab.policy import PolicyEngine, task_intent
from lab.queue import TaskQueue

DEFAULT_DB = Path.home() / ".local" / "share" / "home-lab" / "lab.db"

# Anything matching these is redacted in `show`, so approving an action
# never becomes a way to read a credential off the terminal.
SECRET_HINTS = ("password", "token", "secret", "api_key", "apikey",
                "credential", "authorization", "auth", "private_key")


def _redact(params: dict) -> dict:
    """Mask values whose key looks like a credential."""
    out = {}
    for key, value in params.items():
        if any(hint in key.lower() for hint in SECRET_HINTS):
            out[key] = "<redacted>"
        elif isinstance(value, dict):
            out[key] = _redact(value)
        else:
            out[key] = value
    return out


def _age(timestamp: str) -> str:
    """Human-readable age, so a stale request is obvious at a glance.

    Stored timestamps are UTC. Comparing them against a naive local
    `now()` made every request look hours old, by exactly the local
    offset. Compare in UTC.
    """
    try:
        then = datetime.fromisoformat(timestamp).replace(tzinfo=UTC)
    except ValueError:
        return "?"
    seconds = int((datetime.now(UTC) - then).total_seconds())
    if seconds < 0:
        return "0s"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def cmd_approvals(queue: TaskQueue, policy: PolicyEngine, args) -> int:
    pending = policy.pending()
    if not pending:
        print("Nothing waiting for approval.")
        return 0

    print(f"{len(pending)} waiting:\n")
    for row in pending:
        task = queue.get(row["task_id"])
        title = task.title if task else "(task missing)"
        print(f"  {row['id'][:12]}  {_age(row['requested_at']):>4}  {title}")
        print(f"                  {row['reason']}")
    print("\nInspect one before approving:  show <id>")
    return 0


def cmd_show(queue: TaskQueue, policy: PolicyEngine, args) -> int:
    """The important one. Never approve what you have not read."""
    row = queue._conn.execute(
        "SELECT * FROM approvals WHERE id LIKE ?", (args.id + "%",)
    ).fetchone()
    if row is None:
        print(f"No approval matching {args.id!r}", file=sys.stderr)
        return 1

    task = queue.get(row["task_id"])
    if task is None:
        print(f"Approval {row['id']} points at a task that no longer exists",
              file=sys.stderr)
        return 1

    print(f"Approval   {row['id']}")
    print(f"State      {row['state']}")
    print(f"Requested  {row['requested_at']}  ({_age(row['requested_at'])} ago)")
    print(f"Reason     {row['reason']}")
    print()
    print(f"Task       {task.id}")
    print(f"Title      {task.title}")
    print(f"Kind       {task.agent_kind}")
    print(f"Tier       {task.capability_tier}")
    print(f"State      {task.state}")
    print()
    # The stored intent is the object the hash was computed over (item
    # 1.4). Rows from before intents were stored fall back to the task.
    intent = json.loads(row["intent"]) if row["intent"] else task_intent(task)
    print("This approval authorises EXACTLY this intent:")
    print(json.dumps(_redact(intent), indent=2, sort_keys=True))
    print()
    print(f"Bound to   {row['action_hash'][:16]}...")
    print("Changing any parameter, the state it acts on, or the policy "
          "version invalidates this approval.")
    return 0


def cmd_approve(queue: TaskQueue, policy: PolicyEngine, args) -> int:
    row = queue._conn.execute(
        "SELECT id, state FROM approvals WHERE id LIKE ?", (args.id + "%",)
    ).fetchone()
    if row is None:
        print(f"No approval matching {args.id!r}", file=sys.stderr)
        return 1
    if row["state"] != "pending":
        print(f"Approval is already {row['state']}, nothing to do",
              file=sys.stderr)
        return 1

    released = policy.grant(row["id"], decided_by=args.by,
                            valid_for=timedelta(minutes=args.minutes))
    print(f"Granted {row['id'][:12]} for {args.minutes} minutes, by {args.by}")
    if released:
        print(f"Task {released[:12]} returned to the queue and will run.")
    else:
        print("No parked task released; the approval is stored and will be "
              "consumed when the task reaches the gate.")
    return 0


def cmd_deny(queue: TaskQueue, policy: PolicyEngine, args) -> int:
    row = queue._conn.execute(
        "SELECT id, state FROM approvals WHERE id LIKE ?", (args.id + "%",)
    ).fetchone()
    if row is None:
        print(f"No approval matching {args.id!r}", file=sys.stderr)
        return 1
    if row["state"] != "pending":
        print(f"Approval is already {row['state']}, nothing to do",
              file=sys.stderr)
        return 1

    cancelled = policy.deny(row["id"], decided_by=args.by, reason=args.reason)
    print(f"Denied {row['id'][:12]}, by {args.by}")
    if cancelled:
        print(f"Task {cancelled[:12]} cancelled: {args.reason}")
    return 0


def cmd_tasks(queue: TaskQueue, policy: PolicyEngine, args) -> int:
    counts = queue.counts()
    if not counts:
        print("No tasks.")
        return 0
    width = max(len(state) for state in counts)
    for state, n in sorted(counts.items()):
        print(f"  {state:<{width}}  {n}")
    waiting = counts.get("awaiting_approval", 0)
    if waiting:
        print(f"\n{waiting} task(s) parked. Run 'approvals' to decide.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lab", description="Operate the home lab."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help=f"database path (default: {DEFAULT_DB})")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("approvals", help="list what is waiting for a decision")
    sub.add_parser("tasks", help="task counts by state")

    show = sub.add_parser("show", help="inspect one approval before deciding")
    show.add_argument("id", help="approval id, or a unique prefix")

    approve = sub.add_parser("approve", help="grant an approval")
    approve.add_argument("id")
    approve.add_argument("--by", required=True,
                         help="who is approving; recorded, never defaulted")
    approve.add_argument("--minutes", type=int, default=15,
                         help="how long the grant is valid (default 15)")

    deny = sub.add_parser("deny", help="refuse an approval")
    deny.add_argument("id")
    deny.add_argument("--by", required=True)
    deny.add_argument("--reason", default="denied by operator")

    return parser


COMMANDS = {
    "approvals": cmd_approvals,
    "show": cmd_show,
    "approve": cmd_approve,
    "deny": cmd_deny,
    "tasks": cmd_tasks,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.db.exists():
        print(f"No database at {args.db}", file=sys.stderr)
        return 1

    with TaskQueue(args.db, owner="cli") as queue:
        policy = PolicyEngine(queue._conn)
        return COMMANDS[args.command](queue, policy, args)


if __name__ == "__main__":
    raise SystemExit(main())
