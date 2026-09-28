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
    python3 -m lab.cli ops
    python3 -m lab.cli resolve <op> --happened|--not-happened --by roshan
    python3 -m lab.cli audit verify
    python3 -m lab.cli audit checkpoint --key KEYFILE --out DIR
    python3 -m lab.cli audit check --key KEYFILE --checkpoint FILE
    python3 -m lab.cli artifacts list <task-id>
    python3 -m lab.cli artifacts verify
    python3 -m lab.cli backup --to DIR [--artifacts DIR]
    python3 -m lab.cli restore-check MANIFEST --into DIR
    python3 -m lab.cli drill crash|restore [--log DIR]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import unicodedata
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from lab import audit, backup, drills, skills
from lab import operator as operator_keys
from lab.artifacts import ArtifactStore
from lab.journal import OperationJournal
from lab.policy import PolicyEngine, task_intent
from lab.queue import TaskQueue

DEFAULT_DB = Path.home() / ".local" / "share" / "home-lab" / "lab.db"

# Anything matching these is redacted in `show`, so approving an action
# never becomes a way to read a credential off the terminal.
SECRET_HINTS = ("password", "token", "secret", "api_key", "apikey",
                "credential", "authorization", "auth", "private_key")


def _redact(params: dict[str, Any]) -> dict[str, Any]:
    """Mask values whose key looks like a credential."""
    out: dict[str, Any] = {}
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


def _escape(text: object) -> str:
    """Make text safe to print: no control character, escape sequence or
    bidi override can rewrite what the operator thinks they are reading."""
    return "".join(
        ch if ch == " " or (ch.isprintable() and unicodedata.category(ch) != "Cf")
        else f"\\u{ord(ch):04x}" if ord(ch) <= 0xFFFF else f"\\U{ord(ch):08x}"
        for ch in str(text)
    )


_HEX_PREFIX = re.compile(r"^[0-9a-f]{4,64}$")


def _find_approval(queue: TaskQueue, prefix: str) -> sqlite3.Row | None:
    """The one approval a prefix names, or None after saying why not.

    A prefix that matches two approvals is refused rather than guessed at,
    so a decision can never land on a different request than the one the
    operator read. SQL wildcards in the argument are not wildcards.
    """
    if not _HEX_PREFIX.match(prefix):
        print("An approval id prefix is 4 to 64 hex characters.", file=sys.stderr)
        return None
    rows = queue._conn.execute(
        "SELECT * FROM approvals WHERE substr(id, 1, ?) = ?", (len(prefix), prefix)
    ).fetchall()
    if not rows:
        print(f"No approval matching {prefix!r}", file=sys.stderr)
        return None
    if len(rows) > 1:
        print(f"{len(rows)} approvals match {prefix!r}; give a longer prefix",
              file=sys.stderr)
        return None
    return rows[0]  # type: ignore[no-any-return]


def cmd_approvals(queue: TaskQueue, policy: PolicyEngine, args: argparse.Namespace) -> int:
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


def cmd_show(queue: TaskQueue, policy: PolicyEngine, args: argparse.Namespace) -> int:
    """The important one. Never approve what you have not read."""
    row = _find_approval(queue, args.id)
    if row is None:
        return 1

    task = queue.get(row["task_id"])
    if task is None:
        print(f"Approval {row['id']} points at a task that no longer exists",
              file=sys.stderr)
        return 1

    print(f"Approval   {row['id']}")
    print(f"State      {row['state']}")
    print(f"Requested  {row['requested_at']}  ({_age(row['requested_at'])} ago)")
    print(f"Reason     {_escape(row['reason'])}")
    print()
    print(f"Task       {task.id}")
    print(f"Title      {_escape(task.title)}")
    print(f"Kind       {task.agent_kind}")
    print(f"Tier       {task.capability_tier}")
    print(f"State      {task.state}")
    print(f"Origin     {task.origin_type} {task.origin_id or ''}".rstrip())
    trust = ("UNTRUSTED INPUT: approving one effect does not make it trusted"
             if task.tainted else "operator")
    print(f"Trust      {trust}")
    print(f"Sensitivity {task.sensitivity}")
    print()
    # The stored intent is the object the hash was computed over (item
    # 1.4). Rows from before intents were stored fall back to the task.
    intent = json.loads(row["intent"]) if row["intent"] else task_intent(task)
    print("This approval authorises EXACTLY this intent:")
    print(json.dumps(_redact(intent), indent=2, sort_keys=True, ensure_ascii=True))
    print()
    print(f"Bound to   {row['action_hash'][:16]}...")
    print("Changing any parameter, the state it acts on, or the policy "
          "version invalidates this approval.")
    return 0


