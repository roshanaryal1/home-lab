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
    python3 -m lab.cli ledger show|review|verify ...
    python3 -m lab.cli memory search|inspect|add-curated|add-evidence|correct|revoke|delete|sweep
    python3 -m lab.cli skills validate|inventory --root DIR
    python3 -m lab.cli skills import <dir> --tier TIER --by NAME [--source TEXT]
    python3 -m lab.cli memory proposals|show-proposal|accept|reject
    python3 -m lab.cli skillstore submit|promote|known-good|rollback|history|install ...
    python3 -m lab.cli prereg m2|m6 [--json]
    python3 -m lab.cli publish list|show <key>|reconcile <key> --connectors FILE
    python3 -m lab.cli route <task-id> [--want post|blog|paper]
    python3 -m lab.cli eval run|rerun ...
    python3 -m lab.cli status [--json] [--since-hours N] [--stall-seconds N]
    python3 -m lab.cli backup [--to DIR] [--artifacts DIR] [--keep N] [--alert-config FILE]
    python3 -m lab.cli heartbeat --url-file FILE
    python3 -m lab.cli restore-check MANIFEST --into DIR
    python3 -m lab.cli drill crash|restore [--log DIR]
    python3 -m lab.cli chat [--once] [--chat-id N]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import unicodedata
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from lab import (
    accountplan,
    alert,
    audit,
    backup,
    control,
    deadman,
    drills,
    emitter,
    keepawake,
    loop,
    metrics,
    publish,
    rubric,
    selftest,
    service,
    skills,
    supervisor,
)
from lab import memory as memory_mod
from lab import model as model_mod
from lab import operator as operator_keys
from lab.artifacts import ArtifactStore
from lab.connectors import ConnectorError, load_connectors
from lab.egress import EgressGateway
from lab.journal import OperationJournal
from lab.ledger import Ledger, LedgerError
from lab.memory import Memory, MemoryRefused
from lab.policy import PolicyEngine, task_intent
from lab.queue import TaskQueue
from lab.skillstore import SkillStore, SkillStoreError
from lab.vault import Vault

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


