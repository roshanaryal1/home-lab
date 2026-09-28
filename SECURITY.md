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
- **Execution broker** (`lab/broker.py`, issue #10). Workers submit typed
  tool requests instead of touching the filesystem directly. Per-task
  workspaces, path confinement by resolved path so symlinks are caught as
  well as `..`, default-deny tool allowlists per task, byte and file-count
  ceilings, and an artifact manifest per execution.
- **Durable task state** with bounded dispatch, lease renewal, atomic
  state transitions (#44), lease tokens checked in the same transaction
  as each write with a generation per claim and a host singleton lock
  (#47, #55, `tests/test_queue.py` item 1.3 section), and durable
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
- Approval consumption does not re-check expiry or bind the exact action
  (#49).
- Workspace path checks race with symlink swaps, and sandboxed commands
  inherit the supervisor's environment (#50).
- Retries do not yet distinguish an unknown remote outcome (#56), and
  broker calls do not yet carry the lease token (item 1.2, #48).
- A worker crash or lease loss does not stop the running handler (#52).

## What does NOT exist yet

Stated plainly, because a security policy implying protections that are
absent is worse than no policy:

- **No network egress control.** Nothing restricts outbound connections.
  There is no network tool yet, so nothing makes them either, but that is
  an absence of opportunity, not a control.
- **No CPU or memory ceilings.** Workspace size and file count are
  capped; compute is not. Arrives with the model adapter.
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

Regression checks: `tests/manual_confirm_defects.py`.
