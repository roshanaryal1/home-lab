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
runtime, so it stays open deliberately.

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
