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
  tiers enforced between leasing a task and executing it. Approvals are
  bound to a hash of the exact normalised parameters, are single use, and
  expire. Everything fails closed.
- **Execution broker** (`lab/broker.py`, issue #10). Workers submit typed
  tool requests instead of touching the filesystem directly. Per-task
  workspaces, path confinement by resolved path so symlinks are caught as
  well as `..`, default-deny tool allowlists per task, byte and file-count
  ceilings, and an artifact manifest per execution.
- **Durable task state** with bounded dispatch, lease renewal and a
  fencing check, so a crash cannot cause duplicate execution.

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
- **No process isolation.** The broker confines filesystem access by
  path. It does not sandbox the process itself. A dedicated non-admin
  account is the deployment-level control and is documented in
  `ops/mac-mini-setup.md`, not enforced in code.

**Do not connect real credentials or a real executor until the three
absences above are closed.**

## Fixed

1. Unbounded task leasing. Measured at 19 leased against 1 running slot;
   now bounded by a worker pool. Issue #7.
2. No lease renewal, permitting duplicate execution of live work.
   Issue #8.
3. Approvals defined but not enforced. Issue #9.
4. Isolation documented but not implemented. Issue #10, filesystem
   portion.

Regression checks: `tests/manual_confirm_defects.py`.
