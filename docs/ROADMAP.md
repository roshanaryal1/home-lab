# Roadmap proposal: from a safe runtime to a daily-use agent

**Status: decided 2026-10-01.** Written as a proposal on 2026-09-30. On
2026-10-01 the owner chose **(b), build our own on the broker**, in
[#189](https://github.com/roshanaryal1/home-lab/issues/189). Time estimates
are guesses and have not been measured.

The status lines below were checked against the code, the merged pull
requests and the issues on 2026-10-08. On that date an options memo on #189
([comment](https://github.com/roshanaryal1/home-lab/issues/189#issuecomment-6042898903))
recommends shipping (b) as v0.1 with no new features. On 2026-10-08 the
owner chose that: ship v0.1, then add features month by month through the
broker. The features are in [FEATURE-PLAN.md](FEATURE-PLAN.md), and the
months to v1.0 and a public launch are in [PLAN-7-MONTHS.md](PLAN-7-MONTHS.md).

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

What was missing for daily use on 2026-09-30, and where each stands on
2026-10-08:

- **Chat through the broker: done in part.** It was a TOTP-gated raw shell
  outside the repository that bypassed the broker and the audit log. The broker
  chat (`lab/chat.py`, #239) was installed on the Mac mini on 2026-10-07 and
  answered `/status` and a model reply from the owner's phone, and the raw-shell
  bot was retired the same day
  ([record](reviews/2026-10-07-chat-bot-install.md)). Not yet checked there: an
  unpaired account, `/stop`, the approval boundary and a reboot.
- **Real handlers: built, not yet doing real work on the Mac mini.**
  `lab/handlers/` holds six reviewed handlers besides the demo (its
  `__init__.py` lists them): workspace files, read-only git and web fetch with
  summary (#240), `skill.run` and `mcp.call` (#255, #256, granted at the
  approve tier in PR #266), and `repo.read` (ADR 0008, PR #269). None has yet
  done real work through the deployed lab (README Status); the three from
  #240 ran there on sample tasks for the #180 measurement on 2026-10-07.
- **Agent-proposed memory: built** (M4, #253, PR #259).
- **Skill import: built** (M6a, #254, PR #260). An imported skill is only a
  candidate.
- **Install path: written, not yet proved.** `docs/INSTALL.md` exists since
  PR #196 (2026-10-02). The fresh-account test on another Mac that
  [#188](https://github.com/roshanaryal1/home-lab/issues/188) waits for has
  not been run.
- **Still missing:** calendar and mail tools (no handler for either in
  `lab/handlers/`), and code editing by an executor such as Aider or
  OpenHands (README Status, step 6: not started).

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
(#78), keeping the queue database out of reach of the code the agent runs
(#70; its fabricated-signature test passed on 2026-10-06), scheduled backups (#67),
alerts and an emergency stop from the phone (#79), the power-pull drill
(#77, #91), and the machine checks in #225, #235 and #211. Done when those
issues close. They all need the operator at the mini. One script runs them
in order and writes one report
([#241](https://github.com/roshanaryal1/home-lab/issues/241)).

Status 2026-10-08: not done. Closed: #78 (the freeze drill passed twice, 96 s
and 97 s, PR #277), #225 and #211. Still open, each for the step named in its
latest comment:

- #70: approvals are bound to the intent the operator sees since PR #306, but
  a worker running as `lab` can still open the database. That needs the
  owner's decision on which account owns the database.
- #67: the nightly backup job is installed and passed once under launchd on
  2026-10-07, started by hand; its own 02:47 run is not yet recorded (README
  Status, step 9),
  and the monthly restore drill is left. PR #305 added
  `lab drill restore --from-backup` for that drill.
- #79: the alert reaches the phone; the unplug test of the dead-man switch
  is left (README Status, step 10).
- #77: the owner's FileVault trade-off and the plug-pull test.
- #91: `lab drill interrupted` exists since PR #305; the first full drill on
  the mini, and recovery of the deployed supervisor with a task in flight,
  are left.
- #235: reinstall keep-awake to run as `lab`, which needs sudo.
- #287, added since: the backup's Full Disk Access moves to its own launcher
  (PR #307); the owner's steps on the machine are left.

**M1. Decide.** Done 2026-10-01: #189 records (b). No spike is needed.

**M2. Chat through the broker**
([#239](https://github.com/roshanaryal1/home-lab/issues/239)). A message from the owner's paired chat
becomes a task; the reply comes from the task's result; approve-tier actions
wait for the owner's signature. Done when a test shows the chat path cannot
reach `shell.run` without an approval and the raw-shell bot is retired or
clearly separated (retired 2026-10-07). Boundary: only the paired chat id is answered. About 3 to
5 days. Status: built (#239, closed 2026-10-01). The pre-registered M2 claim
ran on 32 cases with 0 failures
([PREREGISTRATION-SAFETY.md](PREREGISTRATION-SAFETY.md), Results). Installed on
the Mac mini on 2026-10-07 and the raw-shell bot retired the same day (PR
#289, PR #291). The checks on the machine listed above under "What was
missing" are still to do.

**M3. Three real tools**
([#240](https://github.com/roshanaryal1/home-lab/issues/240)). For example workspace files, web fetch with
summary, read-only calendar. Each is a reviewed handler under `lab.handlers`
with a policy tier, tested with hostile input, and it sets the real task
ceilings ([#180](https://github.com/roshanaryal1/home-lab/issues/180)). No
real credentials until the three preconditions in the README hold. About one
week. Status: workspace files, read-only git and web fetch with summary are
built and tested with hostile input
([#240](https://github.com/roshanaryal1/home-lab/issues/240), closed
2026-10-06; `tests/test_local_tools.py`). Their peaks were measured on the
Mac mini on 2026-10-07 on sample tasks, and the task ceilings are now 256 MB
and 30 s (#180, PR #292; `lab/supervisor.py`). They have not yet done real
work through the deployed lab (README Status, M3 row).

**M4. Memory.** The agent may propose a memory; the owner approves it; the
owner can inspect, correct and delete everything (`lab memory` exists). Done
when a proposed memory cannot become active without a decision. About 3 days.
Status: done (#253, PR #259). The broker tool `memory.propose` stores a
pending proposal only, and `lab memory accept` with the operator's signature
is the only way it becomes memory.

**M5. Untrusted code in a container**
([#181](https://github.com/roshanaryal1/home-lab/issues/181)). One disposable
container per task, `--network none`, nothing mounted except the task
workspace. Done when a hostile script cannot reach the network or the host's
files. This comes before any imported skill can run code. About 1 to 2 days.
Status: done. The executor is built (#181, closed 2026-10-06), and the
pre-registered M5 claim ran against the real Apple container on the Mac mini
on 2026-10-07: 30 hostile scripts, 0 failures (PR #279;
[PREREGISTRATION-SAFETY.md](PREREGISTRATION-SAFETY.md), Results, with its
stated limits).

**M6. Skills and interoperability.** Read skills in the agentskills.io format
and speak MCP, each through the broker. An imported skill is only a
candidate; it becomes active on the owner's signed promotion, and its code
runs only in M5's container. Highest risk in the list. About 1 to 2 weeks.
Status: built in code. Skill import as candidates and the pre-registered M6
claim, 36 cases, 0 failures (#254, PR #260); an active skill's scripts run
only in the container (#255, PR #262); MCP through the broker (#256, PR #261);
both tools granted to reviewed handlers at the approve tier (PR #266). A
hostile MCP test server was checked under real Seatbelt on the Mac mini on
2026-10-07 (PR #281). The `skillrun` and `mcp` steps of the session script
have not been run on the Mac mini
([2026-10-06 session](reviews/2026-10-06-mac-session.md) lists them as not
run, and no later report runs them).

**M7. Install and first release.** The install guide finished with a
fresh-user test on another Mac (#188), a tagged release, and this roadmap
updated with what actually happened. About 2 days. Status 2026-10-08: not
done. `docs/INSTALL.md` exists (PR #196), and a newcomer's read-through is an
open pull request (#310), but the fresh-user test has not been run (#188). No
tag or release exists. A draft of the first release notes is in
[RELEASE-NOTES-DRAFT.md](RELEASE-NOTES-DRAFT.md); tagging, the release and a
DOI are the owner's steps (#83, [ops/release.md](../ops/release.md)).

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
The first runs are recorded there under Results: M6 (36 cases) and M2 (32
cases) on 2026-10-01, and M5 (30 cases, on the real container) on
2026-10-07, each with 0 failures.

## What this does not try to do

- Match the features or the community of OpenClaw or Hermes Agent
  (`docs/COMPARISON.md`). The features it does take, through the broker, are in
  `docs/FEATURE-PLAN.md`.
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
   channel the lab already alerts on. Installed on the Mac mini on
   2026-10-07 (PR #289).
3. Which three tools first? #240 built workspace files, read-only git and
   web fetch with summary.
4. Who reviews security independently, and when? Not answered.
5. Which option in the 2026-10-08 memo on #189: ship (b) as v0.1, wrap
   Hermes Agent, or hold the product and finish the research? Not answered.