def cmd_approve(queue: TaskQueue, policy: PolicyEngine, args: argparse.Namespace) -> int:
    row = _find_approval(queue, args.id)
    if row is None:
        return 1
    if row["state"] != "pending":
        print(f"Approval is already {row['state']}, nothing to do",
              file=sys.stderr)
        return 1
    if args.expect_hash and not row["action_hash"].startswith(args.expect_hash):
        print("Refusing: the action hash differs from the one you reviewed "
              f"({row['action_hash'][:16]}... vs {args.expect_hash}). Run `show` again.",
              file=sys.stderr)
        return 1

    key = None
    key_path = args.key or os.environ.get("LAB_OPERATOR_KEY")
    if key_path:
        try:
            key = operator_keys.load_private(Path(key_path))
        except operator_keys.OperatorKeyError as exc:
            print(f"approve: {exc}", file=sys.stderr)
            return 1
    else:
        print("warning: no operator key; this approval is UNSIGNED and a supervisor "
              "that enforces operator signatures will ignore it", file=sys.stderr)

    task = queue.get(row["task_id"])
    print(f"Granting {row['id'][:12]}: {_escape(task.title) if task else '?'} "
          f"[hash {row['action_hash'][:16]}]")
    released = policy.grant(row["id"], decided_by=_escape(args.by),
                            valid_for=timedelta(minutes=args.minutes), signer=key)
    print(f"Granted {row['id'][:12]} for {args.minutes} minutes, label {_escape(args.by)!r}"
          f"{', signed' if key else ', UNSIGNED'}")
    if released:
        print(f"Task {released[:12]} returned to the queue and will run.")
    else:
        print("No parked task released; the approval is stored and will be "
              "consumed when the task reaches the gate.")
    return 0


def cmd_deny(queue: TaskQueue, policy: PolicyEngine, args: argparse.Namespace) -> int:
    row = _find_approval(queue, args.id)
    if row is None:
        return 1
    if row["state"] != "pending":
        print(f"Approval is already {row['state']}, nothing to do",
              file=sys.stderr)
        return 1

    cancelled = policy.deny(row["id"], decided_by=_escape(args.by), reason=args.reason)
    print(f"Denied {row['id'][:12]}, label {_escape(args.by)!r}")
    if cancelled:
        print(f"Task {cancelled[:12]} cancelled: {_escape(args.reason)}")
    return 0


def cmd_tasks(queue: TaskQueue, policy: PolicyEngine, args: argparse.Namespace) -> int:
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


def cmd_ops(queue: TaskQueue, policy: PolicyEngine, args: argparse.Namespace) -> int:
    """Operations whose outcome nobody knows (item 1.7)."""
    rows = OperationJournal(queue._conn).unresolved()
    if not rows:
        print("No unresolved operations.")
        return 0
    print(f"{len(rows)} unresolved:\n")
    for row in rows:
        print(f"  {row['id'][:12]}  {row['state']:<9}  {row['tool']:<10}  "
              f"task {row['task_id'][:12]}  {_age(row['started_at'])}")
        if row["error"]:
            print(f"                {row['error']}")
    print("\nCheck whether each really happened, then: "
          "resolve <op> --happened|--not-happened --by <you>")
    return 0


