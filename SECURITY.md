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

Out of scope for now, because the component does not exist yet:

- The execution broker, policy enforcement and sandboxing are **not
  implemented** as of 2026-09-25. See "Confirmed defects" in `docs/PLAN.md`.
  Do not deploy this with real credentials until Phase 1 is complete.

## Known unfixed issues

Tracked honestly rather than quietly. As of 2026-09-25:

1. Unbounded task leasing (confirmed, reproducible).
2. No lease renewal, which permits duplicate execution (confirmed,
   reproducible).
3. Approvals exist in the schema and are not enforced.
4. Isolation is documented but not implemented.

Reproduction for 1 and 2: `tests/manual_confirm_defects.py`.
