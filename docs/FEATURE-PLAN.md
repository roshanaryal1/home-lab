# Feature plan: what home-lab takes from OpenClaw and Hermes Agent

Written 2026-10-09 (NZDT) for #326, after the owner's decision on #189: ship v0.1, then add
features month by month. The sources for what OpenClaw and Hermes Agent do are in
[COMPARISON.md](COMPARISON.md). Month 1 is October 2026 and month 7 is April 2027, the months of
the seven-month plan to a production release.

## The rule every feature follows

A feature comes in only through the broker. So it runs as the `lab` account, its tools have a
policy tier, anything at the approve tier waits for the owner's signature, every action lands in
the audit log, and untrusted code runs only in the container. A feature that adds a new way for
outside text to cause an action gets a pre-registered claim, in the style of
[PREREGISTRATION-SAFETY.md](PREREGISTRATION-SAFETY.md), before it ships. Effort figures below are
guesses, not measurements.

## Features to take, in order

| # | Feature | What they do | How home-lab does it | Safety condition | Month |
|---|---|---|---|---|---|
| 1 | `lab doctor` | Both have a `doctor` command that checks an install. | One read-only command: Python and package versions, the `lab` account, file permissions, the operator's public key, launchd jobs loaded, the model server on loopback, the last backup and self-test. Prints what to fix. | Read-only. Never prints a secret. | 2 |
| 2 | Update with backup and rollback | OpenClaw's `openclaw update` checks the next version first and advises a verified backup; Hermes Agent's `hermes update` takes a state snapshot. | `lab update` takes a verified backup, checks the target commit, runs migrations on a copy first, and keeps the previous commit for one-command rollback. Replaces the manual "Updating the deployed code" steps. | The deployment stays root-owned; `git` and `uv sync` never run as root. | 2 |
| 3 | `lab security audit` | OpenClaw's `openclaw security audit` checks a live install and offers safe fixes. | A read-only audit of the lab's own boundary: the `lab` account cannot read the key or the owner's home, service definitions are root-owned, the database owner, loopback binds, egress allowlist. Reports, never changes. | Read-only. Output gives no detail beyond pass or fail per check unless the owner asks locally. | 2 |
| 4 | Metrics export | Both export OpenTelemetry; OpenClaw also Prometheus. | A loopback metrics endpoint (queue depth, task outcomes, ceilings hit, approvals waiting, model latency) in Prometheus text format, beside the existing status page. No push to any outside host. | Loopback only, read-only, no task content. | 3 |
| 5 | Owner-defined schedules | OpenClaw's scheduler and heartbeat; Hermes Agent's cron with headless approval set to deny. | `lab schedule add` creates a task on a calendar rule. A scheduled task runs with the same tiers as any other; approve-tier steps park for the owner's signature, never run unattended. | Approve tier never auto-approved. Pre-registered claim: a scheduled task cannot reach an approve-tier tool without a signature. | 3 |
| 6 | A second chat channel | OpenClaw has about 30 channels; Hermes Agent about 20. | One more adapter behind the same pairing, allowlist and broker path as Telegram, chosen by the owner (Signal or Slack are the likely first). The M2 case set is extended to the new channel before it ships. | Only the paired account is answered. M2 holds on the new channel. | 3 |
| 7 | Session search | Hermes Agent has full-text search over past sessions. | Full-text search over the owner's own chat and task history in SQLite FTS5, read-only for the agent, as a broker tool at the auto tier. | Results are tainted text, so anything they trigger follows the Rule of Two. | 4 |
| 8 | User profile file | Both keep `USER.md`. | An owner-written profile the agent reads at the start of a task. The agent may propose changes; they become active only on the owner's signature, like memory (M4). | No silent writes. Hermes Agent and OpenClaw let the agent write memory freely; home-lab does not. | 4 |
| 9 | Skill Workshop, safely | OpenClaw's Skill Workshop and Hermes Agent's agent-written skills let the agent create and improve skills. | The agent may draft a skill as a candidate. It stays inert until the owner signs a promotion, and its scripts run only in the container. A diff view shows what changed between versions. | M6 holds: no candidate becomes active without a signed promotion. | 4 |
| 10 | A curated skill index | ClawHub; Hermes Agent's Skills Hub with eight sources. | A small index of reviewed skills with pinned hashes, imported as candidates. No open marketplace. | Pinned hash per skill. Import is never activation. | 4 |
| 11 | More local runtimes | OpenClaw supports Ollama, LM Studio, llama.cpp, vLLM and SGLang; Hermes Agent lists about 52 providers. | Model adapters for `llama-server` and LM Studio on loopback, chosen per task class, each measured with the existing benchmark before it is used. | Loopback only. The admission controller bounds memory for each. | 5 |
| 12 | Hosted model, opt-in | Both support many hosted providers. | Off by default. When the owner turns it on for a task class, requests go through the egress gateway, with what may leave the machine stated in the config and the audit log. | Never on by default. Tainted or sensitive tasks never leave the machine unless the owner says so per class. | 5 |
| 13 | Bounded delegation | OpenClaw's sub-agents; Hermes Agent's `delegate_task`. | A `task.delegate` tool that creates child tasks with a narrower tool set, inheriting taint and ceilings, with a hard limit on count and depth. | A child never has more tools than its parent. Pre-registered claim before it ships. | 5 |
| 14 | Voice notes | Both transcribe voice messages; Hermes Agent uses local Faster-Whisper. | Local transcription of chat voice notes on the Mac, then the text enters as tainted input like any message. | Local model only. Transcripts are tainted. | 5 |
| 15 | One-line install | Both have one-line installers for macOS, Linux and Windows. | A signed, versioned install script for macOS on Apple silicon that runs `lab setup-plan` and stops before anything that needs `sudo`, so the owner reads and applies it. | The script never runs `sudo` itself. | 6 |
| 16 | Browser in the container | Both automate a browser. | A headless browser inside the M5 container, reaching the web only through the egress gateway's allowlist, returning page text as tainted input. | No network except the gateway. Never the owner's own signed-in browser. | 6 |

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