def cmd_resolve(queue: TaskQueue, policy: PolicyEngine, args: argparse.Namespace) -> int:
    journal = OperationJournal(queue._conn)
    matches = [r for r in journal.unresolved() if r["id"].startswith(args.id)]
    if len(matches) != 1:
        print(f"{len(matches)} unresolved operations match {args.id!r}; "
              "give a longer, unique prefix", file=sys.stderr)
        return 1
    op = matches[0]
    task_id = journal.resolve(op["id"], happened=args.happened, decided_by=args.by)
    if task_id is None:
        print("Operation changed state; nothing done", file=sys.stderr)
        return 1
    verdict = "happened: it will not run again" if args.happened else \
        "did not happen: the retry will run it"
    print(f"Resolved {op['id'][:12]} ({verdict}), by {args.by}")
    if not journal.unresolved(task_id) and queue.requeue_held(task_id):
        print(f"Task {task_id[:12]} returned to the queue.")
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
    approve.add_argument("--key", type=Path, default=None,
                         help="operator private key (or $LAB_OPERATOR_KEY); signs the grant")
    approve.add_argument("--expect-hash", default=None,
                         help="action hash prefix you reviewed in `show`; refuse if it differs")
    approve.add_argument("--minutes", type=int, default=15,
                         help="how long the grant is valid (default 15)")

    deny = sub.add_parser("deny", help="refuse an approval")
    deny.add_argument("id")
    deny.add_argument("--by", required=True)
    deny.add_argument("--reason", default="denied by operator")

    sub.add_parser("ops", help="operations whose outcome is unknown")
    resolve = sub.add_parser("resolve", help="reconcile an unknown operation")
    resolve.add_argument("id", help="operation id, or a unique prefix")
    outcome = resolve.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--happened", dest="happened", action="store_true",
                         help="it took effect; never run it again")
    outcome.add_argument("--not-happened", dest="happened", action="store_false",
                         help="it had no effect; the retry may run it")
    resolve.add_argument("--by", required=True)

    skills_cmd = sub.add_parser("skills", help="check and list a skill library (read-only)")
    skills_sub = skills_cmd.add_subparsers(dest="skills_command", required=True)
    validate = skills_sub.add_parser("validate", help="exit 1 if any skill breaks a rule")
    validate.add_argument("--root", type=Path, required=True, help="directory of skill directories")
    inventory = skills_sub.add_parser("inventory", help="list skills with content hashes")
    inventory.add_argument("--root", type=Path, required=True)
    inventory.add_argument("--json", action="store_true")

    art = sub.add_parser("artifacts", help="list and verify stored task outputs")
    art.add_argument("--store", type=Path, default=None,
                     help="artifact store (default: 'artifacts' next to the database)")
    art_sub = art.add_subparsers(dest="artifacts_command", required=True)
    art_list = art_sub.add_parser("list", help="the files a task left behind")
    art_list.add_argument("task_id")
    art_sub.add_parser("verify", help="re-hash every stored artifact; exit 1 on any problem")

    bak = sub.add_parser("backup", help="snapshot the database and artifacts (online, consistent)")
    bak.add_argument("--to", type=Path, required=True, help="destination directory")
    bak.add_argument("--artifacts", type=Path, default=None,
                     help="artifact store to copy (default: 'artifacts' next to the database)")
    rc = sub.add_parser("restore-check",
                        help="restore a backup into a fresh directory and verify it")
    rc.add_argument("manifest", type=Path)
    rc.add_argument("--into", type=Path, required=True, help="must not exist or be empty")
    drill = sub.add_parser("drill", help="inject a real failure and log the outcome")
    drill.add_argument("name", choices=["crash", "restore"])
    drill.add_argument("--log", type=Path, default=Path("ops/drills/log"),
                       help="where the dated record is written (default: ops/drills/log)")

    op = sub.add_parser("operator", help="create the operator's approval signing key")
    op_sub = op.add_subparsers(dest="operator_command", required=True)
    op_init = op_sub.add_parser("init", help="write operator.key (0600) and operator.pub")
    op_init.add_argument("--dir", type=Path, required=True,
                         help="a directory the agent's OS account cannot read")

    audit_cmd = sub.add_parser("audit", help="verify the audit log and its signed checkpoints")
    audit_sub = audit_cmd.add_subparsers(dest="audit_command", required=True)
    audit_sub.add_parser("verify", help="walk the hash chain; exit 1 if it is broken")
    cp = audit_sub.add_parser("checkpoint", help="sign the chain head and write it to a directory")
    cp.add_argument("--key", type=Path, required=True, help="signing key file (16+ bytes)")
    cp.add_argument("--out", type=Path, required=True,
                    help="directory the lab account cannot write to")
    chk = audit_sub.add_parser("check", help="is the live log a continuation of a checkpoint?")
    chk.add_argument("--key", type=Path, required=True)
    chk.add_argument("--checkpoint", type=Path, required=True)

    return parser


def cmd_backup(args: argparse.Namespace) -> int:
    """Read-only on the source database; opens no queue and applies no migration."""
    try:
        if args.command == "backup":
            store = args.artifacts if args.artifacts is not None else args.db.parent / "artifacts"
            path = backup.backup(args.db, args.to, store if store.exists() else None)
            print(f"wrote {path}")
            return 0
        report = backup.restore_check(args.manifest, args.into)
    except backup.BackupError as exc:
        print(f"backup: {exc}", file=sys.stderr)
        return 1
    for problem in report.problems:
        print(f"FAILED: {problem}")
    print(f"{'ok' if report.ok else 'RESTORE FAILED'}: {report.events} audit events, "
          f"{report.artifacts_checked} artifact blobs checked, restored to {report.database}")
    return 0 if report.ok else 1


