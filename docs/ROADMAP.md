# Roadmap proposal: from a safe runtime to a daily-use agent

**Status: decided 2026-10-01.** Written as a proposal on 2026-09-30. On
2026-10-01 the owner chose **(b), build our own on the broker**, in
[#189](https://github.com/roshanaryal1/home-lab/issues/189). Time estimates
are guesses and have not been measured.

## Where we start

Verified in this repository on 2026-09-30:

- A broker with tiered tools (`lab/broker.py`): `fs.read` and `fs.list` run
  on their own, `fs.write` and `net.fetch` notify, `fs.delete`, `shell.run`
  and `connector.call` need an approval.
- Owner-signed approvals, a hash-chained audit log, an egress gateway, a
  secret vault, versioned skills whose promotion needs the owner's
  signature (`lab/skillstore.py`), and searchable memory the owner can
  inspect, correct and delete (`lab memory`).
- On the Mac mini since 2026-09-30: the lab runs as a non-admin account under
  launchd, with a watchdog and drill records (`ops/drills/log/`).

What is missing for daily use:

- **No chat path through the broker.** The Telegram bot is a TOTP-gated raw
  shell outside the repository; it bypasses the broker and the audit log.
  Update: the chat channel is built (`lab/chat.py`, #239); installing it and
  retiring the raw-shell bot are operator steps on the Mac.
- **No real handlers.** `lab/handlers/` holds one demo.
- No tools a person wants (calendar, mail, richer web, code editing), no
  agent-proposed memory, no skill import, and no install path for other
  people ([#188](https://github.com/roshanaryal1/home-lab/issues/188)).

## The decision that came first

[#189](https://github.com/roshanaryal1/home-lab/issues/189) asked the owner to
pick one of three. The owner picked (b) on 2026-10-01:

- **(a) Wrap an existing agent.** Run Hermes Agent or OpenClaw as the `lab`
  account and route its risky tool calls through the broker. Chat, tools and
  memory come from upstream. **Unverified:** whether either can be put behind
  the broker without heavy patching. A two-day spike answers it.
- **(b) Keep building our own** on the broker. Every milestone below applies
  in full and takes months.
- **(c) Hold the product direction** and finish the lab and the research.

The proposal recommended (a) as a time-boxed spike. The owner chose (b): every
part stays under the lab's own broker, approvals and audit. The milestones
below are written for (b) and now apply in full.

## Milestones

Each has a check that either passes or fails, and a safety boundary it must
not weaken.

**M0. Finish the lab underneath.** Open items: the timed freeze re-drill
(#78), the fabricated-signature test (#70), scheduled backups (#67),
alerts and an emergency stop from the phone (#79), the power-pull drill
(#77, #91), and the machine checks in #225, #235 and #211. Done when those
issues close. They all need the operator at the mini. One script runs them
in order and writes one report
([#241](https://github.com/roshanaryal1/home-lab/issues/241)).

**M1. Decide.** Done 2026-10-01: #189 records (b). No spike is needed.

**M2. Chat through the broker**
([#239](https://github.com/roshanaryal1/home-lab/issues/239)). A message from the owner's paired chat
becomes a task; the reply comes from the task's result; approve-tier actions
wait for the owner's signature. Done when a test shows the chat path cannot
reach `shell.run` without an approval and the raw-shell bot is retired or
clearly separated. Boundary: only the paired chat id is answered. About 3 to
5 days.

**M3. Three real tools**
([#240](https://github.com/roshanaryal1/home-lab/issues/240)). For example workspace files, web fetch with
summary, read-only calendar. Each is a reviewed handler under `lab.handlers`
with a policy tier, tested with hostile input, and it sets the real task
ceilings ([#180](https://github.com/roshanaryal1/home-lab/issues/180)). No
real credentials until the three preconditions in the README hold. About one
week. Status: workspace files, read-only git and web fetch with summary are
built and tested with hostile input
([#240](https://github.com/roshanaryal1/home-lab/issues/240)); the ceilings
wait for them to run real work on the Mac mini (#180).

**M4. Memory.** The agent may propose a memory; the owner approves it; the
owner can inspect, correct and delete everything (`lab memory` exists). Done
when a proposed memory cannot become active without a decision. About 3 days.

**M5. Untrusted code in a container**
([#181](https://github.com/roshanaryal1/home-lab/issues/181)). One disposable
container per task, `--network none`, nothing mounted except the task
workspace. Done when a hostile script cannot reach the network or the host's
files. This comes before any imported skill can run code. About 1 to 2 days.

**M6. Skills and interoperability.** Read skills in the agentskills.io format
and speak MCP, each through the broker. An imported skill is only a
candidate; it becomes active on the owner's signed promotion, and its code
runs only in M5's container. Highest risk in the list. About 1 to 2 weeks.

**M7. Install and first release.** The install guide finished with a
fresh-user test on another Mac (#188), a tagged release, and this roadmap
updated with what actually happened. About 2 days.

## Measure in public

The one thing this project can offer that the larger agents do not is a
number for every safety claim. Each milestone gets one claim written down
before it is measured, in the style of `docs/PREREGISTRATION.md`. For
example: M2, how many of N injected chat messages reach an approve-tier tool
without a signature (the target is 0); M6, how many of a fixed set of
malicious skills become active without a signed promotion. Any amendment is
dated before the run.

The claims for M2, M5 and M6 are pre-registered in
[docs/PREREGISTRATION-SAFETY.md](PREREGISTRATION-SAFETY.md), with their case
sets frozen by SHA-256 in `evals/prereg/` before any of that code is built.

## What this does not try to do

- Match the features or the community of OpenClaw or Hermes Agent
  (`docs/COMPARISON.md`).
- Multi-user or multi-tenant use (#41).
- A skill marketplace.
- Real credentials before the three preconditions in the README hold.

## Risks

- (b) is months of work, not days.
- The estimates above are unmeasured guesses.
- One owner, one machine. The safety claims have been checked by the owner,
  not by an independent reviewer; an outside security review should come
  before anyone else is told to depend on this.

## Questions for the owner

1. Which of (a), (b), (c) in #189? Answered 2026-10-01: (b).
2. First channel: Telegram, or another? #239 starts with Telegram, the
   channel the lab already alerts on.
3. Which three tools first?
4. Who reviews security independently, and when?
