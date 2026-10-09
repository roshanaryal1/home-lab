# home-lab

An AI operating system for an Apple M6 Mac mini (32 GB unified, 512 GB
internal + 1 TB external SSD). Not a 24/7 worker pool that runs whatever
it's told: the target is a system that notices work, decides what it's
worth, and does it safely without waiting to be asked. See
[ADR 0004](docs/decisions/0004-operating-system.md) for the framing and
what's actually missing to get there, or
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for a current-state diagram.

**To install it on your own Mac, start with [docs/INSTALL.md](docs/INSTALL.md).**
Where the rest of this README names a machine, a disk or a date, it means the
project's own Mac mini.

This is the **build**. The **design** came first, from a controlled
study: one frozen prompt was given to eleven frontier LLM systems,
each answer was captured verbatim, and the points of agreement and
disagreement were adjudicated into a reference architecture. That study
is [roshanaryal1/llm-architects](https://github.com/roshanaryal1/llm-architects);
the spec this repo implements is its
`analysis/consensus/reference-architecture.md`.

So the unusual thing here is that the architecture is not one person's
opinion, and it is not one model's opinion either. Where the systems
agreed unanimously, that is recorded as consensus. Where they
disagreed, the disagreement is written down as an open decision rather
than quietly resolved.

## Status

Current state, what is waiting and on whom, and what must not be redone: [docs/STATUS.md](docs/STATUS.md).

| Build step | State |
|---|---|
| 1. SQLite schema: tasks, leases, events, agents, approvals | done |
| 2. Supervisor event loop and durable recovery | done, with known gaps listed in `SECURITY.md` |
| 3. Model adapter, one heavy + one light model | adapter built and tested against a mock, then run against the real heavy model on the M6 with measured memory ([#74](https://github.com/roshanaryal1/home-lab/issues/74), ADR 0001); the light model is not chosen yet |
| 4. Concurrency semaphores (heavy=1, light=2-3) | done, in the supervisor |
| 5. Model swap manager with RAM/headroom policy | not built; the budget it needs is now measured on the M6 (ADR 0001) |
| 6. Aider/OpenHands executor adapters | not started |
| 7. Research evidence ledger and verification pipeline | ledger built: claim status is separate from task status, every claim opens its exact source ([#90](https://github.com/roshanaryal1/home-lab/issues/90)); the automated verification pipeline is not |
| 8. Dedicated-user permissions and task workspaces | on the M6 since 2026-09-30: the lab runs as a non-admin `lab` account that cannot `sudo`, cannot read the owner's approval key and cannot change its own code or service definitions; by default, the supervisor and `lab tick` refuse to start without the owner's public key, and `--allow-unsigned` is refused on a machine where the deployed key exists ([#190](https://github.com/roshanaryal1/home-lab/issues/190), [#200](https://github.com/roshanaryal1/home-lab/issues/200)); approvals made there without the owner's key were refused in the fabricated-signature test on 2026-10-06; still open: the `lab` account owns the queue database, so code it runs, a reviewed handler's worker included, can reach the database directly, [#70](https://github.com/roshanaryal1/home-lab/issues/70) |
| 9. launchd + watchdog + queue-aware caffeinate | installed on the M6 2026-09-30 as six LaunchDaemons; the daily backup job (`com.homelab.backup`: restore-checked, keeps the newest 14, alerts on failure, [#67](https://github.com/roshanaryal1/home-lab/issues/67)) and the dead-man switch ping (`com.homelab.heartbeat`, every 5 minutes to an outside check) were installed on the M6 on 2026-10-07. The backup was run once by launchd by hand (`kickstart`) after the first such run failed because macOS refused the lab interpreter the removable disk (the failure alert reached the phone; Full Disk Access for that interpreter fixed it); that run wrote a manifest and passed its restore check, shown in the job's log, which is not committed. The 02:47 run on its own schedule has not yet been seen. A launcher that holds Full Disk Access in place of the shared interpreter is merged ([#287](https://github.com/roshanaryal1/home-lab/issues/287)); moving the grant to it is the owner's step in the runbook. The outside check showed up and the job pinged it; the alert channel is a Telegram bot, a test alert reached the phone ([report](docs/reviews/2026-10-07-mac-session-alert.md)); after a `kill -9` a new supervisor was found at the first check (the drill polls every 2 s, so under 2 s was not measured more finely) and a frozen one is replaced by the watchdog in under two minutes: 96 s and 97 s in two runs on 2026-10-06 after a gap was fixed that had left a supervisor frozen before its first heartbeat unseen (#271; [kill drill](ops/drills/log/2026-10-07-supervisor-kill.md), [freeze drill](ops/drills/log/2026-10-07-supervisor-freeze.md), [second freeze drill](ops/drills/log/2026-10-07-supervisor-freeze-pass-2.md), [restore drill](ops/drills/log/2026-09-30T010636Z-restore.md)); caffeinate under a real queue still to check, [#78](https://github.com/roshanaryal1/home-lab/issues/78) |
| 10. Tailscale-only FastAPI dashboard and emergency stop | `lab status`, `lab control` (pause, drain, stop), `lab cancel` and a read-only `lab dashboard` on loopback exist; alerts go through the operator's hook, the nightly self-test also reports "ok" every morning (built; run by hand on the M6 on 2026-10-07 it passed with all 636 safety tests, but its own 03:17 run has not yet been seen to pass there) ([#80](https://github.com/roshanaryal1/home-lab/issues/80)), and `lab heartbeat` pings an outside dead-man switch every five minutes while the lab is healthy. Both are built and tested on Linux. On the M6 the Telegram alert reached the phone and the heartbeat job and its outside check were set up on 2026-10-07 (the check showed up and the job pinged; no report is committed for that); the unplug test of the dead-man switch is still to do, [#79](https://github.com/roshanaryal1/home-lab/issues/79) |
| 11. sqlite-vec / FTS retrieval | FTS5 baseline built with inspect, correct, revoke and delete ([#85](https://github.com/roshanaryal1/home-lab/issues/85)); embeddings must beat it on a measured task first |
| 12. Benchmark and tune before adding anything else | benchmarked on the M6 (`evals/bench/`, setup section 14); first tuning test run as pre-registered H3 (prompt cache 1 against 4: no gain, not adopted, `docs/PREREGISTRATION.md`) |
| M3. Three real tools ([#240](https://github.com/roshanaryal1/home-lab/issues/240)) | workspace files (notify), read-only git (autonomous) and web fetch with summary (notify) built as reviewed handlers in `lab/handlers/`, each with only the broker tools it needs, and tested with hostile input on Linux (`tests/test_local_tools.py`); not yet run on the M6 through the deployed lab. Task ceilings were measured on the M6 with their sample tasks and set to 256 MB and 30 s, [#180](https://github.com/roshanaryal1/home-lab/issues/180) |
| Repositories in a workspace ([ADR 0008](docs/decisions/0008-tools-report-control-plane-decides.md)) | A repository enters a task's workspace only through `workspace.acquire`: approve tier, from an operator-signed source on this machine, at an exact commit id. The copy carries no hooks, remote or symlinks, and every copy is recorded with its source, commit, tree, workspace and task (`lab repo acquired`). The `repo.read` handler copies a commit and runs one read-only git command on it. It is registered only when `LAB_REPO_SOURCES` names a file whose every entry is signed. Tested on Linux with real git (`tests/test_workspace_acquire.py`). Not yet run on the M6. |
| Skill scripts and MCP tools through handlers ([#255](https://github.com/roshanaryal1/home-lab/issues/255), [#256](https://github.com/roshanaryal1/home-lab/issues/256)) | The owner decided on 2026-10-01 to grant `skill.run` and `mcp.call` to reviewed handlers now, still at the approve tier. Each handler holds its one tool, and every call waits for the operator's signature. The `skill.run` handler is registered only when `LAB_CONTAINER_IMAGE` is set. The `mcp.call` handler is registered only when `LAB_MCP_SERVERS` names a file whose every entry is signed, and it gets no network. Tested on Linux with a fake container runtime and a fake MCP server (`tests/test_grant_skill_mcp.py`). Not yet run on the M6. |

Steps 1, 2 and 4 are machine-independent and run anywhere. Everything
touching model residency needs the 32 GB machine to mean anything, and
that machine is now running (below).

The table above is the execution substrate: safe to leave running
unattended, but it has no opinion about what work should exist. Three
planes on top of it are what make this an operating system rather than
a task runner:

| Plane | Issue | State |
|---|---|---|
| Observation: notice work from the event stream, emit proposals | [#32](https://github.com/roshanaryal1/home-lab/issues/32) | this repo's closed issues and merged PRs become proposals; the event log yields a proposal when tasks of one kind keep failing alike, when closed issues look alike, or when a measurement has no artifact |
| Router: post / blog / paper by evidence weight | [#33](https://github.com/roshanaryal1/home-lab/issues/33) | rubric built over the evidence ledger: deterministic rules, refuses thin evidence upward, inspectable chain; still needs a human to review every route |
| Chat: a message from the owner's paired chat becomes a task, the reply comes from its result | [#239](https://github.com/roshanaryal1/home-lab/issues/239) | built and tested against a fake Telegram server through the egress gateway; only the paired chat is answered, approving still needs the operator's signature on the Mac; installed on the mini on 2026-10-07: `/status` and a plain message worked from the phone and the real model's reply came back ([record](docs/reviews/2026-10-07-chat-bot-install.md)); not yet checked: an unpaired account, `/stop`, the approval boundary and whether the service survives a reboot. The old raw-shell bot was retired the same day (setup section 23) |
| Publish: post/email on your behalf | [#86](https://github.com/roshanaryal1/home-lab/issues/86) | reviewed publishing built and tested against a dummy provider: approval bound to destination and draft hash, write-ahead receipts, idempotency keys, reconciliation of lost responses; no real destination has been used |

Build order and reasoning are in ADR 0004; the rules that limit what an
agent may hold, and the staged rollout, are in ADR 0006.

## Install on your own Mac

New users should start with [docs/INSTALL.md](docs/INSTALL.md). It covers requirements, RAM-based model selection, the read-only prerequisite check, installation, first run, backup variables and uninstall. The deployment runbook is linked from there for the system-level launchd setup.

## Running on the Mac mini

Since 2026-09-30 the lab's heavy model runs on the M6 itself:
`Qwen3-Coder-30B-A3B-Instruct`, the 4-bit DWQ build (the plain 4-bit build
corrupts copied text, ADR 0001), served by `mlx-lm` on loopback only and kept
up by a user LaunchAgent (after a `kill -9` it was back in 16 seconds).

The lab itself has run there since the same day, installed by
[the runbook](ops/runbook-lab-account-and-daemons.md): the supervisor runs as
the non-admin `lab` account from root-owned code in `/opt/homelab`, and the
watchdog, keep-awake, status check, nightly self-test and five-minute loop
run as launchd daemons. Reviewed handlers run in a worker process as `lab`
(`lab/worker.py`); only the broker's `shell.run` is sandboxed
(`lab/sandbox.py`). The lab has done no real work yet: its queue is empty
and it holds no credentials.

**Reaching it.** The Mac mini and the owner's devices share a private
Tailscale network. Nothing listens on the internet and no router port is
forwarded. SSH works over the tailnet; the model and the dashboard stay on
the Mac mini's loopback and are reached through an SSH tunnel, never by
widening what they bind to:

> **zsh note.** The blocks below have `#` comments at the end of some lines. macOS's default zsh does not treat those as comments when you paste, so run `setopt interactivecomments` first (it lasts for that Terminal window), or leave the comments out.

```sh
ssh -N -L 8080:127.0.0.1:8080 <user>@<mac-mini>   # the model, at http://127.0.0.1:8080/v1
ssh -N -L 8765:127.0.0.1:8765 <user>@<mac-mini>   # lab dashboard, after `lab dashboard` is started on the mini
```

**What has been measured there so far**, each with its record:

| What | Result | Where |
|---|---|---|
| Apple `container`, measured for the untrusted-code tier (used only by the broker's `skill.run`, off unless `LAB_CONTAINER_IMAGE` is set, #255. Since 2026-10-01 the `skill.run` handler holds that tool, approve tier) | 0.64 s median start; inside a container the host's accounts and files were not visible; network is on by default, so the executor always passes `--network none`. On 2026-10-07 the pre-registered M5 claim ran against the real container: 30 hostile scripts (12 network, 10 host path, 8 survivor), 0 failures, with two controls; an outside observer could not see connections to public addresses, so those cases rest on the guest having no network interface (the limits are in the result) | ADR 0007, `docs/PREREGISTRATION-SAFETY.md` |
| Heavy model memory and speed | 17.2 GB loaded, about 200 KB per token of context, about 16K tokens under the 20.5 GB budget, about 67 tok/s | ADR 0001 |
| Utility evaluation, 24 tasks | heavy 19, 4B baseline 20; reruns identical | setup section 14, `evals/runs/` |
| Prompt injection with the real model driving | 0 of 9 attacks succeeded in the registered replication (H4, 2026-09-29 UTC); the model tried the injected action in 3 of the 8 scenarios whose handler ran, and the broker stopped all three. The other two real-model runs, not registered, are in `SECURITY.md` | `docs/PREREGISTRATION.md`, Results, H4 |
| Backup and recovery | encrypted external backup disk; one backup written and restore-checked from the command line on the M6 (2026-10-07, [report](docs/reviews/2026-10-07-mac-session-backup.md)); the scheduled job has run once, by hand, and its own 02:47 run is not yet confirmed; task crash drills passed (2026-09-29); after a supervisor `kill -9` under launchd a new one was found at the first 2 s check, a frozen supervisor was replaced by the watchdog in 96 s and 97 s in two runs (2026-10-06, after the fix for #271), and a first restore drill passed on a still-empty database (2026-09-30) | setup sections 10 and 16, `ops/drills/log/` |

**Pre-registered tests.** The evaluation plan is registered on OSF
([osf.io/jfp74](https://osf.io/jfp74), 2026-09-29 14:45 UTC) at commit
`d8726b43`. Runs made before that are listed in its Amendment 1 as
exploratory; the confirmatory runs come after it.

**One caveat for always-on.** FileVault is on, so after a power cut the
Mac restarts and waits at the login screen; nothing, the model included,
runs until someone logs in.

## Where this is going

The owner's aim since 2026-09-30 is an agent other people can install and
use daily, in the space of OpenClaw and Hermes Agent. home-lab will not
out-feature them. The position is narrower: **the personal agent whose
safety boundary is on by default and measured in public.** New daily-use
features (chat, tools, memory) are to be added only through the existing
broker, so each inherits the lab account, signed approvals and the audit
log. The cited comparison is [docs/COMPARISON.md](docs/COMPARISON.md). On
[#189](https://github.com/roshanaryal1/home-lab/issues/189) the owner chose to ship
v0.1 first, then add features month by month; which features, and what home-lab
will not copy, is in [docs/FEATURE-PLAN.md](docs/FEATURE-PLAN.md).
The months to v1.0 and a public launch, and what v1.0 must pass, are in
[docs/PLAN-7-MONTHS.md](docs/PLAN-7-MONTHS.md). The wider market (OpenAI Dots, Meta Muse, xAI
Grok Bot and the open-source agents) and the owner's hybrid position, local by default with an
opt-in cloud, are in [docs/MARKET-2026.md](docs/MARKET-2026.md).
The install guide for your own Mac is [docs/INSTALL.md](docs/INSTALL.md); it has not
yet been tested end to end on a fresh Mac ([#188](https://github.com/roshanaryal1/home-lab/issues/188)).

## The two rules that shape the code

**One heavy inference slot.** All ten non-anchor systems in the study
agreed: a hundred logical agents does not mean a hundred resident
models. On 32 GB you get one heavy model, with light work alongside it.
The supervisor enforces this with a semaphore rather than trusting
convention, and the model layer holds the heavy slot as a lock file beside
the database, so the supervisor and `lab tick` (separate processes) cannot
both send a heavy request to the model server ([#211](https://github.com/roshanaryal1/home-lab/issues/211)).
A command run by hand that builds its own controller (`lab bench`, the eval
and attack runners) does not take that lock; do not run one while the lab is busy.

**Never replay a destructive task after a crash.** A task is marked
idempotent or it is not. On restart, stranded tasks become
`interrupted`; idempotent ones requeue automatically, everything else
waits for a human. This is the difference between a crash costing you
five minutes and a crash sending the same email twice.

## Security status

The repository is public: nothing in it is a credential by design, and the
history is scanned for secrets in CI. See [SECURITY.md](SECURITY.md) for the
repository controls and how to report a vulnerability privately.

Run the lab only with dummy data, review every draft by hand and connect
no real credentials until three things are true. The first is largely
done: a separate non-admin lab account that cannot read the operator's
approval key exists on the Mac mini since 2026-09-30, and approvals made
there without the key were refused in the fabricated-signature test on
2026-10-06. What is still open is that the lab account owns the queue
database, so code it runs can reach the database directly
([#70](https://github.com/roshanaryal1/home-lab/issues/70)).
Setting it up found one gap, the five-minute loop running without the
key, fixed the same day ([#190](https://github.com/roshanaryal1/home-lab/issues/190)).
Per-task ceilings are set from measured peaks: 256 MB and 30 s of CPU
([#180](https://github.com/roshanaryal1/home-lab/issues/180)); they apply once the
supervisor is redeployed. Still not done: the Keychain path exercised on the mini. The egress gateway and the
secret broker now exist and are tested, but only against fake networks and
dummy credentials. [SECURITY.md](SECURITY.md) has the full list of what is
and is not enforced, and [THREATS.md](THREATS.md) maps it to the OWASP
agentic top 10 with the test or open issue behind every row.

Ideas taken from outside projects do not change that. They are reviewed
against these gaps first; see the
[agent-scripts review](docs/reviews/2026-09-29-agent-scripts.md).

## Skill library checks

`lab skills` is a read-only check on a directory of skills. It never runs,
imports or writes anything it scans. `skills import` is the one exception:
it validates a skill and stores it as a candidate, never as active.

```sh
uv run python -m lab.cli skills validate --root path/to/skills   # exit 1 on any problem
uv run python -m lab.cli skills inventory --root path/to/skills [--json]
uv run python -m lab.cli skills import path/to/skills/one --tier notify --by you --source URL
```

`validate` checks that every skill has a SKILL.md with a safe frontmatter
(no aliases, anchors or tags), a non-empty name and description, a name that
matches its directory, no duplicate names, no symlink leaving the library,
size limits, and none of a short list of forbidden commands (permission
prompts disabled, a download piped into a shell) in SKILL.md, executable
files or a `scripts/` or `bin/` directory. It also refuses zero-width and
bidi control characters in SKILL.md or a name, a name with non-ASCII
look-alike letters, a word that mixes scripts, a second SKILL.md below the
skill root, and an `allowed-tools` list naming a tool the broker does not
know or one above the declared or requested tier. A name one edit away from
another skill, or from a name passed with `--known`, is reported as a
typosquat. `inventory` lists each
skill with a content hash so any change to a skill is visible.

`import` runs the same checks, with the typosquat check against every name
already in the skill store, then submits the skill as a candidate with its
source recorded as `derived_from`. It prints the version id. The candidate
does nothing until an operator other than the importer promotes it with a
signed `lab skillstore promote`. Syncing a
library between machines is deliberately not built: it waits until one
machine is named the canonical copy and changes to skills have an approval
step ([#87](https://github.com/roshanaryal1/home-lab/issues/87)).

## Operating the lab

> **zsh note.** The blocks below have `#` comments at the end of some lines. macOS's default zsh does not treat those as comments when you paste, so run `setopt interactivecomments` first (it lasts for that Terminal window), or leave the comments out.

`lab` and `python -m lab.cli` are the same command. Each command below works with either spelling, for example `uv run lab status`. `lab --version` prints the version.

```sh
uv run python -m lab.cli status [--json] [--alert-config F]   # queue, worker health, counters; exit 2 if unhealthy
uv run python -m lab.cli doctor                   # read-only check: database, operator key, model, disk, backup, selftest, audit chain; exit 1 if any fails
uv run python -m lab.cli security-audit [--details]   # read-only check of owners, modes, the operator key and the loopback model URL, exit 1 if any fails
uv run python -m lab.cli migrate --check           # before a restart onto new code: migrate a private copy of the database and check it; the database is only read (#356)
uv run python -m lab.cli approvals                # what is waiting for a decision
uv run python -m lab.cli show <id>                # read the exact call before deciding
uv run python -m lab.cli approve <id> --by you --key operator.key --expect-hash <prefix>
uv run python -m lab.cli operator init --dir ~/.lab-operator   # create the approval signing key
uv run python -m lab.cli deny <id> --by you       # refuse it; the parked task is cancelled
uv run python -m lab.cli tasks                    # task counts by state
uv run python -m lab.cli control pause|resume|drain|stop --by you   # the whole-lab switch; resume takes --key operator.key; `control show` reads it
uv run python -m lab.cli watchdog [--max-age 90] [--dry-run]   # kill a hung supervisor; launchd restarts it
uv run python -m lab.cli cancel <task> --by you   # cancel work that has not started
uv run python -m lab.cli chat [--once] [--chat-id N]   # the paired Telegram chat: messages become tasks; cannot approve or resume
uv run python -m lab.cli selftest [--alert-config F [--report-ok]]   # chain, backup restore, health, safety tests; alerts on failure, and with --report-ok also on success
uv run python -m lab.cli heartbeat --url-file F   # ping the dead-man switch, only while the lab is healthy; the URL is never printed
uv run python -m lab.cli setup-plan [--apply]      # print the lab-account setup; --apply needs root on macOS
uv run python -m lab.cli keepawake [--once] [--grace 600]   # hold caffeinate only while work is pending
uv run python -m lab.cli tick [--repo owner/repo]     # one pass: observe, summarize with the model, route
uv run python -m lab.cli schedule add NAME --daily HH:MM|--weekly DAY HH:MM|--every-minutes N --kind KIND --title T --key K --by you   # an owner-signed schedule; `lab tick` starts its task when due, approve-tier steps still wait for you. `schedule list`, `schedule remove NAME --by you` (docs/SCHEDULES.md)
uv run python -m lab.cli emit [--min-failures 3]  # queue proposals from patterns in the event log
uv run python -m lab.cli chain <task>             # the events that produced a proposal
uv run python -m lab.cli ops                      # operations of unknown outcome
uv run python -m lab.cli resolve <op> --happened|--not-happened --by you   # reconcile one
uv run python -m lab.cli audit verify             # walk the hash-chained event log
uv run python -m lab.cli audit checkpoint --key K --out DIR    # signed head, kept outside the lab
uv run python -m lab.cli artifacts verify         # re-hash every stored output
uv run python -m lab.cli backup --to DIR [--keep N] [--alert-config F]   # online snapshot of the database and every blob it refers to (task artifacts, evidence snapshots, skill files, #358); with --keep, restore-check it, then keep the newest N
uv run python -m lab.cli restore-check <manifest> --into DIR   # restore into a fresh dir and verify everything, every blob re-hashed
uv run python -m lab.cli drill crash              # inject a real failure and log it (ops/drills/)
uv run python -m lab.cli skillstore submit|promote|known-good|rollback|history|install   # versioned skills, operator-promoted, one-step rollback
uv run python -m lab.cli prereg m2|m6 [--json]   # run pre-registered Claim M2 or M6 on its frozen cases, refused if the case file changed
uv run python -m lab.cli publish list|show <key>|reconcile <key> --connectors FILE   # receipts for credentialed sends; ask the provider about a lost response
uv run python -m lab.cli mcp snapshot <server> [--allow a,b] [--key K --by you]   # what the operator signs for an MCP server. `mcp list [--check]` shows each server's state
uv run python -m lab.cli repo sign <name> <path> --key K --by you   # sign a repository source for workspace.acquire. `repo list` shows each source's state, `repo acquired [--task ID]` where every copied repository came from
uv run python -m lab.cli memory search|inspect|add-evidence|correct|revoke|delete   # inspectable FTS5 memory
uv run python -m lab.cli route <task> [--want paper]   # post, blog, paper or nothing, by evidence weight; thin evidence refused upward
uv run python -m lab.cli shadow --cases evals/shadow_cases.jsonl   # measure the rubric on labeled cases; a model candidate is compared in shadow, never applied
uv run python -m lab.cli dashboard [--port 8765]   # read-only status page on loopback; no controls, GET only, everything escaped
uv run python -m lab.cli ledger show <task>       # claims, their evidence and status; review, verify
uv run python -m lab.cli memory-budget [TOKENS ...] [--measurements FILE]   # predicted resident memory of the heavy model at each context length (default 8192 16384 37000). With a measurement record, the error against it (#321)
uv run python -m lab.cli eval run --endpoint URL --model M --revision H --tokenizer-revision H --weights-mb N   # 24 fixed tasks, sealed provenance record; --db notes it as a measurement and takes the model slot the supervisor shares
uv run python -m lab.cli eval rerun <record>      # repeat a run from its record alone, then compare
uv run python -m lab.cli bench run --endpoint URL --model M --revision H --tokenizer-revision H --weights-mb N   # cold start, first token, decode speed, server memory
uv run python -m lab.cli bench tune <baseline> <candidate>   # recommend a setting only on a measured gain with no task lost
uv run python -m lab.cli measure-ceilings [--repeats 5] [--headroom 2]   # peak memory and CPU of every reviewed handler in real workers; suggests task ceilings, changes nothing
uv run python -m lab.attacks                      # benign-plus-hostile scenarios against a stub model
```

Approvals are signed with the operator's private key and the supervisor
honours only signatures that verify against the public key it is given
(`LAB_OPERATOR_PUBKEY`). That is a boundary only once the key is out of the
agent account's reach.

## Layout

```
lab/migrations/      numbered SQL migrations for the queue, audit and origin schema
lab/queue.py         state machine, leases, retry, crash recovery
lab/supervisor.py    asyncio loop, concurrency slots, dispatch
lab/policy.py        capability tiers, approvals (operator-signed), the gate
lab/authority.py     the Rule of Two, enforced per task (ADR 0006)
lab/origin.py        where a task's input came from, and the taint that follows it
lab/broker.py        typed tools, workspaces, per-call audit
lab/handlers/        reviewed handlers, the only code a worker loads: workspace files, read-only git, web summary, skill.run, mcp.call, repo.read
lab/egress.py        the only outbound path: default-deny, resolve-then-pin
lab/vault.py, connectors.py, publish.py   secrets injected per call, one destination each, receipts and reconciliation
lab/audit.py         append-only hash chain and signed checkpoints
lab/artifacts.py     content-addressed task outputs
lab/backup.py, drills.py      verifying backup, rotation of a scheduled backup folder, logged recovery drills
lab/metrics.py       `lab status`, derived from the event log
lab/control.py       the operator mode switch: pause, drain, stop
lab/service.py       heartbeat, watchdog and the launchd definitions (`ops/launchd/`)
lab/selftest.py, alert.py   nightly self-test; the operator-configured alert hook
lab/deadman.py       the dead-man switch ping, sent through the egress gateway only while the lab is healthy
lab/accountplan.py   the lab-account setup written as a plan, dry-run by default
lab/shadow.py        candidate routing model measured against the rubric on labeled cases; advice only
lab/grammar.py       tool-call JSON Schema generated from the broker table, for constrained decoding
lab/dashboard.py     read-only status page on loopback
lab/keepawake.py, logsetup.py   queue-aware sleep prevention; rotating private JSON logs
lab/loop.py          the loop: summarizer handler, ledger claim, rubric route, `lab tick`
lab/emitter.py       proposals emitted from the event log, each with its event chain
lab/schedule.py      owner-signed schedules that start tasks on a calendar rule and never approve
lab/model.py         bounded model adapter: pinned revisions, admission, strict tool calls
lab/memory.py        inspectable memory: FTS5, provenance, expiry, revoke that reaches drafts
lab/rubric.py        the router's rules: evidence weight to post, blog, paper or nothing
lab/ledger.py        research claims with statuses separate from task state, evidence snapshots
lab/bench.py         benchmark of one endpoint and the tuning gate
lab/ceilings.py      peak memory and CPU of each reviewed handler, and suggested task ceilings
evals/ceilings/      the sample tasks it runs, and its reports
lab/evals.py         fixed task set run against any endpoint, sealed provenance records
lab/attacks.py       injection harness
evals/tasks.jsonl    the 24 tasks (arithmetic, extraction, format, code, tool calls, injection)
evals/shadow_cases.jsonl   12 labeled research cases for the shadow experiment
docs/PREREGISTRATION.md    evaluation plan, registered at osf.io/jfp74, with results
docs/REFERENCES.md         every cited paper: published or preprint, and how checked
lab/skills.py        read-only skill validator and inventory
lab/skillstore.py    skills as versioned artifacts: candidate, promote, known good, rollback
lab/prereg.py        runners for the pre-registered Claims M2 and M6 over their frozen case files
THREATS.md           OWASP agentic top 10 mapped to controls and tests
docs/decisions/      ADRs 0001 to 0007
ops/mac-mini-setup.md  setup and parked-hardware checklists for the mini itself
ops/drills/          recovery drill template and dated records
tests/               pytest suite
```

## Running the tests

```sh
uv sync --locked --extra dev   # exact Python from .python-version, deps from uv.lock
uv run python -m pytest tests/ -q
```

One runtime dependency, `cryptography`, for the operator's Ed25519
approval signatures: the standard library has no asymmetric signatures and
a shared secret would let the verifier forge. Async tests run through a
small hook in `tests/conftest.py` rather than pulling in `pytest-asyncio`,
because this machine is meant to run unattended and every dependency is a
thing that can break at 3am.

## Citing

`CITATION.cff` is validated in CI and kept in step with the package version.
A DOI needs the owner to connect Zenodo; the steps are in
[`ops/release.md`](ops/release.md).

## Setting up the mini

See [`ops/mac-mini-setup.md`](ops/mac-mini-setup.md). Do the accounts
and network sections before anything else; the dedicated non-admin user
is what keeps an agent mistake survivable.

Once the lab is deployed, the checks that need the machine run in one sitting:
`ops/mac-session.sh` runs each of them in order and writes one report. See
[`ops/mac-session.md`](ops/mac-session.md) for what each check proves and which
issue its result goes into. [`docs/MAC-WORK.md`](docs/MAC-WORK.md) lists every
step that still needs the Mac mini, in order, and why.
