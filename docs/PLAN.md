# Home lab: research findings, paper strategy, and build plan

**Roshan Aryal · 2026-09-25 · v1, pre-execution**

This document exists because the decision was made to research before building.
Nothing in the build changes until the decisions at the end are made.

Every external claim here was checked against a primary source on 2026-09-25.
Where something could not be verified, it says so.

---

## 0. What I could not verify

- **The two X/Twitter links.** Both return HTTP 402 to an automated fetch. I
  have not seen the video or the post. Anything about them below is absent, not
  summarised from guesswork. If they matter, paste the text.
- **Exact arXiv moderation delay for cs.AI.** arXiv announces Sunday to
  Thursday and states that moderation "may result in delays" without publishing
  a typical duration. Plan for 1 to 3 days, not same-day.

---

## 1. Market research: what actually changed

### 1.1 Jev, and why it matters more than the agent frameworks

This was the most consequential find, and it is not an agent framework at all.

**Jev** is a closed commercial API from TypeSafe AI, released **2026-09-15**. It
is a *decision model*, not a text generator: you give it application state plus
a predefined question, and it returns a **typed answer with probabilities** that
code can consume directly. Priced at USD 0.042 per million input tokens, output
unmetered.

The open reimplementations appeared within about 24 hours. The leading one is
**Laya** (Convai Innovations, Apache-2.0, released 2026-09-18): 421M parameters
on ModernBERT-large, **32.8 to 39.5 ms per question on a T4**, 19,301 GitHub
stars. **Kev-9B** reaches 0.852 accuracy against Jev's 0.857, a gap of half a
percentage point, also Apache-2.0.

**Why this changes our architecture.** The reference architecture has a router
and a policy plane. The default assumption was that an LLM makes those calls.
A typed decision model is a better fit on three axes at once:

| | LLM making the call | Typed decision model |
|---|---|---|
| Latency | seconds | ~35 ms |
| Memory | competes for the one heavy slot | 421M, runs alongside |
| Output | free text, needs schema repair | typed by construction |
| Injection surface | can be talked out of a decision | output space is constrained, not instructed |

That last row is the interesting one and it is an open research question, not a
settled fact. See paper P5 below.

### 1.2 The *Claw ecosystem, measured rather than described

**OpenClaw** hit GitHub 2026-01-30 and is the reference point: ~430,000 lines of
TypeScript, roughly 1 GB of memory. The ecosystem forked along different
constraints:

| Project | Approach |
|---|---|
| OpenClaw | most complete, heaviest |
| Nanobot | ~4,000 readable lines of Python, built for hackability |
| ZeroClaw | Rust runtime, 3.4 MB binary |
| NanoClaw | container-first, security above everything |
| PicoClaw | targets ~USD 10 hardware, air-gapped capable |

