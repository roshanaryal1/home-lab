# Security policy

## What this project is

An always-on agent platform that runs on a personal machine with access to a
filesystem, a shell and network egress. The threat model is not incidental to
the project; it is most of the project. Read `docs/PLAN.md` for the design
rationale and `ops/mac-mini-setup.md` for the deployment boundary.

## Reporting a vulnerability

Open a private security advisory through GitHub's "Report a vulnerability"
button on the Security tab, or email <roshanaryaal@gmail.com>. Please do not
open a public issue for anything exploitable.

Expect an acknowledgement within 7 days.

## Repository controls

- `main` is protected: pull requests only, linear history, resolved
  conversations, and the `test`, `workflow-audit` and `secret-scan` CI jobs
  must pass.
- CI actions are pinned to commit SHAs and audited by zizmor.
- Dependabot alerts and security updates are on for `uv.lock` and actions.
- The repository is private, so GitHub secret scanning, push protection
  and CodeQL are not available without Advanced Security. gitleaks scans
  the full history in CI instead. It runs after a push, so it detects a
  leaked secret rather than preventing it.

## Scope

In scope, and taken seriously:

- Sandbox or workspace escape from a task workspace.
- Bypassing the approval gate, replaying or forging an approval token.
- Prompt injection that causes an unauthorized tool call, credential access or
  data egress.
- Privilege escalation from the agent account.
- Any path by which a model output becomes an executed instruction without
  passing policy.

The risk-by-risk mapping to the OWASP agentic top 10, with the control, its
test and the open issue for each gap, is in [THREATS.md](THREATS.md).

## What exists, as of 2026-09-25

Implemented and tested:

