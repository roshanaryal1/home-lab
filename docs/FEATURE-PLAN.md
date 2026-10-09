# Feature plan: what home-lab takes from OpenClaw and Hermes Agent

Written 2026-10-09 (NZDT) for #326, after the owner's decision on #189: ship v0.1, then add
features month by month. The sources for what OpenClaw and Hermes Agent do are in
[COMPARISON.md](COMPARISON.md). Month 1 is October 2026 and month 7 is April 2027, the months of
the [seven-month plan](PLAN-7-MONTHS.md) to a production release.

## The rule every feature follows

A feature comes in only through the broker. So it runs as the `lab` account, its tools have a
policy tier (autonomous, notify, approve or never, as in `lab/broker.py`), anything at the approve tier waits for the owner's signature, every action lands in
the audit log, and untrusted code runs only in the container. A feature that adds a new way for
outside text to cause an action gets a pre-registered claim, in the style of
[PREREGISTRATION-SAFETY.md](PREREGISTRATION-SAFETY.md), before it ships. Effort figures below are
guesses, not measurements.

## Features to take, in order

| # | Feature | What they do | How home-lab does it | Agent access and tier | Safety condition | Month |
|---|---|---|---|---|---|---|
| 1 | `lab doctor` | Both have a `doctor` command that checks an install. | One read-only command: Python and package versions, the `lab` account, file permissions, the operator's public key, launchd jobs loaded, the model server on loopback, the last backup and self-test. Prints what to fix. | Owner command, not an agent tool. | Read-only. Never prints a secret. | 2 |
| 2 | Update with backup and rollback | OpenClaw's `openclaw update` checks the next version first and advises a verified backup. Hermes Agent's `hermes update` takes a state snapshot. | `lab update` takes a verified backup, checks the target commit, runs migrations on a copy first, and keeps the previous commit. The two ownership steps of the runbook's "Updating the deployed code" stay with the owner: the command prints them for `sudo` and waits. Rollback works the same way, to the kept commit. | Owner command, not an agent tool. | The deployment stays root-owned. `git` and `uv sync` never run as root. | 2 |
| 3 | `lab security audit` | OpenClaw's `openclaw security audit` checks a live install and offers safe fixes. | A read-only audit of the lab's own boundary: the `lab` account cannot read the key or the owner's home, service definitions are root-owned, the database owner, loopback binds, egress allowlist. Reports, never changes. | Owner command, not an agent tool. | Read-only. Output gives no detail beyond pass or fail per check unless the owner asks locally. | 2 |
| 4 | Metrics export | Both export OpenTelemetry. OpenClaw also exports Prometheus metrics. | A loopback metrics endpoint (queue depth, task outcomes, ceilings hit, approvals waiting, model latency) in Prometheus text format, beside the existing status page. No push to any outside host. | No agent access. The owner reads it. | Loopback only, read-only, no task content. | 3 |
| 5 | Owner-defined schedules | OpenClaw has a scheduler and heartbeat. Hermes Agent has cron, with headless approval set to deny. | `lab schedule add` creates a task on a calendar rule. A scheduled task runs with the same tiers as any other. Approve-tier steps park for the owner's signature, never run unattended. | Owner command. The agent cannot add a schedule. | Approve tier never auto-approved. Pre-registered claim: a scheduled task cannot reach an approve-tier tool without a signature. | 2 |
| 6 | A second chat channel | OpenClaw has about 30 channels. Hermes Agent has about 20. | One more adapter behind the same pairing, allowlist and broker path as Telegram, chosen by the owner (Signal or Slack are the likely first). The M2 case set is extended to the new channel before it ships. | Messages become tasks. Each tool keeps its tier. | Only the paired account is answered. M2 holds on the new channel. | 3 |
| 7 | Session search | Hermes Agent has full-text search over past sessions. | Full-text search over the owner's own chat and task history in SQLite FTS5, read-only for the agent, as a broker tool at the auto tier. | `session.search` at autonomous. | Results are tainted text, so anything they trigger follows the Rule of Two. | 4 |
| 8 | User profile file | Both keep `USER.md`. | An owner-written profile the agent reads at the start of a task. The agent may propose changes. They become active only on the owner's signature, like memory (M4). | Read at autonomous. A change is proposed at notify and needs the owner's signature. | No silent writes. Hermes Agent and OpenClaw let the agent write memory freely. Home-lab does not. | 4 |
| 9 | Skill Workshop, safely | OpenClaw's Skill Workshop and Hermes Agent's agent-written skills let the agent create and improve skills. | The agent may draft a skill as a candidate. It stays inert until the owner signs a promotion, and its scripts run only in the container. A diff view shows what changed between versions. | Drafting at notify. Promotion needs the owner's signature. | M6 holds: no candidate becomes active without a signed promotion. | 4 |
| 10 | A curated skill index | OpenClaw has ClawHub. Hermes Agent has a Skills Hub with eight sources. | A small index of reviewed skills with pinned hashes, imported as candidates. No open marketplace. | Import as candidates at notify. Promotion needs the owner's signature. | Pinned hash per skill. Import is never activation. | 4 |
| 11 | More local runtimes | OpenClaw supports Ollama, LM Studio, llama.cpp, vLLM and SGLang. Hermes Agent lists about 52 providers. | Model adapters for `llama-server` and LM Studio on loopback, chosen per task class, each measured with the existing benchmark before it is used. | Owner configuration, not an agent tool. | Loopback only. The admission controller bounds memory for each. | 5 |
| 12 | Hosted model, opt-in | Both support many hosted providers. | Off by default. When the owner turns it on for a task class, requests go through the egress gateway, with what may leave the machine stated in the config and the audit log. | Owner configuration per task class, not an agent tool. | Never on by default. Tainted or sensitive tasks never leave the machine unless the owner says so per class. | 4 |
| 13 | Bounded delegation | OpenClaw has sub-agents. Hermes Agent has `delegate_task`. | A `task.delegate` tool that creates child tasks with a narrower tool set, inheriting taint and ceilings, with a hard limit on count and depth. | `task.delegate` at notify. Child tools never wider than the parent's. | A child never has more tools than its parent. Pre-registered claim before it ships. | 5 |
| 14 | Voice notes | Both transcribe voice messages. Hermes Agent uses local Faster-Whisper. | Local transcription of chat voice notes on the Mac, then the text enters as tainted input like any message. | Transcription at autonomous. The text is tainted input. | Local model only. Transcripts are tainted. | 5 |
| 15 | One-line install | Both have one-line installers for macOS, Linux and Windows. | A signed, versioned install script for macOS on Apple silicon that runs `lab setup-plan` and stops before anything that needs `sudo`, so the owner reads and applies it. | Owner command, not an agent tool. | The script never runs `sudo` itself. | 5 |
| 16 | Browser in the container | Both automate a browser. | A headless browser inside the M5 container, reaching the web only through the egress gateway's allowlist, returning page text as tainted input. | `browser.fetch` at notify, like `net.fetch`. | No network except the gateway. Never the owner's own signed-in browser. | 3 |
| 17 | Public injection suite | None of the three cloud agents has an independent, rerunnable injection test ([MARKET-2026.md](MARKET-2026.md)). | A fixed, versioned set of injection cases over web pages, email and files, run against the real broker path on every release, with the results published. Any case that succeeds is fixed before its result is published. | No agent access. CI and the owner run it. | The suite only grows. A release with a successful case does not ship. | 2 |
| 18 | Stalled task check | Dots users report a dot that reports progress without finishing ([MARKET-2026.md](MARKET-2026.md)). | The watchdog flags a task whose lease keeps renewing with no new event for a set time, and the status page and alerts say so. | No agent access. | Read-only. It flags, it never kills work on its own. | 3 |
| 19 | Cost meter and daily cap | Self-hosted agents surprise users with model bills. | For the opt-in hosted model (feature 12): tokens and estimated cost per task and per day, and a hard daily cap the agent cannot raise. | No agent access. The cap is owner configuration. | A task that would pass the cap waits for the owner. | 4 |
| 20 | Export and today view | Dots memory cannot be exported or edited item by item. | `lab export` writes memory, the action log and task results as plain JSON and Markdown. A one-page "what the agent did today" on the status page. | Owner command. | Read-only. | 4 |
| 21 | Opt-in cloud runner | The cloud agents keep working while the user's computer sleeps. | A documented way to run a second lab on a machine the owner rents, with the owner's own key, the same broker and the same signatures. Off by default. The hosted model and the runner are named in the audit log. | Owner configuration. | Same tiers and signatures as the local lab. No vendor holds the owner's key. | 5 |
| 22 | AI label on messages | The EU AI Act's duty to tell people they face an AI system applies from 2026-08-02 ([MARKET-2026.md](MARKET-2026.md), source 32). | Every message the agent sends to a person through a channel says it comes from an AI agent. | No agent access. Added by the channel adapter. | The label cannot be turned off by the agent. | 3 |