def cmd_measure_ceilings(args: argparse.Namespace) -> int:
    """Measure every reviewed handler; write the report; exit 1 on any problem."""
    from lab import ceilings
    try:
        extra = [ceilings.parse_handler(h) for h in args.handler]
        report = ceilings.measure(
            args.tasks or ceilings.DEFAULT_TASKS, repeats=args.repeats, headroom=args.headroom,
            extra=extra, include_registered=not args.only_named, max_rss_mb=args.max_rss_mb,
            max_cpu_seconds=args.max_cpu_seconds)
        path = ceilings.save(report, args.out or ceilings.DEFAULT_OUT)
    except (ValueError, OSError) as exc:
        print(f"measure-ceilings: {exc}", file=sys.stderr)
        return 1
    print(ceilings.render(report))
    print(f"wrote {path}")
    return 1 if report.problems else 0


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
    validate.add_argument("--known", action="append", default=[], metavar="NAME",
                          help="a name in use elsewhere, checked for typosquats (repeatable)")
    inventory = skills_sub.add_parser("inventory", help="list skills with content hashes")
    inventory.add_argument("--root", type=Path, required=True)
    inventory.add_argument("--json", action="store_true")
    imp = skills_sub.add_parser(
        "import", help="validate a skill directory and store it as a candidate, never active")
    imp.add_argument("directory", type=Path)
    imp.add_argument("--tier", required=True, choices=["autonomous", "notify",
                                                      "approve", "never"])
    imp.add_argument("--by", required=True, help="who is importing it")
    imp.add_argument("--source", default=None,
                     help="where it came from (a URL, a repo and commit), kept as derived_from")
    imp.add_argument("--store", type=Path, default=None,
                     help="artifact store (default: 'artifacts' next to the database)")

    art = sub.add_parser("artifacts", help="list and verify stored task outputs")
    art.add_argument("--store", type=Path, default=None,
                     help="artifact store (default: 'artifacts' next to the database)")
    art_sub = art.add_subparsers(dest="artifacts_command", required=True)
    art_list = art_sub.add_parser("list", help="the files a task left behind")
    art_list.add_argument("task_id")
    art_sub.add_parser("verify", help="re-hash every stored artifact; exit 1 on any problem")

    bak = sub.add_parser("backup", help="snapshot the database and artifacts (online, consistent)")
    bak.add_argument("--to", type=Path, default=None,
                     help="destination directory (default: $LAB_BACKUP_DIR)")
    bak.add_argument("--artifacts", type=Path, default=None,
                     help="artifact store to copy (default: 'artifacts' next to the database)")
    bak.add_argument("--keep", type=int, default=None, metavar="N",
                     help="restore-check the new backup, then delete all but the newest N "
                     "backups in the folder (only its own files)")
    bak.add_argument("--alert-config", type=Path, default=None,
                     help="operator-owned JSON naming a command to run when the backup fails")
    rc = sub.add_parser("restore-check",
                        help="restore a backup into a fresh directory and verify it")
    rc.add_argument("manifest", type=Path)
    rc.add_argument("--into", type=Path, required=True, help="must not exist or be empty")
    drill = sub.add_parser("drill", help="inject a real failure and log the outcome")
    drill.add_argument("name", choices=["crash", "restore", "model-load"])
    drill.add_argument("--endpoint", default=None,
                       help="model-load: a live loopback server for the wrong-model case")
    drill.add_argument("--log", type=Path, default=Path("ops/drills/log"),
                       help="where the dated record is written (default: ops/drills/log)")

    op = sub.add_parser("operator", help="create the operator's approval signing key")
    op_sub = op.add_subparsers(dest="operator_command", required=True)
    op_init = op_sub.add_parser("init", help="write operator.key (0600) and operator.pub")
    op_init.add_argument("--dir", type=Path, required=True,
                         help="a directory the agent's OS account cannot read")

    sk = sub.add_parser("skillstore", help="versioned skills: submit, promote, roll back, install")
    sk.add_argument("--store", type=Path, default=None)
    sk.add_argument("--operator-pubkey", type=Path, default=None,
                    help="operator public key; with it, promotions must be signed")
    sk_sub = sk.add_subparsers(dest="skillstore_command", required=True)
    sk_submit = sk_sub.add_parser("submit", help="store a skill directory as a candidate version")
    sk_submit.add_argument("directory", type=Path)
    sk_submit.add_argument("--tier", required=True, choices=["autonomous", "notify",
                                                            "approve", "never"])
    sk_submit.add_argument("--by", required=True)
    sk_submit.add_argument("--derived-from", default=None)
    sk_promote = sk_sub.add_parser("promote", help="make a candidate the active version")
    sk_promote.add_argument("version_id", type=int)
    sk_promote.add_argument("--by", required=True)
    sk_promote.add_argument("--signature", default=None)
    sk_promote.add_argument("--allow-loosen", action="store_true")
    sk_good = sk_sub.add_parser("known-good",
                                help="record evidence that the active version is good")
    sk_good.add_argument("version_id", type=int)
    sk_good.add_argument("--by", required=True)
    sk_good.add_argument("--evidence", required=True)
    sk_good.add_argument("--signature", default=None)
    sk_back = sk_sub.add_parser("rollback", help="one step back to the newest known-good version")
    sk_back.add_argument("name")
    sk_back.add_argument("--by", required=True)
    sk_back.add_argument("--signature", default=None)
    sk_back.add_argument("--allow-loosen", action="store_true")
    sk_hist = sk_sub.add_parser("history")
    sk_hist.add_argument("name")
    sk_inst = sk_sub.add_parser("install", help="write the active version to a directory")
    sk_inst.add_argument("name")
    sk_inst.add_argument("--to", type=Path, required=True)

    pub = sub.add_parser("publish", help="credentialed sends: receipts and reconciliation")
    pub_sub = pub.add_subparsers(dest="publish_command", required=True)
    pub_sub.add_parser("list", help="every send and whether it is confirmed")
    p_show = pub_sub.add_parser("show", help="one send, with what was approved")
    p_show.add_argument("key", help="idempotency key or a prefix of at least 6 characters")
    p_rec = pub_sub.add_parser("reconcile", help="ask the provider whether a send happened")
    p_rec.add_argument("key")
    p_rec.add_argument("--connectors", type=Path, required=True,
                       help="the connectors JSON the supervisor uses")
    p_rec.add_argument("--by", default="reconcile")

    mem = sub.add_parser("memory", help="inspect, search, correct, revoke and delete memory")
    mem.add_argument("--store", type=Path, default=None)
    mem_sub = mem.add_subparsers(dest="memory_command", required=True)
    m_search = mem_sub.add_parser("search")
    m_search.add_argument("query")
    m_search.add_argument("--limit", type=int, default=5)
    m_inspect = mem_sub.add_parser("inspect")
    m_inspect.add_argument("id", type=int)
    m_cur = mem_sub.add_parser("add-curated", help="a fact a person promotes")
    m_cur.add_argument("text")
    m_cur.add_argument("--source", required=True)
    m_cur.add_argument("--by", required=True)
    m_ev = mem_sub.add_parser("add-evidence", help="a source-backed, untrusted, expiring note")
    m_ev.add_argument("text")
    m_ev.add_argument("--source", required=True)
    m_ev.add_argument("--sha256", required=True)
    m_ev.add_argument("--by", required=True)
    m_cor = mem_sub.add_parser("correct")
    m_cor.add_argument("id", type=int)
    m_cor.add_argument("text")
    m_cor.add_argument("--by", required=True)
    m_cor.add_argument("--reason", required=True)
    for name in ("revoke", "delete"):
        m_end = mem_sub.add_parser(name)
        m_end.add_argument("id", type=int)
        m_end.add_argument("--by", required=True)
        m_end.add_argument("--reason", required=True)
    mem_sub.add_parser("sweep", help="retire expired memories")
    mem_sub.add_parser("proposals", help="memories tasks proposed, waiting for a decision")
    m_sp = mem_sub.add_parser("show-proposal", help="the exact text, source and taint mark")
    m_sp.add_argument("id", type=int)
    m_acc = mem_sub.add_parser("accept", help="make a proposal curated memory (signed)")
    m_acc.add_argument("id", type=int)
    m_acc.add_argument("--by", required=True)
    m_acc.add_argument("--key", type=Path, default=None,
                       help="operator private key (or $LAB_OPERATOR_KEY); signs the decision")
    m_acc.add_argument("--operator-pubkey", type=Path, default=None,
                       help="operator public key to verify with (or $LAB_OPERATOR_PUBKEY); "
                       "ignored in favour of the deployed key where one is installed")
    m_acc.add_argument("--untrusted-ok", action="store_true",
                       help="required to accept a proposal from a tainted task")
    m_rej = mem_sub.add_parser("reject", help="turn a proposal down (no signature needed)")
    m_rej.add_argument("id", type=int)
    m_rej.add_argument("--by", required=True)
    m_rej.add_argument("--reason", required=True)

    stc = sub.add_parser("selftest", help="verify the audit chain, a backup restore, health and "
                         "the safety tests; alert on failure")
    stc.add_argument("--no-safety-tests", action="store_true")
    stc.add_argument("--tests-dir", type=Path, default=None)
    stc.add_argument("--alert-config", type=Path, default=None,
                     help="operator-owned JSON naming the alert command")
    stc.add_argument("--report-ok", action="store_true",
                     help="with --alert-config, also send one short alert when every check "
                     "passes, so a result arrives every morning")
    sp = sub.add_parser("setup-plan", help="print (or, as root on macOS, apply) the lab-account "
                        "setup")
    sp.add_argument("--user", default="lab")
    sp.add_argument("--operator-pubkey", default=None,
                    help="absolute path of the operator's PUBLIC key to install "
                    "(default: ~/.lab-operator/operator.pub)")
    sp.add_argument("--apply", action="store_true",
                    help="run the mutating steps; needs root and macOS")

    ka = sub.add_parser("keepawake", help="hold the machine awake only while work is pending")
    ka.add_argument("--once", action="store_true", help="print the decision and exit")
    ka.add_argument("--grace", type=float, default=keepawake.DEFAULT_GRACE_SECONDS)
    ka.add_argument("--interval", type=float, default=30.0)

    tk = sub.add_parser("tick", help="one pass of the loop: observe, summarize, route")
    tk.add_argument("--repo", default=None, help="also observe this owner/repo on GitHub")
    tk.add_argument("--mock-reply", default=None,
                    help="scripted model reply, for smoke tests; no model is contacted")
    tk.add_argument("--min-failures", type=int, default=3)
    tk.add_argument("--allow-unsigned", action="store_true",
                    help="run without an operator key (dummy data only)")

    hb = sub.add_parser("heartbeat", help="ping the operator's dead-man switch, only while "
                        "the lab is healthy")
    hb.add_argument("--url-file", type=Path, required=True,
                    help="file holding the ping URL; owned by this user, mode 600")

    wd = sub.add_parser("watchdog", help="kill a supervisor whose heartbeat has gone stale")
    wd.add_argument("--max-age", type=float, default=service.DEFAULT_MAX_AGE,
                    help="seconds without a heartbeat before the supervisor counts as hung")
    wd.add_argument("--dry-run", action="store_true")

    ctl = sub.add_parser("control", help="pause, resume, drain or stop the whole lab")
    ctl.add_argument("action", choices=["show", "pause", "resume", "drain", "stop"])
    ctl.add_argument("--by", default="operator", help="an audit label")
    ctl.add_argument("--reason", default=None)
    ctl.add_argument("--key", default=None,
                     help="operator private key; signs a resume (or LAB_OPERATOR_KEY)")

    cn = sub.add_parser("cancel", help="cancel a task that has not started running")
    cn.add_argument("task_id")
    cn.add_argument("--by", default="operator")
    cn.add_argument("--reason", default="cancelled by operator")

    cht = sub.add_parser("chat", help="poll the paired Telegram chat; messages become tasks")
    cht.add_argument("--chat-id", default=None,
                     help="the paired private chat id (or LAB_CHAT_ID); nothing else is answered")
    cht.add_argument("--once", action="store_true", help="one poll, print what it did, exit")
    cht.add_argument("--interval", type=float, default=1.0,
                     help="seconds between polls, on top of Telegram's long poll")

    em = sub.add_parser("emit", help="queue proposals from patterns in the event log")
    em.add_argument("--min-failures", type=int, default=3,
                    help="failures of one kind and reason before it is proposed")

    ch = sub.add_parser("chain", help="the events that produced a proposal")
    ch.add_argument("task_id")

    rt = sub.add_parser("route", help="route a research task to post, blog, paper or nothing")
    rt.add_argument("task_id")
    rt.add_argument("--want", choices=["post", "blog", "paper"], default=None,
                    help="the route you hoped for; thin evidence is refused upward")
    rt.add_argument("--store", type=Path, default=None)

    dash = sub.add_parser("dashboard", help="serve a read-only status page on loopback")
    dash.add_argument("--host", default="127.0.0.1", help="loopback addresses only")
    dash.add_argument("--port", type=int, default=8765)

    sh = sub.add_parser("shadow", help="measure the rubric on labeled research cases")
    sh.add_argument("--cases", type=Path, required=True)

    led = sub.add_parser("ledger", help="research claims, their evidence and their status")
    led.add_argument("--store", type=Path, default=None,
                     help="artifact store (default: 'artifacts' next to the database)")
    led_sub = led.add_subparsers(dest="ledger_command", required=True)
    led_show = led_sub.add_parser("show", help="claims, evidence and review state of one task")
    led_show.add_argument("task_id")
    led_review = led_sub.add_parser("review", help="run the contradiction pass and missing-"
                                    "evidence list")
    led_review.add_argument("task_id")
    led_review.add_argument("--by", required=True)
    led_verify = led_sub.add_parser("verify", help="sign a supported claim off as verified")
    led_verify.add_argument("claim_id", type=int)
    led_verify.add_argument("--by", required=True)

    ev = sub.add_parser("eval", add_help=False,
                        help="run the fixed task set against a model endpoint, with provenance")
    ev.add_argument("eval_args", nargs=argparse.REMAINDER)

    pr = sub.add_parser("prereg", add_help=False,
                        help="run a pre-registered safety claim against its frozen case file")
    pr.add_argument("prereg_args", nargs=argparse.REMAINDER)

    bn = sub.add_parser("bench", add_help=False,
                        help="benchmark a model endpoint; compare two runs for a tuning gain")
    bn.add_argument("bench_args", nargs=argparse.REMAINDER)

    mc = sub.add_parser("measure-ceilings",
                        help="measure peak memory and CPU of every reviewed handler and "
                             "suggest task ceilings (#180)")
    mc.add_argument("--tasks", type=Path, default=None,
                    help="sample task file (default: evals/ceilings/tasks.json)")
    mc.add_argument("--repeats", type=int, default=5, help="runs of each sample (default 5)")
    mc.add_argument("--headroom", type=float, default=2.0,
                    help="suggested ceiling = largest peak times this (default 2)")
    mc.add_argument("--handler", action="append", default=[], metavar="KIND=REF",
                    help="also measure lab.handlers.module:function as KIND; repeatable")
    mc.add_argument("--only-named", action="store_true",
                    help="measure only --handler handlers, not those register_all registers")
    mc.add_argument("--max-rss-mb", type=int, default=None,
                    help="memory ceiling to measure under (default: the current default)")
    mc.add_argument("--max-cpu-seconds", type=float, default=None,
                    help="CPU ceiling to measure under (default: the current default)")
    mc.add_argument("--out", type=Path, default=None,
                    help="report directory (default: evals/ceilings)")

    st = sub.add_parser("status", help="queue, worker health and counters, from the event log")
    st.add_argument("--json", action="store_true")
    st.add_argument("--since-hours", type=float, default=None,
                    help="count events from the last N hours (default: all time)")
    st.add_argument("--stall-seconds", type=float, default=metrics.DEFAULT_STALL_SECONDS,
                    help="how long work may wait with no worker before it is unhealthy")
    st.add_argument("--alert-config", type=Path, default=None,
                    help="operator-owned JSON naming a command to run when unhealthy")

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


