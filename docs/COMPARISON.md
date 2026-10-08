# home-lab compared with OpenClaw and Hermes Agent

First written 2026-09-30 for #189. Rewritten 2026-10-09 (NZDT) for #326. Every statement about
another project comes from that project's own repository files or docs, read on 2026-10-08 (UTC),
unless it says otherwise. Repository heads at the time of reading: `openclaw/openclaw`
`ea7cc1066568fbbeece9151c497ecab15923bd2a`, `NousResearch/hermes-agent`
`38880bd2f1e90dbc9a1aeec03af62539ee64719a`. The GitHub REST API refused this session, so counts
come from the github.com pages and from git. Both projects change daily. Check again before
quoting any of this.

The feature plan that follows from this comparison is in [FEATURE-PLAN.md](FEATURE-PLAN.md).

## Size and maturity

| | OpenClaw | Hermes Agent | home-lab |
|---|---|---|---|
| Repository | [openclaw/openclaw](https://github.com/openclaw/openclaw) | [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) | this repository |
| Stars / forks (github.com page) | 391.6k / 82.3k | 252.2k / 54.3k | new, one owner |
| Latest stable | v2026.9.9, tagged 2026-10-08 | tag v2026.9.24, 2026-09-24 | none |
| Language and runtime | TypeScript, Node 24.16+ or 26.1+ (SECURITY.md) | Python | Python 3.13 |
| Licence | MIT, OpenClaw Foundation | MIT | MIT |
| Published GitHub security advisories | 722: 14 critical, 249 high, 390 moderate, 69 low (advisories page) | none published (advisories page) | none |

The advisory count measures an active disclosure programme as much as risk. OpenClaw triages
reports in public. Hermes Agent having none published on GitHub does not show it has fewer
flaws: search results name several CVEs for it that could not be opened from here, so they are
unverified.

## What they do that home-lab does not

Both are mature daily-use agents with large communities. home-lab has none of the following, or
only a small part.

**Chat everywhere.** OpenClaw ships A2A, Reef, Telegram and WebChat in the core install and 23
more channels as official plugins, among them Discord, iMessage, Signal, Slack, Teams and
WhatsApp (`docs/concepts/features.md`). Hermes Agent serves about 20 platforms from one gateway,
among them Telegram, Discord, Slack, WhatsApp, Signal, email and Home Assistant, with voice memo
transcription (`website/docs/user-guide/messaging/index.md`). home-lab has one channel,
Telegram, through the broker (#239).

**Skills and plugins.** OpenClaw has skills as `SKILL.md` folders, a Skill Workshop for skills the
agent learns itself "with change history and undo", a plugin SDK and the ClawHub registry
(`docs/tools/skills.md`, `docs/tools/skill-workshop.md`). Hermes Agent ships 58 bundled and 154
optional skills, reads the agentskills.io format, and lets the agent create and patch its own
skills. `skills.write_approval` defaults to off (`website/docs/user-guide/features/skills.md`).
home-lab imports agentskills.io skills as inert candidates that need the owner's signature (M6).

**Memory and learning.** OpenClaw keeps plain Markdown memory (`USER.md`, `MEMORY.md`, daily
notes) and says "there is no hidden state" (`docs/concepts/memory.md`). Hermes Agent keeps
`MEMORY.md` and `USER.md` with size caps, full-text search over past sessions, and memory
providers such as Honcho as plugins. `memory.write_approval` defaults to off
(`website/docs/user-guide/features/memory.md`). home-lab has owner-inspectable memory and
agent proposals that need the owner's signature (M4), but no session search.

**Scheduling and automation.** OpenClaw has a built-in scheduler, heartbeat, hooks, webhooks and
mail triggers (`docs/automation/cron-jobs.md`). Hermes Agent has natural-language cron jobs,
a no-agent script mode and webhook triggers. Headless approval for cron defaults to deny
(`website/docs/user-guide/features/cron.md`). home-lab runs its own jobs under launchd and a
five-minute loop, but a user cannot schedule a task.

**Subagents.** OpenClaw: sub-agents and a swarm mode, on by default since 2026.9.2
(`CHANGELOG/2026.9.2.md`). Hermes Agent: `delegate_task` with 10 concurrent children by default
(`hermes_cli/config_defaults.py`). home-lab tasks can create child tasks, which inherit taint,
but there is no delegation tool.

**Models.** OpenClaw lists 75 provider entries, hosted and local (`docs/providers/index.md`).
Hermes Agent lists about 52 (`website/docs/integrations/providers.md`). home-lab serves one local
model on loopback.

**Voice, browser, apps.** Both have voice modes, browser automation and companion apps:
OpenClaw on iOS, Android, macOS, Windows and Linux (`docs/platforms/index.md`). Hermes Agent has
a desktop app and an Android package (README). home-lab has none of these.

**Operations.** Both have a `doctor` command, one-line installers for macOS, Linux and Windows,
an update command with a backup step, and OpenTelemetry export (OpenClaw
`docs/install/updating.md`, `docs/gateway/opentelemetry.md`. Hermes Agent
`website/docs/getting-started/updating.md`, `developer-guide/gateway-monitoring.md`). OpenClaw
also has `openclaw security audit` (`docs/gateway/security/running-the-audit.md`). home-lab has
`lab status`, a read-only loopback status page and a written install guide, but no installer,
no `doctor` and no update command.

## Where home-lab differs on safety

This is the case for home-lab. It rests on what each project says its own boundary is.

**1. What stops a tricked agent, by default.**

- OpenClaw: "Exec behavior is host-first by default: `agents.defaults.sandbox.mode` defaults to
  `off`" (SECURITY.md, Operator Trust Model). In the single-operator default, host exec "is
  allowed without approval prompts" (`docs/gateway/security/trust-model.md`).
- Hermes Agent: "The only security boundary against an adversarial LLM is the operating system"
  (SECURITY.md 2.2). Its default terminal backend "runs commands directly on the host"
  (SECURITY.md 2.1), and its code-execution tool, MCP subprocesses, plugins and skills run on the
  host even when the terminal uses a container (SECURITY.md 2.2).
- home-lab: the agent runs by default as a separate non-admin account, `lab`, that cannot become
  root, cannot read the owner's files or approval key, and cannot edit its own code or service
  definitions (`ops/runbook-lab-account-and-daemons.md`, steps 2 and 3). Untrusted code runs in a
  disposable container with no network (M5, 0 of 30 hostile scripts escaped).

**2. Approvals.**

- OpenClaw: exec approvals are "operator guardrails to reduce accidental command execution, not
  a multi-tenant authorization boundary" (SECURITY.md).
- Hermes Agent: the default approval mode, `smart`, asks an auxiliary model to judge each
  command (`website/docs/user-guide/security.md`), and its policy says the gate "catches
  cooperative-mode mistakes, not adversarial output" (SECURITY.md 2.4).
- home-lab: an approval is a signature by the owner's Ed25519 key, bound to the intent the owner
  sees (#306), from a key the agent's account cannot read. Pre-registered claim M2: 0 of 32
  injected chat messages reached an approve-tier tool without a signature.

**3. Plugins and skills.**

- OpenClaw: plugins load "in-process with the Gateway and are treated as trusted code"
  (SECURITY.md, Plugin Trust Boundary).
- Hermes Agent: "Plugins load into the agent process and run with full agent privileges"
  (SECURITY.md 2.5), and the agent may write skills without approval by default.
- home-lab: handlers load only from reviewed code under `lab.handlers`, each in its own worker
  process with a minimal environment, CPU and memory ceilings and no database access. An imported
  skill stays an inert candidate until the owner signs a promotion (M6, 0 of 36 became active).

**4. Prompt injection.** Both projects put prompt injection on its own outside their security
policy unless it bypasses a boundary (OpenClaw SECURITY.md, "What Usually Is Not a Security Bug", and
Hermes Agent SECURITY.md 3.2). home-lab measures it in public: H4 (0 of 9 attacks succeeded with
the real model driving) and the pre-registered study at https://osf.io/jfp74.

**5. Evidence.** home-lab publishes pre-registered safety claims with frozen case sets, run
records, recovery drills on the target machine (`ops/drills/log/`) and a catalog of real
incidents (#325). Neither project's README describes an equivalent published record. That is an
absence in what was read, not proof they do not test it.

**6. Outbound traffic.** OpenClaw sends a daily update check by default
(`docs/gateway/telemetry.md`). Hermes Agent's shared metrics are off by default
(`hermes_cli/config_defaults.py`). home-lab has no telemetry. The chat and the agent's tools reach
the network only through its egress gateway, to hosts the owner configured. The alert command
sends to the fixed Telegram API host the owner set it up for (`lab/telegram_alert.py`).

## Where home-lab is weaker, stated plainly

- One chat channel, six reviewed handlers besides the demo, one local model, macOS on Apple
  silicon only, and only the 32 GB tier measured.
- No installer, no `doctor`, no update command, no release yet.
- No voice, no browser automation, no companion apps, no scheduling a user can set, no
  delegation tool, no session search.
- No community. The safety claims have been checked by the owner on one Mac mini, not by an
  independent review.
- The local model routes poorly on its own (H1: 0.10 against the rubric's 0.83), which is why
  rules and the owner's signature decide and the model only proposes.

## What this suggests

home-lab should not try to out-feature either project. Its position is: **the personal agent
whose safety boundary is on by default and measured in public.** Every feature it takes from
them comes in through the broker, so it inherits the lab account, signed approvals, the audit log
and a pre-registered claim where the feature adds risk. [FEATURE-PLAN.md](FEATURE-PLAN.md) lists
which features, in what order, and what home-lab will not copy.
