# What still needs the Mac mini, and why

Everything that can be built and proved without the machine is either merged
or tracked as an open pull request. What is left needs the Mac mini itself. It
falls into four kinds:

- **A check on real hardware.** The code exists and its tests pass in CI with a
  fake, but the claim is about macOS, launchd, Seatbelt, Apple `container` or
  the real model. A fake cannot prove it.
- **A setting only you can make.** A key, a token or a pairing that must never
  pass through this repository or any other machine.
- **A measurement.** A number that only exists once the real model and the
  real handlers run on the real machine.
- **A decision or a person.** Not the Mac, but not code either. Listed at the
  end so nothing is lost.

Do them in the order below. Each later step assumes the earlier ones.

## 0. Deploy the latest main

Why: every check below tests the deployed build, not the repository.

```sh
cd "$HOME/home-lab" && git checkout main && git pull --ff-only origin main
```

Then deploy as in `ops/mac-mini-setup.md` section 8 and reload the daemons
(section 16). `lab.cli status` must say `IDLE` before you go on.

## 1. One sitting with the session script

Why: these are checks of the machine's own behaviour (file permissions, power
assertions, launchd restarts, the alert path). The script runs them in order and
writes one report. See `ops/mac-session.md`.

Two steps need setup from section 2 first: `skillrun` needs the pinned
container image in `LAB_CONTAINER_IMAGE`, and `mcp` needs at least one signed
server in `/etc/homelab/mcp.json`. Without them those steps report SKIPPED. Do
that setup first, or rerun them afterwards with `./ops/mac-session.sh --only skillrun` and
`./ops/mac-session.sh --only mcp`.

The `selftest` step reads the nightly self-test log, which only exists once the
self-test job from section 2 has run. On a first session it reports FAIL for
that reason. Rerun it the morning after with
`./ops/mac-session.sh --only selftest`.

```sh
./ops/mac-session.sh --dry-run
LAB_CONTAINER_IMAGE='<name@sha256:...>' ./ops/mac-session.sh
```

| Step | Issue | Why it needs the Mac |
|---|---|---|
| `home` | #225 | Whether `lab` can read your home folder depends on this Mac's real permissions. |
| `caffeinate` | #235 | Whether a non-admin account can hold a power assertion is a macOS rule. If it passes, the keep-awake daemon moves to `lab`. |
| `signature` | #70 | Uses the real operator public key in `/etc/homelab`. |
| `backup` | #67 | Writes to the real backup disk as `lab`. |
| `alert` | #79, #80 | A real alert must reach your phone. |
| `selftest` | #80 | Reads the nightly self-test log launchd wrote. |
| `skillrun` | #255 | Runs a skill script through `skill.run` in a real Apple container. Needs `LAB_CONTAINER_IMAGE`. |
| `mcp` | #256 | Starts each signed MCP server under real Seatbelt and compares its tools with the signed snapshot. Skipped when no server is configured. |
| `concurrency` | #211 | Two real model requests at once: time, memory, swap. |
| `drills` | #78 | `kill -9` and `kill -STOP` of the real supervisor under launchd. |
| `network` | #79 | Manual: unplug the network and time the dead-man alert. |
| `power` | #77, #91 | Manual: pull the power during a task and time the return. |

Commit the report to `docs/reviews/` and paste the summary into each issue it
names. A PASS closes the matching check box. A FAIL becomes a new issue.

## 2. Settings only you can make

Why: these put a secret or an identity on the machine. They are done at the
keyboard so no secret travels through the repository.