def cmd_setup_plan(args: argparse.Namespace) -> int:
    """Touches no database. Prints the plan; with --apply, runs it (root, macOS only)."""
    try:
        steps = accountplan.build(args.user, operator_pubkey=args.operator_pubkey)
    except ValueError as exc:
        print(f"setup-plan: {exc}", file=sys.stderr)
        return 1
    print(accountplan.render(steps))
    if not args.apply:
        return 0
    try:
        result = accountplan.apply(steps)
    except accountplan.ApplyRefused as exc:
        print(f"setup-plan: {exc}", file=sys.stderr)
        return 1
    if not result.ok and result.failed is not None:
        print(f"setup-plan: stopped at: {result.failed.command}\n{_escape(result.detail)}",
              file=sys.stderr)
        return 1
    print(f"applied {len(result.steps_run)} steps; now run the checks above by hand")
    return 0


def cmd_keepawake(args: argparse.Namespace) -> int:
    """Read-only on the database. On the mini this runs as a LaunchDaemon."""
    if not args.db.exists():
        print(f"No database at {args.db}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.execute("PRAGMA busy_timeout = 5000")
    holder = keepawake.Holder()
    try:
        while True:
            decision = keepawake.decide(conn, args.grace)
            print(f"{'hold' if decision.hold else 'release'}: {decision.reason}", flush=True)
            if args.once:
                return 0
            try:
                holder.apply(decision)
            except OSError as exc:
                print(f"keepawake: cannot start caffeinate: {exc}", file=sys.stderr)
                return 1
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        holder.close()
        conn.close()


def cmd_tick(args: argparse.Namespace) -> int:
    """Uses the model named by LAB_MODEL_URL, LAB_MODEL_NAME and LAB_MODEL_REVISION."""
    if not args.db.exists():
        print(f"No database at {args.db}", file=sys.stderr)
        return 1
    if args.mock_reply is not None:
        spec = model_mod.ModelSpec("mock", "0" * 40, "0" * 40, 8192, 512, 1, heavy=False)
        model: model_mod.BoundedModel | None = model_mod.BoundedModel(
            spec, model_mod.MockAdapter([args.mock_reply]))
    else:
        model = loop.model_from_env(args.db)
    if model is None:
        print("tick: no model configured; set LAB_MODEL_URL, LAB_MODEL_NAME and "
              "LAB_MODEL_REVISION (a loopback server), or pass --mock-reply", file=sys.stderr)
        return 1
    unsigned = args.allow_unsigned or args.mock_reply is not None
    try:
        if unsigned:
            supervisor.refuse_unsigned_when_deployed()
        report = asyncio.run(loop.tick(
            args.db, model, repo=args.repo, min_failures=args.min_failures,
            require_operator_key=not unsigned))
    except (supervisor.AlreadyRunning, supervisor.MissingOperatorKey) as exc:
        print(f"tick: {exc}", file=sys.stderr)
        return 1
    print(f"{report.proposed} proposed, {report.summarized} summarized, {report.routed} routed"
          + ("" if report.ran else " (queue left to the running supervisor)"))
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    """Exit 2 when it cannot start (no pairing, no token, another poller), 0 otherwise."""
    from lab import chat
    from lab.vault import SecretUnavailable

    if not args.db.exists():
        print(f"No database at {args.db}", file=sys.stderr)
        return 1
    raw = args.chat_id or os.environ.get("LAB_CHAT_ID") or ""
    if not re.fullmatch(r"[1-9]\d{0,19}", raw.strip()):
        print("chat: no paired chat; pass --chat-id or set LAB_CHAT_ID to the owner's "
              "private chat id (a positive number)", file=sys.stderr)
        return 2
    try:
        token = Vault().resolve(chat.BOT_SECRET)
    except SecretUnavailable as exc:
        print(f"chat: {exc}; store the chat bot's token as described in SECURITY.md",
              file=sys.stderr)
        return 2
    try:
        with chat.single_poller(args.db), TaskQueue(args.db, owner="chat") as queue:
            def audit_denials(kind: str, detail: dict[str, Any]) -> None:
                # Polls are routine and would flood the chain; refusals are not.
                if kind != "egress_allow":
                    queue.record_event(None, kind, detail)

            channel = chat.ChatChannel(queue, int(raw),
                                       cli=f"python -m lab.cli --db {args.db}")
            transport = chat.TelegramTransport(token, EgressGateway(), audit=audit_denials)
            poller = chat.ChatPoller(channel, transport)
            while True:
                try:
                    outcomes = poller.poll_once()
                except chat.ChatError as exc:
                    print(f"chat: {exc}", file=sys.stderr, flush=True)
                    if args.once:
                        return 1
                    time.sleep(max(args.interval, 5.0))
                    continue
                for outcome in outcomes:
                    print(f"update {outcome.update_id}: {outcome.action.value}"
                          + (f" task {outcome.task_id[:12]}" if outcome.task_id else ""),
                          flush=True)
                if args.once:
                    return 0
                time.sleep(args.interval)
    except chat.ChatError as exc:
        print(f"chat: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0


def cmd_watchdog(args: argparse.Namespace) -> int:
    """Touches no database. Exit 0 healthy or nothing to do, 2 if it killed (or would kill)."""
    verdict = service.check(args.db, max_age=args.max_age, dry_run=args.dry_run)
    age = f" (heartbeat {verdict.age:.0f}s old)" if verdict.age is not None else ""
    print(f"watchdog: {verdict.action}"
          + (f" pid {verdict.pid}" if verdict.pid else "") + age)
    return 2 if verdict.action in ("killed", "would_kill") else 0


def cmd_heartbeat(args: argparse.Namespace) -> int:
    """Read-only on the database. Exit 0 pinged, 2 unhealthy so not pinged, 1 failed.
    The URL is a secret: nothing printed here ever contains it."""
    outcome = deadman.run(args.db, args.url_file)
    print(f"heartbeat: {outcome.message}", file=sys.stdout if outcome.code == 0 else sys.stderr)
    return outcome.code


def cmd_status(args: argparse.Namespace) -> int:
    """Read-only. Exit 0 ok, idle or attention; 2 unhealthy (for a watchdog)."""
    if not args.db.exists():
        print(f"No database at {args.db}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        report = metrics.collect(conn, window_hours=args.since_hours,
                                 stall_seconds=args.stall_seconds)
    except sqlite3.DatabaseError as exc:
        print(f"status: cannot read the database: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(json.dumps(report.as_dict(), indent=2, sort_keys=True) if args.json
          else metrics.render(report))
    if report.health == "unhealthy" and args.alert_config is not None:
        _send_alert(args, "unhealthy", "; ".join(report.reasons) or "the lab is unhealthy")
    return 2 if report.health == "unhealthy" else 0


def _send_alert(args: argparse.Namespace, kind: str, message: str) -> None:
    """Run the operator's hook. A broken hook never changes the exit code."""
    try:
        config = alert.load(args.alert_config)
    except alert.AlertConfigError as exc:
        print(f"alert: {exc}", file=sys.stderr)
        return
    sent = alert.send(config, kind=kind, message=message,
                      state_file=Path(f"{args.db}.alert"))
    print(f"alert: {'sent' if sent else 'not sent (suppressed or the hook failed)'}",
          file=sys.stderr)


def cmd_selftest(args: argparse.Namespace) -> int:
    if args.report_ok and args.alert_config is None:
        print("selftest: --report-ok needs --alert-config", file=sys.stderr)
        return 1
    if not args.db.exists():
        print(f"No database at {args.db}", file=sys.stderr)
        return 1
    report = selftest.run(args.db, tests_dir=args.tests_dir,
                          run_safety_tests=not args.no_safety_tests)
    for check in report.checks:
        print(f"{'ok  ' if check.ok else 'FAIL'} {check.name:<15} {_escape(check.detail)}")
    if args.alert_config is not None:
        if not report.ok:
            _send_alert(args, "selftest", "; ".join(f"{c.name}: {c.detail}"
                                                     for c in report.failures()))
        elif args.report_ok:
            # Its own kind, so a morning "ok" never uses up the window a
            # failure alert needs, and the config's rate limit still applies.
            _send_alert(args, "selftest_ok", f"selftest ok: {len(report.checks)} checks")
    return 0 if report.ok else 1


def _backup_destination(args: argparse.Namespace) -> Path:
    if args.to is not None:
        return Path(args.to)
    env = os.environ.get("LAB_BACKUP_DIR", "")
    if not env:
        raise backup.BackupError("no destination: pass --to or set LAB_BACKUP_DIR")
    if "PASTE_" in env or not os.path.isabs(env):
        raise backup.BackupError("LAB_BACKUP_DIR is still the placeholder or not absolute; "
                                 "set it in the installed service definition")
    return Path(env)


def cmd_scheduled_backup(args: argparse.Namespace) -> int:
    """Back up, prove the new backup restores, then rotate. Alerts on any failure."""
    def failed(message: str) -> int:
        print(f"backup: {message}", file=sys.stderr)
        if args.alert_config is not None:
            _send_alert(args, "backup", message)
        return 1

    if args.keep is not None and args.keep < 1:
        return failed("--keep must be at least 1")
    try:
        dest = _backup_destination(args)
        if args.keep is not None and dest.is_symlink():
            # Rotation deletes files, so it only works in the folder it was given.
            return failed(f"{dest} is a symlink; give the real directory")
        store = args.artifacts if args.artifacts is not None else args.db.parent / "artifacts"
        path = backup.backup(args.db, dest, store if store.exists() else None)
        print(f"wrote {path}")
        if args.keep is None:
            return 0
        with tempfile.TemporaryDirectory(prefix="lab-backup-check-") as tmp:
            report = backup.restore_check(path, Path(tmp) / "restore")
        if not report.ok:
            return failed(f"the new backup {path.name} failed its restore check: "
                          + "; ".join(report.problems))
        print(f"restore check ok: {report.events} audit events, "
              f"{report.artifacts_checked} artifact blobs")
        rotated = backup.rotate(dest, args.keep, protect=path)
    except (backup.BackupError, OSError, sqlite3.DatabaseError) as exc:
        return failed(str(exc))
    print(f"kept {len(rotated.kept)}, removed {len(rotated.removed)} backups and "
          f"{rotated.blobs_removed} artifact blobs")
    for note in rotated.left_alone:
        print(f"left alone: {_escape(note)}")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    """Read-only on the source database; opens no queue and applies no migration."""
    if args.command == "backup":
        return cmd_scheduled_backup(args)
    try:
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
    elif args.name == "model-load":
        results = drills.drill_model_load(args.endpoint)
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
    result = skills.scan(args.root, known_names=getattr(args, "known", ()))
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
        print(_escape(f"{found.skill or '-'}: {found.code}: {found.message}"))
    count = len(result.skills)
    print(f"{count} skill{'s' if count != 1 else ''} checked, {len(result.problems)} problem(s)")
    return 1 if result.problems else 0


def cmd_skills_import(queue: TaskQueue, policy: PolicyEngine, args: argparse.Namespace) -> int:
    """Validate, then store as a candidate. Importing never activates anything:
    the store's typosquat check runs against every name it already holds,
    and only an operator-signed promotion by someone else makes it active."""
    store = SkillStore(queue._conn, ArtifactStore(args.store or args.db.parent / "artifacts",
                                                  queue._conn))
    try:
        vid = store.submit(args.directory, args.tier, _escape(args.by),
                           derived_from=_escape(args.source) if args.source else None)
    except (SkillStoreError, OSError) as exc:
        print(_escape(f"skills import: {exc}"), file=sys.stderr)
        return 1
    row = store.get(vid)
    print(f"imported {row['name']} v{row['version']} as version id {vid} "
          f"(tier {row['tier']}, from {_escape(row['derived_from'] or 'unstated')})")
    print("It is a candidate, not active. It needs a signed promotion by an operator "
          f"other than {_escape(row['submitted_by'])}: lab skillstore promote {vid} "
          "--by NAME --signature SIG")
    return 0


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


def cmd_skillstore(queue: TaskQueue, policy: PolicyEngine, args: argparse.Namespace) -> int:
    key = operator_keys.load_public(args.operator_pubkey) if args.operator_pubkey else None
    store = SkillStore(queue._conn, ArtifactStore(args.store or args.db.parent / "artifacts",
                                                  queue._conn), key)
    cmd = args.skillstore_command
    try:
        if cmd == "submit":
            vid = store.submit(args.directory, args.tier, _escape(args.by),
                               derived_from=args.derived_from)
            row = store.get(vid)
            print(f"{row['name']} v{row['version']} stored as a candidate "
                  f"(tier {row['tier']}, id {vid})")
        elif cmd == "promote":
            store.promote(args.version_id, _escape(args.by), signature=args.signature,
                          allow_loosen=args.allow_loosen)
            print(f"version {args.version_id} is now active")
        elif cmd == "known-good":
            store.mark_known_good(args.version_id, _escape(args.by), args.evidence,
                                  signature=args.signature)
            print(f"version {args.version_id} marked known good")
        elif cmd == "rollback":
            to = store.rollback(args.name, _escape(args.by), signature=args.signature,
                                allow_loosen=args.allow_loosen)
            print(f"{args.name} rolled back to v{to}")
        elif cmd == "history":
            for r in store.history(args.name):
                print(f"v{r['version']:<3} {r['state']:<12} tier {r['tier']:<10} "
                      f"{'known-good ' if r['known_good'] else ''}by {_escape(r['submitted_by'])}"
                      f"  {r['content_sha256'][:12]}")
        else:
            done = store.install(args.name, args.to)
            print(f"installed {done.name} v{done.version} (tier {done.tier}) at {done.path}")
    except (SkillStoreError, operator_keys.OperatorKeyError) as exc:
        print(f"skillstore: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_publish(queue: TaskQueue, policy: PolicyEngine, args: argparse.Namespace) -> int:
    conn = queue._conn
    cmd = args.publish_command
    try:
        if cmd == "list":
            rows = conn.execute("SELECT * FROM publications ORDER BY id").fetchall()
            for r in rows:
                print(f"{r['idempotency_key'][:12]}  {r['state']:<9} {r['connector']:<12} "
                      f"{_escape(r['host'])}{_escape(r['path'])}  "
                      f"provider id {_escape(r['provider_id'] or '-')}")
            print(f"{len(rows)} publication(s)")
            return 0
        if cmd == "show":
            r = publish.find(conn, args.key)
            for key in ("idempotency_key", "state", "task_id", "connector", "host", "method",
                        "path", "body_sha256", "status_code", "provider_id", "response_sha256",
                        "confirmed_via", "created_at", "updated_at"):
                print(f"{key:<16}{_escape(r[key])}")
            return 0
        connectors = load_connectors(args.connectors)
        gateway = EgressGateway(audit=lambda kind, d: policy.audit(d.get("task_id") or None,
                                                                    kind, d))
        result = publish.reconcile(conn, args.key, gateway=gateway, vault=Vault(),
                                   connectors=connectors, journal=OperationJournal(conn),
                                   decided_by=_escape(args.by))
    except (publish.PublishError, ConnectorError) as exc:
        print(f"publish: {exc}", file=sys.stderr)
        return 1
    print(f"{result.outcome.upper()}: {_escape(result.detail)}")
    if result.provider_id:
        print(f"provider id {_escape(result.provider_id)}")
    if result.operation:
        print(f"operation {result.operation[:12]} resolved as happened")
    return 0 if result.outcome == "confirmed" else 2


def cmd_memory(queue: TaskQueue, policy: PolicyEngine, args: argparse.Namespace) -> int:
    memory = Memory(queue._conn)
    ledger = Ledger(queue._conn, ArtifactStore(args.store or args.db.parent / "artifacts",
                                               queue._conn))
    cmd = args.memory_command
    try:
        if cmd == "search":
            hits = memory.search(args.query, limit=args.limit)
            for hit in hits:
                print(f"#{hit.id} [{hit.kind}, {hit.trust}] {_escape(hit.source_id)}: "
                      f"{_escape(hit.excerpt)}")
            print(f"{len(hits)} result(s)")
        elif cmd == "inspect":
            row = memory.inspect(args.id)
            for key in ("kind", "state", "trust", "source_id", "source_sha256", "created_by",
                        "created_at", "expires_at", "embedding_version", "corrected_from",
                        "ended_by", "ended_reason"):
                print(f"{key:<18}{_escape(row[key])}")
            print(f"{'read by tasks':<18}{memory.used_by(args.id)}")
            print(f"text\n  {_escape(row['text']) or '(deleted)'}")
        elif cmd == "add-curated":
            print(f"memory {memory.add_curated(args.text, args.source, args.by)} added")
        elif cmd == "add-evidence":
            print(f"memory {memory.add_evidence(args.text, args.source, args.sha256, args.by)} "
                  "added (untrusted, expires)")
        elif cmd == "correct":
            new_id = memory.correct(args.id, args.text, args.by, args.reason, ledger=ledger)
            print(f"memory {new_id} replaces {args.id}")
        elif cmd == "revoke":
            print(f"revoked: {memory.revoke(args.id, args.by, args.reason, ledger=ledger)}")
        elif cmd == "delete":
            memory.delete(args.id, args.by, args.reason, ledger=ledger)
            print(f"memory {args.id} deleted; the row remains as a tombstone")
        elif cmd == "sweep":
            print(f"{memory.sweep_expired()} expired memory(ies) retired")
        else:
            return _memory_proposal_command(memory, args)
    except (MemoryRefused, LedgerError) as exc:
        print(f"memory: {exc}", file=sys.stderr)
        return 1
    return 0


def _operator_public_key(override: Path | None) -> Ed25519PublicKey:
    """The key an owner decision is verified with.

    Where the deployed key exists (root-owned, outside the lab account's
    reach) it is the only one accepted, so code running as the lab account
    cannot point the check at a key it made itself. Elsewhere (dummy data)
    it is ``--operator-pubkey`` or ``$LAB_OPERATOR_PUBKEY``. No key, no accept.
    """
    deployed = supervisor.DEPLOYED_OPERATOR_KEY
    if deployed.exists():
        if override is not None and override.resolve() != deployed.resolve():
            raise operator_keys.OperatorKeyError(
                f"{deployed} is installed here; decisions verify against it, not {override}")
        return operator_keys.load_public(deployed)
    path = override or os.environ.get("LAB_OPERATOR_PUBKEY")
    if not path:
        raise operator_keys.OperatorKeyError(
            "accepting a proposal needs the operator public key: --operator-pubkey or "
            "$LAB_OPERATOR_PUBKEY")
    return operator_keys.load_public(Path(path))


def _print_proposal(row: sqlite3.Row) -> None:
    for key in ("state", "task_id", "source_id", "source_sha256", "text_sha256", "proposed_at",
                "decided_by", "decided_at", "decision_reason", "memory_id"):
        print(f"{key:<16}{_escape(row[key])}")
    print(f"{'trust':<16}" + ("UNTRUSTED: proposed by a tainted task; its text may be an "
                              "outsider's" if row["tainted"] else "operator task"))
    print(f"reason\n  {_escape(row['reason'])}")
    print(f"text\n  {_escape(row['text'])}")


def _memory_proposal_command(memory: Memory, args: argparse.Namespace) -> int:
    cmd = args.memory_command
    if cmd == "proposals":
        rows = memory.proposals()
        for r in rows:
            mark = "TAINTED" if r["tainted"] else "trusted"
            print(f"#{r['id']} [{mark}] task {_escape(r['task_id'][:12])} "
                  f"{_escape(r['source_id'])}: {_escape(r['text'][:80])}")
        print(f"{len(rows)} pending proposal(s)")
        if rows:
            print("Read one in full before deciding:  memory show-proposal <id>")
        return 0
    row = memory.proposal(args.id)
    if cmd == "show-proposal":
        _print_proposal(row)
        return 0
    if cmd == "reject":
        memory.reject(args.id, _escape(args.by), _escape(args.reason))
        print(f"proposal {args.id} rejected")
        return 0
    if row["tainted"] and not args.untrusted_ok:
        print(f"memory: proposal {args.id} came from a tainted task; read it with "
              "show-proposal and pass --untrusted-ok to accept it anyway", file=sys.stderr)
        return 1
    key_path = args.key or os.environ.get("LAB_OPERATOR_KEY")
    if not key_path:
        print("memory: accepting a proposal needs the operator private key (--key or "
              "$LAB_OPERATOR_KEY)", file=sys.stderr)
        return 1
    by = _escape(args.by)
    try:
        signer = operator_keys.load_private(Path(key_path))
        public = _operator_public_key(args.operator_pubkey)
    except operator_keys.OperatorKeyError as exc:
        print(f"memory: {exc}", file=sys.stderr)
        return 1
    memory_id = memory.accept(args.id, by, memory_mod.sign_acceptance(signer, row, by), public)
    print(f"proposal {args.id} accepted as curated memory {memory_id}, promoted by {by!r}"
          + (" (from a tainted task)" if row["tainted"] else ""))
    return 0


_CONTROL_MODES = {"pause": "paused", "resume": "running", "drain": "draining",
                  "stop": "stopped"}


def cmd_control(queue: TaskQueue, policy: PolicyEngine, args: argparse.Namespace) -> int:
    signer = None
    if args.action == "resume":
        key_path = args.key or os.environ.get("LAB_OPERATOR_KEY")
        if key_path:
            try:
                signer = operator_keys.load_private(Path(key_path))
            except operator_keys.OperatorKeyError as exc:
                print(f"control: {exc}", file=sys.stderr)
                return 1
        else:
            print("warning: no operator key; this resume is UNSIGNED and a supervisor that "
                  "has the operator's public key will ignore it", file=sys.stderr)
    if args.action != "show":
        control.set_mode(queue._conn, _CONTROL_MODES[args.action], by=_escape(args.by),
                         reason=_escape(args.reason) if args.reason else None, signer=signer)
    state = control.get(queue._conn)
    print(f"mode {state.mode}"
          + (f"   set by {_escape(state.set_by)} at {state.set_at}" if state.set_by else "")
          + (", resume signed" if state.signature else ""))
    if state.reason:
        print(f"reason {_escape(state.reason)}")
    return 0


def cmd_cancel(queue: TaskQueue, policy: PolicyEngine, args: argparse.Namespace) -> int:
    rows = queue._conn.execute(
        "SELECT id, state, title FROM tasks WHERE id LIKE ? || '%'", (args.task_id,)).fetchall()
    if len(rows) != 1:
        print(f"cancel: {'no task' if not rows else 'ambiguous prefix'} matching "
              f"{_escape(args.task_id)}", file=sys.stderr)
        return 1
    row = rows[0]
    if row["state"] in ("running", "leased"):
        print(f"cancel: {row['id'][:12]} is {row['state']}; use `control stop` to end running "
              "work", file=sys.stderr)
        return 1
    try:
        queue.cancel(row["id"], f"{_escape(args.reason)} (by {_escape(args.by)})")
    except Exception as exc:
        print(f"cancel: {_escape(exc)}", file=sys.stderr)
        return 1
    print(f"cancelled {row['id'][:12]}: {_escape(row['title'])}")
    return 0


def cmd_emit(queue: TaskQueue, policy: PolicyEngine, args: argparse.Namespace) -> int:
    created = emitter.emit_proposals(queue, min_failures=args.min_failures)
    print(f"{len(created)} proposal(s) queued")
    for task_id in created:
        task = queue.get(task_id)
        print(f"  {task_id}  {_escape(task.title if task else '')}")
    return 0


def cmd_chain(queue: TaskQueue, policy: PolicyEngine, args: argparse.Namespace) -> int:
    links = emitter.chain_for(queue, args.task_id)
    urls = emitter.sources_for(queue, args.task_id)
    if urls:
        print(f"{len(urls)} issue(s) produced this proposal:")
        for url in urls:
            print(f"  {_escape(url)}")
    if not links and not urls:
        print(f"chain: {args.task_id} was not emitted from the event log", file=sys.stderr)
        return 1
    print(f"{len(links)} event(s) produced this proposal:")
    for link in links:
        print(f"  event {link.event_id}  task {link.task_id}  {link.kind}  "
              f"{_escape(link.detail or '')}  {(link.hash or '')[:12]}")
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    from lab import dashboard
    try:
        server = dashboard.make_server(args.db, host=args.host, port=args.port)
    except (dashboard.DashboardError, OSError) as exc:
        print(f"dashboard: {exc}", file=sys.stderr)
        return 1
    host, port = str(server.server_address[0]), server.server_address[1]
    print(f"read-only status page on http://{host}:{port}/  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def cmd_shadow(args: argparse.Namespace) -> int:
    from lab import shadow
    try:
        report = shadow.run(shadow.load_cases(args.cases))
    except (shadow.ShadowError, OSError) as exc:
        print(f"shadow: {exc}", file=sys.stderr)
        return 1
    print(shadow.format_report(report))
    return 0


def cmd_route(queue: TaskQueue, policy: PolicyEngine, args: argparse.Namespace) -> int:
    ledger = Ledger(queue._conn, ArtifactStore(args.store or args.db.parent / "artifacts",
                                               queue._conn))
    try:
        decision = rubric.route_research_task(ledger, args.task_id, args.want)
        text = rubric.draft_from_decision(ledger, args.task_id, decision)
    except LedgerError as exc:
        print(f"route: {exc}", file=sys.stderr)
        return 1
    print(f"route: {decision.route.upper()}"
          + (f"   (asked for {decision.requested}: REFUSED)" if decision.refused_upward else ""))
    for reason in decision.reasons:
        print(f"  - {_escape(reason)}")
    print("evidence chain:")
    for link in decision.chain:
        srcs = ", ".join(f"{_escape(s)} [{t}]" for s, t in link.sources) or "none"
        print(f"  claim {link.claim_id} ({link.kind}, {link.status}): {srcs}")
    print()
    print(text)
    return 0


def cmd_ledger(queue: TaskQueue, policy: PolicyEngine, args: argparse.Namespace) -> int:
    ledger = Ledger(queue._conn, ArtifactStore(args.store or args.db.parent / "artifacts",
                                               queue._conn))
    try:
        if args.ledger_command == "verify":
            ledger.verify(args.claim_id, _escape(args.by))
            print(f"claim {args.claim_id} verified by {_escape(args.by)!r}")
            return 0
        if args.ledger_command == "review":
            state = ledger.run_review_pass(args.task_id, _escape(args.by))
        else:
            task = ledger.research_task(args.task_id)
            print(f"question   {_escape(task['question'])}")
            print(f"protocol   {_escape(task['protocol_version'])}")
            state = ledger.review_state(args.task_id)
            for claim in ledger.claims(args.task_id):
                print(f"\n[{claim.status.upper():<12}] claim {claim.id}: {_escape(claim.text)}")
                for ev in ledger.evidence(claim.id):
                    print(f"    {ev['relation']:<11} snapshot {ev['snapshot_id']} "
                          f"{_escape(ev['source_id'])}: \"{_escape(ev['quote'])}\"")
                if not claim.supports:
                    print("    (no supporting evidence)")
            print()
    except LedgerError as exc:
        print(f"ledger: {exc}", file=sys.stderr)
        return 1
    print(f"reviewable: {'yes' if state.reviewable else 'NO'}"
          + (f"  ({'; '.join(state.reasons)})" if state.reasons else ""))
    if state.missing_evidence:
        print(f"claims with missing evidence: {state.missing_evidence}")
    if state.contradicted:
        print(f"claims with contradicting evidence: {state.contradicted}")
    return 0


COMMANDS = {
    "control": cmd_control,
    "cancel": cmd_cancel,
    "emit": cmd_emit,
    "chain": cmd_chain,
    "skillstore": cmd_skillstore,
    "publish": cmd_publish,
    "memory": cmd_memory,
    "route": cmd_route,
    "ledger": cmd_ledger,
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
    if args.command == "skills" and args.skills_command != "import":
        return cmd_skills(args)
    if args.command == "prereg":
        from lab import prereg
        return prereg.main(args.prereg_args)
    if args.command == "audit":
        return cmd_audit(args)
    if args.command == "operator":
        return cmd_operator(args)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "watchdog":
        return cmd_watchdog(args)
    if args.command == "heartbeat":
        return cmd_heartbeat(args)
    if args.command == "chat":
        return cmd_chat(args)
    if args.command == "tick":
        return cmd_tick(args)
    if args.command == "selftest":
        return cmd_selftest(args)
    if args.command == "keepawake":
        return cmd_keepawake(args)
    if args.command == "shadow":
        return cmd_shadow(args)
    if args.command == "dashboard":
        return cmd_dashboard(args)
    if args.command == "setup-plan":
        return cmd_setup_plan(args)
    if args.command == "measure-ceilings":
        return cmd_measure_ceilings(args)
    if args.command == "bench":
        from lab import bench
        return bench.main(args.bench_args)
    if args.command == "eval":
        from lab import evals
        return evals.main(args.eval_args)
    if args.command in ("backup", "restore-check"):
        return cmd_backup(args)
    if args.command == "drill":
        return cmd_drill(args)
    if not args.db.exists():
        print(f"No database at {args.db}", file=sys.stderr)
        return 1

    with TaskQueue(args.db, owner="cli") as queue:
        policy = PolicyEngine(queue._conn)
        if args.command == "skills":            # only import gets here, see above
            return cmd_skills_import(queue, policy, args)
        return COMMANDS[args.command](queue, policy, args)


if __name__ == "__main__":
    raise SystemExit(main())
