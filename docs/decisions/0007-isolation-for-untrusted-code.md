# ADR 0007: isolation for untrusted code, and what to do about `mach-lookup`

**Status:** accepted for the software side; the measurements are parked
until the Mac mini M6 is on the desk. **Date:** 2026-09-29. Item 4.6
(#71). Settles #27.

## Context

Two facts drive this.

1. `sandbox-exec` and Seatbelt profiles are deprecated by Apple but work
   on macOS 26 and 27, where `lab/sandbox.py` was tested. A future major
   release can remove or change them; the nightly self-test (6.4) exists
   to notice.
2. Apple's open-source [`container`](https://github.com/apple/container)
   tool runs each **Linux** container in its own lightweight VM on Apple
   silicon under macOS 26 or later. It cannot run macOS binaries. It
   gives VM-level isolation and a per-container mount set, at the price
   of a Linux guest and a per-container memory and start-up cost.

The lab has two very different kinds of thing to run, and one mechanism
does not suit both.

## Decision

**Tier by what the code is, not by how nervous we feel.**

| Code | Mechanism | Why |
|---|---|---|
| Our own tools in the broker (`shell.run` on a checked command, file tools, reviewed handlers) | Seatbelt via `lab/sandbox.py`, as today | Native macOS, no guest OS, no start-up cost, runs the same interpreter the lab uses. The code is reviewed and the command is policy-checked; Seatbelt limits blast radius, it is not the only control. |
| Code fetched or generated from untrusted input that must actually be *executed* (a downloaded repository's tests, an agent-written script from a poisoned source) | Disposable Apple container, one per task, network off by default, workspace mounted read-write, nothing else mounted, destroyed after the task | VM boundary instead of a profile that a future macOS may change. The Linux-only limit is acceptable: such code is normally portable, and macOS-specific work is out of scope for untrusted execution. |
| Anything needing the model runtime or Metal | Neither. Runs in the supervisor's trust domain, never from untrusted input | A Linux guest cannot use MLX or Metal, so untrusted code never gets model access. |

Until the container path exists, **untrusted code is not executed at
all**. The broker has no tool that would run it, and this ADR does not
add one. Adding the container executor is a separate item, gated on the
measurement below.

## `mach-lookup` (#27): accept, do not narrow

`(allow mach-lookup)` stays blanket. A sandboxed process can enumerate
accounts and read the operator's home path and shell through
`opendirectoryd`; filesystem confinement is intact and no credentials are
exposed.

Reasons:

- Narrowing to a global-name allowlist would very likely break dyld,
  Python and, later, the model runtime, discovered piecemeal. The model
  runtime is still unchosen (ADR 0001), so narrowing now would be done
  twice.
- The high-risk case, untrusted code, is routed to the container tier,
  where the host's directory service is not reachable at all. What
  remains in Seatbelt is reviewed code, for which host fingerprinting is
  a modest risk on a single-user machine.
- Fewer moving parts in a profile that a future macOS may change anyway.

`SECURITY.md` already states this limit plainly. #27 closes as a
deliberate non-fix. Reopen if untrusted code ever has to run under
Seatbelt, or if the mini becomes multi-user.

## What is not measured yet

Recorded so nobody quotes a number that does not exist:

- Memory overhead and start-up time of one Apple container on the M6.
- Whether a container and the heavy model can be resident together on
  32 GB (ADR 0001 planning figures suggest the heavy slot leaves little
  room; this is the reason containers are per task and short-lived).
- Behaviour of `container` on macOS 27, which the mini runs.

Checklist for the mini is in `ops/mac-mini-setup.md`, section 9.

## Consequences

- No code change in this PR. `lab/sandbox.py` keeps its documented
  limits.
- The rollout ADR (item 4.1) treats "executes untrusted code" as a
  separate, later stage that requires the container tier.
- If the measurement shows a container is too costly next to the heavy
  model, the fallback is to keep refusing untrusted execution rather than
  to run it under Seatbelt.
