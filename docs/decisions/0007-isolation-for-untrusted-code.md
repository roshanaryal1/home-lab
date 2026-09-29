# ADR 0007: isolation for untrusted code, and what to do about `mach-lookup`

**Status:** accepted for the software side; start-up, memory and
isolation measured on the M6, coexistence with the heavy model still
open. **Date:** 2026-09-29. Item 4.6
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

## Measured on the M6 (2026-09-29)

Machine: Mac mini, Apple M6, 32 GB, macOS 27.0 (26A428). Tool: Apple
`container` 1.5.0 (release, commit `d265d66`), installed from the signed,
notarized `container-1.5.0-installer-signed.pkg`; default kernel installed
by `container system start --enable-kernel-install`. Containers ran with
the defaults: 4 CPUs, 1024 MB.

**Start-up.** 20 runs of `container run --rm alpine:3.22 true`, wall clock
from the CLI, image already pulled, 0 failures:

| | seconds |
|---|---|
| median | 0.637 |
| worst | 17.377 (run 1, the first container ever started after install) |
| worst of runs 2 to 20 | 0.706 |
| best | 0.594 |

**Memory.** Resident memory of the host's
`com.apple.Virtualization.VirtualMachine` process for the container:

| state | RSS (MiB) |
|---|---|
| alpine, idle (`sleep`), 10 samples over 30 s | 381 |
| python:3.13-slim, idle, fresh | 379 |
| after installing the lab's dependencies in the guest | 1187 |
| during the lab's test suite, mean / peak (66 samples, 1 s) | 1716 / 1980 |
| 20 s after the suite finished | 1989 |
| after `container stop` and `rm` | process gone |

The host process grew to nearly twice the 1024 MB guest size (guest
`MemTotal` 1101 MiB) and did not shrink while the container lived. Budget
roughly 2 GiB of host memory per busy container, not the configured guest
size. Stopping the container returns it. Host swap stayed at 0 throughout.

The suite inside the guest: 1009 passed, 10 failed, 26 skipped. The 10
failures need `ps`/process groups or a git checkout, which the slim image
and the exported workspace do not have; they are not isolation faults.
With the image's own Python (SQLite 3.46.1) the queue refuses to open, as
`lab/queue.py` intends; the run above used uv's managed Python (SQLite
3.53.1).

**Isolation.** From inside a container:

- `getent passwd` lists only the image's own accounts; the host operator
  account is not present. The host directory service is not reachable.
- With `-v <workspace>:/work`, the only host-backed mount is `/work`
  (virtiofs); the root is the guest's own ext4 disk and `/Users` does not
  exist.
- **The default is network on**: the guest gets an address on
  `192.168.64.0/24` and can reach the internet. `--network none` leaves
  only loopback, and an outbound connection fails with "Network
  unreachable". The executor must pass `--network none` explicitly; this
  ADR's "network off by default" is a requirement on our executor, not
  the tool's default.

## A container beside the heavy model

Measured on the M6, with the heavy model generating 2,000 tokens while one
container started, ran and exited:

| served build | swap before | swap after | free memory during |
|---|---|---|---|
| plain MLX 4-bit, 2026-09-29 | 1,247.8 MB | 1,239.8 MB | 38% |
| DWQ 4-bit, 2026-09-30 (`--network none`) | 1,268.8 MB | 1,260.8 MB | 38 to 39% |

No swap growth either time, so one short-lived container fits beside the
heavy model on 32 GB. Two or more at once, or a long-running one, were not
measured.

Checklist for the mini is in `ops/mac-mini-setup.md`, section 9.

## Consequences

- No code change in this PR. `lab/sandbox.py` keeps its documented
  limits.
- The rollout ADR (item 4.1) treats "executes untrusted code" as a
  separate, later stage that requires the container tier.
- If the measurement shows a container is too costly next to the heavy
  model, the fallback is to keep refusing untrusted execution rather than
  to run it under Seatbelt.