**The number that matters most**, from *Resource Constraints and Performance in
Agentic AI Systems* ([arXiv 2608.27886](https://arxiv.org/abs/2608.27886)),
which compared OpenClaw and NanoBot head to head:

> Full task completion: **OpenClaw 31%, NanoBot 25%**, no statistically
> significant advantage for either. But OpenClaw took longer on 83% of prompts,
> with geometric mean ratios of **2.98x execution time and 19.44x peak memory**.

Read that carefully. The heavyweight uses roughly **nineteen times the memory
for no measurable completion advantage**, and the state of the art completes
under a third of tasks. That is the real baseline we are building against, and
it is much lower than the marketing suggests.

### 1.3 Agent frameworks, current state

- **LangGraph**: the production choice where work must be auditable and
  resumable, because of explicit state graphs, checkpointing and interrupt
  primitives. Closest in spirit to what we already built by hand.
- **CrewAI**: 5.2M monthly downloads, role-playing paradigm, fast to stand up.
- **AutoGen**: **in maintenance mode as of mid-2026.** Successors are Microsoft
  Agent Framework 1.0 (GA April 2026) and the community fork AG2. Do not build
  on AutoGen.
- **n8n**: fastest path for automation-shaped agents; pairs with a stateful
  service rather than replacing one.
- **OpenHands, Browser Use**: executor-layer tools, candidates for step 6, not
  backbone choices.

**This validates the original decision.** The study's adjudicated position was
that the supervisor should be our own code, with a framework used only inside
workflows where its durability semantics earn their place. AutoGen going into
maintenance eleven months after being a default is exactly the risk that
position was hedging against.

### 1.4 Self-improving agents: the finding that changes the ask

You asked for self-improving, self-updating agents. The current literature says
that is measurably unsafe by default, and I would rather tell you now than after
it is built.

From *Practice Makes Unsafe: Skill Misevolution in Self-Improving LLM Agents*
([arXiv 2608.12851](https://arxiv.org/abs/2608.12851)):

> Across 25 agent-method configurations, **all 21 evolved skill variants
> produced unsafe artifacts**, and 15 caused harm in fresh sessions. Malicious
> task exposure raised unsafe carryover from **16.0% to 35.3%**.

The mechanism is worth understanding: an agent that learns from its own
successes optimises for *task completion*, not safety. So *"an unsafe success
can become reusable policy after its triggering input disappears."* The unsafe
shortcut outlives the situation that produced it.

Their mitigation, **SafeEvolve**, is a wrapper that repairs unsafe skill content
and controls reuse: unsafe retrieval down 26.7 points, fresh-session harm down
17.3 points, benign performance essentially unchanged (0.4 points).

They also released **SkillMisevo-Bench** and **SkillMisevo-Gym**, which means the
measurement apparatus already exists and we do not have to build it.

**So: yes to self-improving agents, but skill evolution has to be gated the same
way tool calls are.** That is not caution for its own sake, it is a measured
16.0% to 35.3% degradation. It is also, conveniently, a paper (P6).

### 1.5 Benchmarking: crowded, with specific gaps

I checked whether "benchmark local agent platforms" is novel. It largely is not:
**ReliabilityBench** covers consistency, perturbation robustness and
chaos-engineering-style fault injection; there is a public environment with
**97 tasks and 629 security tests** for prompt injection on tool-using agents;
*Engineering Reliable Coding Agents* ([arXiv 2608.13867](https://arxiv.org/abs/2608.13867))
and *Where Reliability Lives* ([arXiv 2609.03192](https://arxiv.org/abs/2609.03192))
both exist.

**The gap that remains open** is duration. Everything above measures behaviour
within a session or under injected faults. Nothing I found measures *multi-week
unattended operation*: silent failure accumulation, state drift, intervention
frequency per day, recovery success across many real restarts. That gap is
where P7 lives, and it is defensible precisely because it requires owning a
machine that runs for a month.

---

## 2. Confirmed defects in the current code

The external review listed four issues and correctly flagged them as
observations from reading, not confirmed failures. I tested the two that are
testable. **Both reproduce**, and the second is worse than described.

### Defect 1: unbounded leasing, CONFIRMED

The supervisor leases in a loop and creates an asyncio task per lease. The
semaphores gate *execution*, not *leasing*.

Measured: with `light_slots=1` and 20 queued tasks, after 100 ms the queue
showed **19 leased, 1 running**. Nineteen tasks were holding leases with no
prospect of running soon. A crash at that moment puts all nineteen through
recovery.

### Defect 2: no lease renewal, CONFIRMED, and it causes duplicate execution

Leases have a 300-second default TTL and nothing renews them.

Measured: a task leased with a 1-second TTL and still being actively worked was
seen by a second supervisor's `recover()` as abandoned, marked `interrupted`,
and **requeued to `queued`**. The first worker is still running it.

That is duplicate execution of a task that was never actually abandoned. It
defeats the single most important safety property in the design. The idempotent
flag limits the blast radius but does not fix the cause.

### Defect 3: approvals not enforced, CONFIRMED by inspection

`approvals` and the capability tiers exist in `lab/schema.sql`. Nothing in
`lab/supervisor.py` consults them. There is no authorization check between
leasing and execution. The tiers are currently documentation.

### Defect 4: isolation is a document, not a sandbox, CONFIRMED by inspection

`ops/mac-mini-setup.md` describes a non-admin user, scoped secrets and private
networking. None of it is implemented or enforced in code, and a non-admin
account alone is not isolation from other processes and services on the host.

**Assessment: the reviewer was right on all four.** Defect 2 should be treated
as the most serious, because it silently breaks a guarantee the rest of the
design depends on.

---

## 3. Paper strategy

### 3.1 Where the portfolio actually stands

You said "we finished one paper, we are working on the second." The record says
otherwise, and it is better news:

| # | Paper | Status |
|---|---|---|
| 1 | A Serialisation Ceiling in Browser-Native Semantic Retrieval | under review, double-blind |
| 2 | Adverse Weather, Darkness and Injury Severity in NZ Road Crashes | under review, *Journal of Road Safety* |
| 3 | LLMs as Systems Architects | *Cureus J. Comp. Sci.* art. 21832, Step 5 of 7 |
| 4 | The Rater Is Stale | in progress, TMLR target, Senaka co-author |

**You have four, not two.** Reaching five needs one more; the home-lab work can
credibly support three.

### 3.2 Proposed papers 5 to 7

Each is chosen because it (a) falls out of work the build requires anyway,
(b) has a gap I verified rather than assumed, and (c) has an existing public
benchmark or baseline so we are not also building measurement apparatus.

---

**P5: typed decision models as the authorization gate**

*Question.* Does replacing the LLM that authorizes a tool call with a small
typed decision model reduce prompt-injection-driven unauthorized actions?

*Why it is novel.* Jev is from 2026-09-15 and Laya from 2026-09-18. This is a
ten-day-old capability. The hypothesis, that a typed output space resists
injection structurally rather than by instruction, is plausible and untested.

*Why it is cheap.* The 97-task / 629-security-test injection environment already
exists. Laya and Kev-9B are Apache-2.0. The experiment is a swap plus a
measurement.

*Risk.* Fast-moving area; someone else may do it. This argues for doing it
first and fast, and it is a small experiment.

*Venue.* A security or eval workshop for the short version, then a full paper.

---

**P6: does external policy enforcement prevent skill misevolution?**

*Question.* SafeEvolve wraps the skill system. Our architecture puts authority
entirely outside the model. Does out-of-model enforcement prevent skill
misevolution better than an in-band wrapper?

*Why it is novel.* *Practice Makes Unsafe* establishes the failure and proposes
a wrapper mitigation. Nobody has tested capability-tier enforcement as the
control. It is a direct, comparable follow-up with a named baseline.

*Why it is cheap.* SkillMisevo-Bench and SkillMisevo-Gym are public. Baseline
numbers are published: unsafe carryover 16.0% to 35.3%, SafeEvolve recovering
26.7 and 17.3 points.

*This is also the honest answer to your self-improving-agents request*: build
it, gate it, and measure whether the gate works.

---

**P7: what breaks in thirty days, unattended operation of a local agent platform**

*Question.* What actually degrades when an agent platform runs unattended for
weeks? Silent failure accumulation, state drift, intervention frequency,
recovery success across real restarts.

*Why it is novel.* Verified gap. Existing work measures sessions and injected
faults, not duration.

*Why it is defensible.* It requires a dedicated always-on machine for a month.
Most researchers will not do this. You are doing it anyway.

*Baseline to compare against.* OpenClaw 31% / NanoBot 25% completion, 2.98x time
and 19.44x memory, from arXiv 2608.27886.

*Caveat to state up front.* n=1 machine. Frame as a longitudinal case study with
full instrumentation, not a general claim.

---

### 3.3 The "publish something small every two weeks" question

I have to be straight with you: **peer-reviewed publication in under two weeks
essentially does not exist in computer science.** Verified figures:

| Venue | Realistic time |
|---|---|
| MDPI journals | ~2-4 weeks review, ~1-2 weeks to publish, ~4-6 weeks total |
| IEEE Access | 6-8 weeks, described as the gold standard for rigorous-fast |
| JOSS (software) | median **32 days**, fastest recorded 2 days |
| arXiv preprint | announced Sun-Thu; **1-3 days** allowing for moderation |
| Zenodo artifact | **immediate** DOI |

So the two-week goal is achievable, but only if we stop equating *output* with
*peer-reviewed publication*. Run two tracks:

**Track A, fast, every 2 to 3 weeks.** A citable artifact. An arXiv preprint, or
a Zenodo release with a DOI, or a dataset descriptor. Real, citable, countable
on a PhD application as a preprint or artifact. This is where the "small thing"
cadence lives.

**Track B, slow, months.** The peer-reviewed version of the same work, submitted
once Track A has already established the claim and the date.

Track A also **establishes priority**, which matters a lot for P5 given the
ten-day-old technology.

Additional fast formats worth using:
- **JOSS software paper** for home-lab itself once it is real. Review happens in
  the open on GitHub and rewards exactly the engineering rigour we are
  committing to anyway.
- **Dataset descriptors** on Zenodo for the entity dataset, benchmark fixtures,
  and the 30-day telemetry from P7.
- **Registered reports** for P5 to P7. In-principle acceptance before results
  exist, which removes the risk of a null result being unpublishable. Worth
  considering given P7's n=1 exposure.

---

## 4. Build plan

Ordered by dependency, gated by evidence. No phase starts before the previous
phase's gate passes.

### Phase 0: fix what is broken (no new capability)

1. Bounded dispatch: lease only when a worker slot is free.
2. Lease renewal plus ownership fencing: a worker that lost its lease must not
   be able to commit a result.
3. Approval enforcement between lease and execute.
4. Regression tests for all three, including the two failing cases already
   reproduced above.

**Gate.** The two confirmed defects have tests that failed before the fix and
pass after. Two concurrent supervisors cannot double-execute a task. A protected
action cannot run without a valid, unexpired, parameter-bound approval.

### Phase 1: the execution broker

Workers stop touching the filesystem, shell, and network directly. They submit
typed tool requests. Per-task workspaces, path confinement, network default-deny.

**Gate.** Path traversal attempts fail. A worker cannot read outside its
workspace. Every execution produces an artifact manifest and audit event.

### Phase 2: intelligence plane

Model adapter, one heavy and one light model, measured. **Plus the P5
experiment**: typed decision model as router and policy evaluator.

**Gate.** Two or three heavy candidates measured on identical fixtures with real
peak-memory numbers, not advertised parameter counts. The heavy-model decision
gets written down with its reasoning.

### Phase 3: first worker, research only

Read-only. Every claim traceable to a stored source. No sending, no purchasing,
no account actions.

**Gate.** Injection content in retrieved pages does not become instructions.

### Phase 4: coding worker, then the rest

Isolated workspace, tests must pass, patch output, approval required to push.

### Phase 5: 24/7 and the P7 study

launchd, independent watchdog, backups with a tested restore, dashboard,
emergency stop. Then the instrumented 30-day run.

### On "uncensored"

Worth separating two things that the word blurs. **Model choice and
conversational style: yours, entirely.** Open-weight models, your own system
prompts, no vendor's content policy in the middle. **Unrestricted access to your
accounts, files and OS: a different thing**, and the measured 16.0% to 35.3%
skill-misevolution figure is the reason to keep the gate even when the model is
fully open. The gate is what makes leaving it running overnight reasonable.

---

## 5. Decisions needed from you

1. **Papers 5 to 7: approve, replace, or reorder?** My ranking is P5 first
   (most time-sensitive), P6 second (strongest baseline), P7 last (needs the
   machine running anyway).
2. **Two-track publishing: agreed?** Specifically, are preprints and DOI'd
   artifacts acceptable as the fast-cadence output?
3. **Registered reports for P5 to P7?** Slower to start, removes null-result
   risk.
4. **Repo public when?** Enterprise-grade hardening is worth doing either way;
   this only changes timing and whether secret scanning becomes free.
5. **Heavy model**: still open from the original study, still needs deciding
   and benchmarking, still must not be chosen by default.