- **Policy enforcement** (`lab/policy.py`, issue #9). Four capability
  tiers enforced **per task**, between leasing it and executing it.
  Approvals are bound to a hash of the task's normalised parameters, are
  single use, and expire. **Per tool call** as well (item 1.1, #43): the
  broker takes each tool's tier from a trusted registry and asks policy
  before every call; an approve-tier call needs an approval for that
  exact tool and arguments, and without a policy engine or an audit
  record nothing runs (`tests/test_broker.py`, item 1.1 section).
  Each approval binds an immutable intent (task, tool, arguments, the
  workspace state it acts on, policy version), stored and shown in
  `lab.cli show`; it is consumed by one UPDATE that re-checks grant,
  expiry and hash, so a changed file, a new policy version or an expiry
  at the moment of use voids it (item 1.4, R08).
- **Execution broker** (`lab/broker.py`, issue #10). Workers submit typed
  tool requests instead of touching the filesystem directly. Private
  (0700) per-task workspaces; file tools walk paths by descriptor with
  O_NOFOLLOW in every component, so a symlink, or a directory swapped for
  one, is refused at the moment of use (item 1.5, R09); git hooks and
  shell start-up files cannot be written or deleted; the Seatbelt
  profile is passed inline and never written into the workspace (R10);
  default-deny tool allowlists per task, byte and file-count ceilings,
  and an artifact manifest per execution.
- **Durable task state** with bounded dispatch, lease renewal, atomic
  state transitions (#44), lease tokens checked in the same transaction
  as each write with a generation per claim and a host singleton lock
  (#47, #55, `tests/test_queue.py` item 1.3 section); lease loss or an
  emergency stop cancels the running handler and kills its worker
  process group, the stop revoking broker authority first, and a
  supervisor-side error releases the task instead of stranding it
  (item 1.8, `tests/test_stopping.py`); non-idempotent tool calls are
  journaled before they run, so a retry replays a confirmed outcome and
  holds an unknown one for a person to reconcile (`lab.cli ops`,
  `resolve`) instead of repeating it, and retry budgets count executions,
  not approval waits (item 1.7, `tests/test_journal.py`); and durable
  commits on a SQLite without the WAL-reset bug (item 1.9,
  `tests/test_durability.py`).
- **Audit log** (`lab/audit.py`, item 3.2, #65). `events` refuses UPDATE
  and DELETE by trigger, no longer cascades from task deletion (a task
  with leases or approvals cannot be deleted at all), and every row
  carries a SHA-256 chain over its content and the previous row. Every
  broker call writes one `broker_call` event: tool, parameter hash (never
  the parameters), lease generation, decision and result. `lab audit
  checkpoint` signs the chain head with HMAC-SHA256 under a key file and
  writes it to a directory; `lab audit check` then proves the live log
  still contains the checkpointed row, which catches a rewrite of the
  whole chain that a bare chain cannot (`tests/test_audit.py`). Limits,
  stated plainly: triggers and chain stop accidents and edits, not
  someone who can drop the trigger and also reach the key and the
  checkpoint directory. Until the separate operator account exists
  (#70) the key and checkpoints must be kept off the lab account by
  hand. Events from before migration 3 are kept but not chained.
- **Content-addressed artifacts** (`lab/artifacts.py`, item 3.3, #66).
  Before a task can be recorded as succeeded, every regular file in its
  workspace is copied into an immutable store keyed by SHA-256 (temp file,
  fsync, rename to a read-only blob, deduplicated) and described in
  `artifacts` (path, hash, size, type, task, attempt, producing build,
  lineage). Symlinks, devices, sockets and FIFOs are found by `lstat`,
  opened only with `O_NOFOLLOW` and re-checked with `fstat`, so a file
  swapped for a link between listing and reading reads nothing; they are
  refused, audited, and do not fail the task. A file over 64 MiB, more
  than 2000 files, or any I/O error fails the task instead of dropping
  output silently. `lab artifacts verify` re-hashes every blob and is what
  a restore drill runs (`tests/test_artifacts.py`). Artifacts are not yet
  encrypted or size-quota'd across tasks.
- **Rule of Two** (`lab/authority.py`, item 4.1, #68, ADR 0006). A task
  that would hold untrusted input, a secret and an external action at
  once is cancelled before its handler runs, with no approval offered.
  The secret and external legs come from trusted registration and a
  fixed tool table (an unclassified tool counts as external); the
  untrusted leg is true unless the task carries an operator mark, which
  is only as strong as the queue's write access until origin tracking
  lands (#69). No external tool or secret exists yet, so the rule is
  proven by a deliberate test, not yet by production traffic.

## Known gaps in what exists

Reproduced by an independent review on 2026-09-28 and tracked, not fixed
yet. Until these close, run the lab only with dummy data and review
every draft by hand.

- Handlers registered with `register_reviewed` run in their own worker
  process with a minimal environment, no database path and no lease
  token, and act only through the broker (item 1.2). They still run as
  the same OS user, so a hostile handler that found the database file
  could open it; the separate lab account closes that (#70). Only code
  under `lab.handlers` can be loaded into a worker.

## What does NOT exist yet

Stated plainly, because a security policy implying protections that are
absent is worse than no policy:

- **No network egress control.** Nothing restricts outbound connections.
  There is no network tool yet, so nothing makes them either, but that is
  an absence of opportunity, not a control.
- **No memory ceiling per task.** Commands get only PATH, HOME, TMPDIR
  and LANG; parameters are schema-checked; timeouts are clamped to 300 s;
  output is capped at 256 KiB per stream; the command's whole process
  group is killed at the deadline and on exit; tool calls, child tasks
  and each run's wall-clock time are capped (item 1.10,
  `tests/test_limits.py`). Memory is not, and arrives with the model
  adapter (#16, #74). A fork bomb inside the deadline is contained by the
  group kill, not prevented.
- **No secret broker.** There is nowhere to inject secrets yet, so there
  is nothing to leak, but credential handling is unimplemented.
**Do not connect real credentials until the three absences above are
closed.**

## Process isolation

Implemented (issue #17). Anything that executes code runs under a macOS
Seatbelt profile via `sandbox-exec`, applied by `lab/sandbox.py`. The
kernel intercepts file opens, network connects and forks at the syscall
boundary, and **child processes inherit the restrictions**, so a
subprocess cannot escape by spawning another.

If OS-level isolation is unavailable, execution is **refused** rather
than downgraded. A caller that asked for confinement and silently did not
get it would be trusted with work it cannot safely run.

### What the sandbox does NOT stop

**Local account enumeration.** Measured on macOS 27 with `/etc` and
`/private/etc` fully denied:

```
/usr/bin/id -un                    -> the operator's username
/usr/bin/dscl . -list /Users       -> all 133 accounts
/usr/bin/dscl . -read /Users/<u>   -> home directory and shell
```

These reach `opendirectoryd` over a mach port, not through
`/etc/passwd`, so denying `/etc` does not affect them. Narrowing
`mach-lookup` would very likely break dyld, Python and the model
runtime, so it stays open deliberately, a decision recorded in
[ADR 0007](docs/decisions/0007-isolation-for-untrusted-code.md) (#27).
Untrusted code is not executed under Seatbelt at all; when it must be, it
goes in a disposable Apple container (Linux guest, VM boundary), which is
not built yet.

Filesystem confinement itself is intact: `ls /Users` is refused. A
sandboxed agent cannot walk home directories, but it **can** enumerate
every account on the machine and learn the operator's home path and
shell.

**Do not describe this sandbox as preventing username or home-directory
disclosure.** It does not.

Known limitation: `sandbox-exec` is deprecated by Apple. It remains
functional, macOS's own daemons use Seatbelt internally, and Apple has
published no replacement covering headless process sandboxing, since App
Sandbox requires code signing and an Xcode project. Tracked as a risk
with Apple's container framework as the fallback if it is ever removed.

## Fixed

1. Unbounded task leasing. Measured at 19 leased against 1 running slot;
   now bounded by a worker pool. Issue #7.
2. No lease renewal, permitting duplicate execution of live work.
   Issue #8.
3. Approvals defined but not enforced. Issue #9.
4. Isolation documented but not implemented. Issue #10, filesystem
   portion; issue #17, process isolation.

5. `recover()` acted on a scan taken before its transaction began, so it
   could raise on a task that had just finished, or release a lease
   another supervisor had taken in the meantime. It now re-checks each
   task inside the transaction that changes it. Found by the race tests
   in `tests/test_races.py` (item 2.4, #61).
6. `resume_after_approval` changed the state and wrote its audit event as
   two separate commits, so a crash between them left a transition with
   no event. It now runs in one transaction.

Regression checks: `tests/manual_confirm_defects.py`.