- **Chat bot (#239).** Make a new bot in BotFather, pair it, and run the checks
  in `ops/mac-mini-setup.md` section 23. Then retire the old raw-shell bot
  (#184), as that section says.
- **Container image (#181).** Install Apple `container`, pull one small Linux
  image and note it pinned by digest (`name@sha256:...`). The tests below read
  it from `LAB_CONTAINER_IMAGE`.
- **Daily backup, heartbeat and morning report (#67, #79, #80).** Install the
  backup and heartbeat jobs and reinstall the self-test job from
  `ops/launchd/`, as runbook step 4 and setup sections 10, 18 and 19 say. Set
  the backup folder in the installed backup plist. Put the dead-man ping URL in
  `/etc/homelab/heartbeat-url` (owned by `lab`, mode 600) at a hidden prompt.
  At the outside service, use a 5 minute period and a 5 minute grace, so the
  alert reaches the phone within ten minutes (owner's decision, 2026-10-01).
- **MCP servers (#256), if you want any.** For each server, run
  `lab mcp snapshot <server> --allow <tools> --key <operator key> --by <you>`
  and add the signed entry to `/etc/homelab/mcp.json`. Nothing runs from an
  unsigned entry.
- **Repository sources (ADR 0008), if you want any.** Keep a mirror of each
  repository on the Mac, readable by `lab`. For each, run
  `lab repo sign <name> <path> --key <operator key> --by <you>` and add the
  printed entry to `/etc/homelab/sources.json`. Check with `lab repo list`.
  Then set `LAB_REPO_SOURCES` to that file in the supervisor plist, as below.
  Check that one `repo.read` task copies a commit as `lab` from a mirror you
  own. If git refuses the mirror for its owner, fix the mirror's ownership or
  permissions, not the check.
- **Turn on the two granted handlers, if you want them.** The supervisor only
  registers the `skill.run` handler when `LAB_CONTAINER_IMAGE` is set, and the
  `mcp.call` handler when `LAB_MCP_SERVERS` names the signed server file. Add
  both, and `LAB_REPO_SOURCES` if you signed sources, to the
  `EnvironmentVariables` of the installed
  `com.homelab.supervisor.plist`, then reload it. If any entry in the server
  or sources file is unsigned, the supervisor exits with code 2 and does not
  start. Check `lab.cli status` says `IDLE` afterwards.

## 3. Real-container and real-sandbox checks

Why: the container executor and the sandbox are tested in CI against a fake
runtime. The claims are about Apple's real VM and real Seatbelt.

```sh
cd "$HOME/home-lab"
LAB_CONTAINER_IMAGE='<name@sha256:...>' uv run pytest tests/test_container.py -v \
  -k "hostile_script or setsid_survivor"
```

- The two real-container tests must pass (#181). They prove the network is off,
  `/work` is the only host mount, and a `setsid` process dies with the
  container.
- The guest now runs as the unprivileged user `65534:65534`, never root
  (owner's decision, 2026-10-01). Check that it can still write `/work`. If the
  shared folder's ownership stops it, fix how the folder is shared, not the
  user (ADR 0007).
- The `skillrun` and `mcp` steps of the session script are the real-machine
  checks for `skill.run` (#255) and MCP (#256).
- The M5 claim ran on 2026-10-07: 30 cases, 0 failures (docs/PREREGISTRATION-SAFETY.md,
  `uv run python -m lab.cli prereg m5 --image <name@sha256:...>`). Rerun it after any change
  to the container executor. M2 and M6 ran in CI. M5 is about the real
  container, so it can only run here.

## 4. Measurements on the real model

Why: these numbers only exist on this machine with the real model loaded.

| Issue | What | How |
|---|---|---|
| #211 | Is one heavy slot enough? | The `concurrency` step above, then decide. |
| #180 | Per-task memory and CPU ceilings | As `lab`, `uv run python -m lab.cli measure-ceilings`. Commit the report, then set `task_max_rss_mb` and `task_max_cpu_seconds` from it (setup section 22). On Linux the 2x suggestion was 102 MB and 1 s of CPU. Decide then whether 1 s is too tight and a larger `--headroom` is needed. |
| #84 | Typed decision model in shadow | Setup section 22. Grow the case file past 30 cases first. |
| #179 | Constrained decoding for routing labels | Blocked until H1's case file is frozen. |
| H1 to H4 | Pre-registered model runs | Setup sections 12 to 14 and 21. |

## 5. A fresh install on another Mac (#188)

Why: the install guide is only proved when someone follows it on a Mac that
has never run the lab. That needs a second Mac and a fresh user account.
Follow `docs/INSTALL.md` word for word and log every place it was unclear.
This is the main step of M7, the first release.

## 6. Not the Mac, but still open

- **Decisions already made (2026-10-01).** Containers run as a non-root user.
  A networked MCP server counts as untrusted input only, gated by your
  signature, the egress list and the approve tier. `skill.run` and `mcp.call`
  are granted to reviewed handlers, still at the approve tier. The heartbeat
  uses a 5 minute grace.
- **Decisions made later the same day (ADR 0008).** Containers stay behind
  `skill.run`. A repository enters a workspace only through an explicit
  `workspace.acquire` step that records provenance (built 2026-10-02). An MCP
  error reply is a failure, and only a call whose outcome is unknown is held.
- **Make each paper citable (#83).** A Zenodo account and a release. No Mac
  needed, but it needs your account.
- **An independent security review.** The safety claims have only been checked
  by the owner (docs/ROADMAP.md, Risks).

## How to tell it is done

Each Mac-dependent issue above closes only when its check passed on the Mac and
the result is written where the issue says. Code merged with passing CI is not enough for
these, because CI cannot see this machine.