def cmd_drill(args: argparse.Namespace) -> int:
    if args.name == "crash":
        results = drills.drill_crash()
    else:
        if not args.db.exists():
            print(f"No database at {args.db}", file=sys.stderr)
            return 1
        artifacts_dir = args.db.parent / "artifacts"
        results = [drills.drill_restore(args.db, artifacts_dir if artifacts_dir.exists() else None)]
    for result in results:
        path = drills.record(result, args.log)
        print(f"{'PASS' if result.passed else 'FAIL'} {result.name}: {result.actual}\n  -> {path}")
    if not drills.on_target():
        print("note: not the target machine; logged as a rehearsal, not a demonstration")
    return 0 if all(r.passed for r in results) else 1


def cmd_operator(args: argparse.Namespace) -> int:
    try:
        private, public = operator_keys.generate(args.dir)
    except operator_keys.OperatorKeyError as exc:
        print(f"operator: {exc}", file=sys.stderr)
        return 1
    print(f"private key {private} (keep it where the agent's account cannot read it)")
    print(f"public key  {public} (give this to the supervisor: LAB_OPERATOR_PUBKEY)")
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    """Read-only on the database: it opens no queue and applies no migration."""
    if not args.db.exists():
        print(f"No database at {args.db}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        if args.audit_command == "checkpoint":
            path = audit.write_checkpoint(conn, args.key, args.out)
            print(f"wrote {path}")
            return 0
        if args.audit_command == "check":
            report = audit.check_against_checkpoint(conn, args.checkpoint, args.key)
        else:
            report = audit.verify_chain(conn)
    except audit.CheckpointError as exc:
        print(f"audit: {exc}", file=sys.stderr)
        return 1
    except sqlite3.DatabaseError as exc:
        print(f"audit: cannot read the log: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    if report.ok:
        print(f"ok: {report.events} chained events, {report.unchained} older unchained, "
              f"head {(report.last_hash or '-')[:12]}")
        return 0
    print(f"BROKEN at event {report.bad_id}: {report.problem}")
    return 1


def cmd_skills(args: argparse.Namespace) -> int:
    result = skills.scan(args.root)
    if args.skills_command == "inventory":
        if args.json:
            print(json.dumps([asdict(skill) for skill in result.skills], indent=2))
        else:
            for skill in result.skills:
                print(f"{skill.name}  {skill.sha256[:12]}  files={skill.files}  "
                      f"scripts={len(skill.scripts)}  {skill.path}")
        if result.problems:
            print(f"{len(result.problems)} problem(s); run `lab skills validate`", file=sys.stderr)
        return 0

    for found in result.problems:
        print(f"{found.skill or '-'}: {found.code}: {found.message}")
    count = len(result.skills)
    print(f"{count} skill{'s' if count != 1 else ''} checked, {len(result.problems)} problem(s)")
    return 1 if result.problems else 0


def cmd_artifacts(queue: TaskQueue, policy: PolicyEngine, args: argparse.Namespace) -> int:
    store = ArtifactStore(args.store or args.db.parent / "artifacts", queue._conn)
    if args.artifacts_command == "list":
        rows = store.for_task(args.task_id)
        for r in rows:
            print(f"attempt {r['attempt']}  {r['sha256'][:12]}  {r['size']:>10}  {r['path']}")
        print(f"{len(rows)} artifact(s)")
        return 0
    problems = store.verify_all()
    for p in problems:
        print(f"{p.problem}: {p.sha256[:12]} {p.path} (task {p.task_id})")
    print(f"{len(problems)} problem(s)")
    return 1 if problems else 0


COMMANDS = {
    "artifacts": cmd_artifacts,
    "approvals": cmd_approvals,
    "show": cmd_show,
    "approve": cmd_approve,
    "deny": cmd_deny,
    "tasks": cmd_tasks,
    "ops": cmd_ops,
    "resolve": cmd_resolve,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "skills":
        return cmd_skills(args)
    if args.command == "audit":
        return cmd_audit(args)
    if args.command == "operator":
        return cmd_operator(args)
    if args.command in ("backup", "restore-check"):
        return cmd_backup(args)
    if args.command == "drill":
        return cmd_drill(args)
    if not args.db.exists():
        print(f"No database at {args.db}", file=sys.stderr)
        return 1

    with TaskQueue(args.db, owner="cli") as queue:
        policy = PolicyEngine(queue._conn)
        return COMMANDS[args.command](queue, policy, args)


if __name__ == "__main__":
    raise SystemExit(main())
