# home-lab compared with OpenClaw and Hermes Agent

Written 2026-09-30 for #189. Every statement about another project comes
from that project's own README or SECURITY.md, or from the GitHub API, read
that day. Repository heads at about the time of reading: `openclaw/openclaw`
`0e33bb3d`, `NousResearch/hermes-agent` `02e41181`. Both projects change
daily; re-check before quoting any of this.

## Size and maturity (GitHub API, 2026-09-30)

| | OpenClaw | Hermes Agent | home-lab |
|---|---|---|---|
| Repository | [openclaw/openclaw](https://github.com/openclaw/openclaw) | [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) | this repository |
| Created | 2025-11-24 | 2025-07-22 | 2026-09-25 |
| Stars / forks | 390,802 / 82,200 | 250,081 / 53,383 | new, one owner |
| Latest release | v2026.9.6 (2026-09-23) | v2026.9.24 (2026-09-24) | none |
| Language | TypeScript | Python | Python |
| Licence | MIT (OpenClaw Foundation) | MIT | MIT |
| Published GitHub security advisories | 722 (14 critical, 249 high, 390 medium, 69 low) | 0 through GitHub's advisory feature | 0 |

The advisory count measures an active disclosure programme as much as
risk: OpenClaw triages reports in public, and most were published from
February to April 2026. Hermes Agent having none published on GitHub does
not show it has fewer flaws; it may handle reports another way.

## What they do that home-lab does not

Both are mature daily-use agents. home-lab has none of the following yet:

- **Chat everywhere.** OpenClaw meets users in Discord, iMessage, Slack,
  Teams, Telegram, WhatsApp "and 20+ more" (README). Hermes Agent runs
  Telegram, Discord, Slack, WhatsApp, Signal and CLI from one gateway, with
  voice memo transcription (README).
- **Tools, skills and plugins, with a marketplace.** OpenClaw has tools,
  skills, a plugin SDK and ClawHub (README). Hermes Agent has skills
  compatible with the agentskills.io standard, and MCP (README).
- **Memory and learning.** Hermes Agent: agent-curated memory, autonomous
  skill creation, skills that improve during use, cross-session search,
  Honcho user modelling (README).
- **Scheduling, subagents, remote runtimes.** Hermes Agent: built-in cron,
  parallel subagents, seven terminal backends including Docker, SSH, Modal
  and Daytona (README).
- **Any model.** Both swap hosted and local model providers (READMEs).
- **One-line install** on macOS, Linux and Windows (Hermes Agent README;
  OpenClaw's recorded README also documents one-line shell and PowerShell
  installers for macOS, Linux and Windows).

## Where home-lab differs on safety

This is the case for home-lab, and it is narrower than "better". It rests
on what each project says its own security boundary is.

**1. What stops a tricked agent.**
Hermes Agent: "The only security boundary against an adversarial LLM is
the operating system. Nothing inside the agent process constitutes
containment" (SECURITY.md §2.2). Its default terminal backend "runs
commands directly on the host" (§2.1), and its approval gate "catches
cooperative-mode mistakes, not adversarial output" (§2.4).
OpenClaw: "Exec behavior is host-first by default:
`agents.defaults.sandbox.mode` defaults to `off`" (SECURITY.md, Operator
Trust Model), and "Tools run on the host for the main session unless you
configure sandboxing" (README).
home-lab: the agent runs by default as a separate non-admin OS account,
`lab`, that cannot become root, cannot read the owner's files or approval
key, and cannot edit its own code or service definitions. Checked on the
Mac mini on 2026-09-30 (`ops/runbook-lab-account-and-daemons.md`, steps 2
and 3). So the operating-system boundary that both projects call the real
one is home-lab's default, not an opt-in.

**2. Approvals.**
Hermes Agent's approval gate is a pattern check on shell strings, which its
own policy calls structurally incomplete (§2.4). OpenClaw requires approval
for configured actions and treats anyone holding the gateway secret as a
full operator (Operator Trust Model).
home-lab: an approval is a signature by the owner's Ed25519 key, which
lives in the owner's account where the agent's account cannot read it
(`lab/policy.py`, `lab/supervisor.py`). The agent cannot approve its own
action, short of an OS privilege escalation.
**Caveat, found on the first deployment (#190):** this holds only where
the running process was given the owner's public key. The five-minute
loop (`lab tick`) builds its own supervisor without it, and runs the queue
itself whenever the supervisor daemon is down, so in that window
approvals would not be signature-checked. No tasks or credentials existed
when this was found. The Mac mini was fixed the same day by giving the
loop the key; the code is being changed so that a supervisor without the
key refuses to run tasks at all.

**3. Prompt injection.**
OpenClaw lists "prompt injection without a policy, auth, approval, sandbox,
or tool-boundary bypass" as not a security bug (SECURITY.md). Hermes Agent
likewise puts the boundary at the OS, not the model.
home-lab measures it: the pre-registered study (https://osf.io/jfp74)
tests how often the local model refuses injected tool calls (H2, H2b), and
AgentDojo attack runs are recorded (`docs/PREREGISTRATION.md`). That is a
measurement, not a defence; its value is that the numbers are public and
the hypotheses were fixed before the runs.

**4. Plugins.**
Both run plugins in-process with the agent's full privileges and make
operator review the boundary (Hermes Agent SECURITY.md §2.5; OpenClaw
"Plugin Trust Boundary"). home-lab loads handlers only from reviewed code
under `lab.handlers`, each in its own worker process with a minimal
environment and no database access, acting only through the broker
(SECURITY.md, "Known gaps"). It has no plugin marketplace, which makes
this easier for it.

**5. Evidence of recovery.**
home-lab records recovery drills on the target machine: on 2026-09-30 the
supervisor was killed (`kill -9`) and frozen (`kill -STOP`), first with a
result that did not meet the two-minute target and exposed a real gap in the
watchdog (#271), then, after the fix, twice in 96 s and 97 s on 2026-10-06; and
a backup restore passed on a still-empty database (`ops/drills/log/`).
Neither README describes an equivalent published drill record; that is an
absence in what was read, not proof they do not test it.

## Where home-lab is weaker, stated plainly

- It is not a daily-use agent yet: no chat interface, few real tools, no
  cross-session memory for a user, no skills a user can install.
- One machine, one owner, one heavy model at a time on 32 GB.
- No community, no releases, no install guide yet (#188).
- Its own phone control path, the Telegram bot, sits outside the repo and
  bypasses the broker, approvals and audit log (SECURITY.md).
- Several protections are designed but not built (SECURITY.md, "What does
  NOT exist yet"). The container executor for untrusted code is built as
  a module but not yet a broker tool or run on the Mac (#181), and the
  first deployment already found one gap (#190).
- The safety claims above have been checked on one Mac mini by its owner,
  not by an independent review.

## What this suggests for the roadmap

home-lab should not try to out-feature either project. A defensible
position is: **the personal agent whose safety boundary is on by default
and measured in public.** Add daily-use features only through the existing
broker, so every new tool inherits the lab account, signed approvals and
the audit log:

1. Fix #190, then the install guide (#188).
2. A chat interface (Telegram first, since it exists), routed through the
   broker instead of a raw shell.
3. A small set of tools (files in a workspace, web fetch, calendar read),
   each behind policy and approvals.
4. Memory that the owner can inspect, correct and delete (`lab memory`
   already exists).
5. The container executor for untrusted code (#181).
6. Interoperability rather than competition: reading skills in the
   agentskills.io format, and MCP, each through the broker.

The owner decides this order in #189 before any build work starts.