## The owner's decision on 2026-10-09: hybrid, and four things first

After the market research in [MARKET-2026.md](MARKET-2026.md), the owner chose a hybrid
position: local by default, with an opt-in hosted model (feature 12) and an opt-in cloud runner
(feature 21) for people who want them. Both are off by default. No mode lets a model approve an
action.

The owner also chose four things to build first: scheduled tasks (feature 5, moved to month 2),
a safe browser (feature 16, moved to month 3), an easy install (features 2, 3 and 15, with
feature 15 moved to month 5), and injection defence (feature 17). Feature 12 moved to month 4.
The months in the table above already show the moves.

## What home-lab will not copy

- **Sandbox off by default.** OpenClaw runs host exec without approval prompts in its default.
  home-lab keeps the `lab` account and the container on by default.
- **A model judging approvals.** Hermes Agent's default `smart` mode asks a model to approve
  commands. In home-lab only the owner's signature approves.
- **Agent writes without review.** Both let the agent write memory and skills by default. In
  home-lab those stay proposals until the owner signs.
- **In-process plugins with full privileges.** Both run plugins inside the agent process. home-lab
  keeps handlers in separate worker processes with ceilings.
- **An open marketplace.** OpenClaw's ClawHub had malicious skills reported in 2026 (press
  reports, not opened from here, so unverified). home-lab keeps a small pinned index instead.
- **Phoning home.** No update check or metrics leave the machine unless the owner configures it.
- **Many users on one gateway.** Both say one gateway is one trust boundary. home-lab is for one
  owner (#41 stays deferred).

## How each feature starts

Each row becomes its own issue when its month starts, with acceptance criteria and, where the
table says so, a claim registered before the code is written. The monthly issues of the seven
month plan list them.
