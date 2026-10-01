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

## Implementation (2026-10-01, #181)

`lab/container.py` builds the container tier. It is a module with a tested
seam, not yet a broker tool.

**What a run does.** One container per call, under a name the executor
chooses. The command line is built by one pure function,
`build_run_argv`, and is always an argument list:

```
container run --name lab-<task>-<random> --network none --read-only
  --tmpfs /tmp --cpus 2 --memory 1024M --volume <workspace>:/work
  --workdir /work --env HOME=/work --env PATH=... --env TMPDIR=/tmp
  <image@sha256:...> <command...>
```

- `--network none` is always there. The tool's default is network on, as
  measured above, and no caller input reaches the option list.
- The task workspace is the only host mount. `/tmp` is a tmpfs in the
  guest's own memory, and the root file system is read-only.
- CPU and memory default to 2 and 1024 MB, with ceilings of 4 and
  2048 MB. A busy 1024 MB guest costs about 2 GiB of host memory.
- The guest gets only `HOME`, `PATH`, `TMPDIR` and variables on a short
  allowlist (`LANG`, `TZ` and a few Python switches). Any other name is
  refused, not dropped.

**What it refuses, before anything starts.** An image not pinned by
`sha256` digest. A workspace that is a symlink, is not strictly inside the
lab's workspace root, does not exist, or has a `:` or `,` in its path. A
command that is not a list of strings. A host with no `container` CLI.
There is no fallback: untrusted code that cannot get a container does not
run.

**Removal on every path.** After the run, a `finally` block runs
`container delete --force <name>`: on a normal exit, a failing command, a
timeout, a stop (the same cancel flag `shell.run` uses) and an exception.
Killing the CLI client does not stop the guest, so the forced delete is
the control. If the delete fails, a listing decides whether the container
is gone; if that is uncertain, the result says it was not removed and the
error is logged.

**This is what closes #223 for untrusted code.** Under Seatbelt, a process
that calls `setsid()` leaves the process group and survives the kill. In
a container it is still inside the VM, and deleting the container ends
every process in it. The decision on #223 was to leave those survivors to
this tier; the tests prove the delete runs on every path.

**Tests.** `tests/test_container.py` uses a fake runtime that records each
command line, and a stand-in CLI script that exercises the real process
handling on any host. Two tests run a hostile script in a real container:
it tries HTTP, TCP, a Python socket and DNS, lists `/Users`, writes to
`/etc` and reads `/etc/passwd`, and the test asserts every probe failed,
that `/work` is the only virtiofs mount, and that the container is gone
afterwards. The second starts a `setsid` process and checks it ends with
the container. They skip unless the host is a Mac with the CLI and
`LAB_CONTAINER_IMAGE` names an image pinned by digest. They have not run
on the M6 yet.

**Why not a broker tool yet.** A broker tool needs a schema in
`broker.TOOL_SCHEMAS`. That table also generates the model's grammar and
the pre-registered tool-call corpus (`evals/toolcalls-v1.jsonl`, 70
tasks over seven tools), so an eighth tool changes a measured artifact.
That belongs in its own change. The tool will be a thin wrapper over
`ContainerExecutor.run`: approve tier, journaled as non-idempotent, and
classified as untrusted input in `authority.TOOL_LEGS`. Until then the
broker still has no tool that runs untrusted code.

*Update, 2026-10-01 (#255).* The tool now exists as `skill.run`. It runs a
script from the active version of a named skill, and only that. Approve
tier, journaled as non-idempotent, and `TOOL_LEGS` gives it untrusted
input. The approval names the skill version and its content hash, so a
promotion or a rollback after review needs a new approval. It refuses
before anything starts: a skill with no active version (a candidate,
rejected or rolled-back version never runs), a script that is not an
executable file in that version's manifest, a path that escapes, a version
whose stored files fail `verify_version`, and a host with no container
runtime or no digest-pinned image. Then it installs the verified version
into a fresh directory in the task's workspace, so the guest sees it under
`/work`, and runs it through `ContainerExecutor` with the same cancel flag
`shell.run` uses. There is no fallback to Seatbelt or the host. The
installed copy is removed after the run. Output is cleaned, capped and
marked untrusted, and the task is tainted when it reads it. The tool is
off unless `LAB_CONTAINER_IMAGE` names a pinned image, and no handler is
granted it yet. The grammar includes the new tool. The pre-registered
corpus stays pinned to its seven tools (`toolcorpus.CORPUS_TOOLS`), and a
test checks that `skill.run` is not in it. The fake-runtime tests are in
`tests/test_skillrun.py`. Its real-container test runs in the `skillrun`
step of `ops/mac-session.sh`.

*Update, 2026-10-01 (#255).* The owner decided on 2026-10-01 to grant
`skill.run` to a reviewed handler now, still at the approve tier. The
`skill.run` handler (`lab/handlers/skill_run.py`) takes a skill, a script
and optional arguments, holds that one tool and nothing else, and returns
the output marked untrusted. `register_all` registers it only when
`LAB_CONTAINER_IMAGE` names a pinned image. Every call still parks the task
until the operator signs. The tests are in `tests/test_grant_skill_mcp.py`.

**Never root in the guest (2026-10-01).** The owner chose to run the guest
as an unprivileged user. Every run passes `--user 65534:65534` ("nobody" in
common Linux images, so the image needs no account for it). The user is set
by trusted configuration, must be a numeric `uid:gid`, and uid or gid 0 is
refused before anything starts. The VM stays the boundary; this is one more
layer if the VM ever has a flaw. On the Mac, check that the guest can still
write to `/work` as that user. If the shared folder maps ownership so that it
cannot, the fix belongs in how the workspace is shared, not in going back to
root.

**Flags to confirm on the Mac.** The spellings follow the `container`
command reference: `--read-only`, `--tmpfs`, `--cpus`, `--memory`,
`--user`, `--volume`, `--workdir`, `--env`, `delete --force` and `list --all
--quiet`. Only `-v` and `--network none` were exercised in the
measurement above. The real-container tests are the check.

## Consequences

- No code change in the PR that accepted this ADR. `lab/sandbox.py`
  keeps its documented limits. The container tier followed in #181; see
  "Implementation".
- The rollout ADR (item 4.1) treats "executes untrusted code" as a
  separate, later stage that requires the container tier.
- If the measurement shows a container is too costly next to the heavy
  model, the fallback is to keep refusing untrusted execution rather than
  to run it under Seatbelt.
