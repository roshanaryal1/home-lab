# Home lab: research findings, paper strategy, and build plan

**Roshan Aryal · 2026-09-25 · v1, pre-execution**

> **Status, 2026-09-28:** this is the original research document and is kept as written. It is not the current plan or status. For what is built, see the README status table and `docs/ARCHITECTURE.md`; for what remains and is parked on the Mac mini, see `ops/mac-mini-setup.md`.

This document exists because the decision was made to research before building.
Nothing in the build changes until the decisions at the end are made.

Every external claim here was checked against a primary source on 2026-09-25.
Where something could not be verified, it says so.

**Superseded framing, 2026-09-26:** this document's "intelligence plane"
(§4, Phase 2) is the predecessor of what
[ADR 0004](decisions/0004-operating-system.md) names as three separate
planes: observation, routing, publishing. ADR 0004 is the current target
and adds a correction this document does not have: one heavy inference
slot, not the wide concurrency implied by "24/7 operation". Read this
document for the research and defect history; read ADR 0004 for the
current build target.

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
on ModernBERT-large, about 33 to 39.5 ms per question per its model card.
**Kev-9B** is a LoRA adapter on the 9B `Qwen3.5-9B-Base`, also Apache-2.0.
On the same out-of-domain development items it scores 0.822 against Jev's
0.857, a gap of 3.5 points. Its 0.852 is on a locked test Jev was not scored
on, so comparing 0.852 with 0.857 (as an earlier version of this page did) is
not a like-for-like gap. Figures read from the Kev-9B and Laya model cards on
2026-09-29.

**Why this changes our architecture.** The reference architecture has a router
and a policy plane. The default assumption was that an LLM makes those calls.
A typed decision model is a better fit on three axes at once:

| | LLM making the call | Typed decision model |
|---|---|---|
| Latency | seconds | ~35 ms |
| Memory | competes for the one heavy slot | 421M (Laya), runs alongside; Kev-9B's 9B base does not |
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

`approvals` and the capability tiers exist in the schema (`lab/migrations/`). Nothing in
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

### 3.1 Correction: the roadmap already exists

An earlier draft of this section was wrong. It reconstructed the portfolio from
a memory summary instead of reading `paper/portfolio.md` in the llm-architects
repo, which is the actual roadmap and has been since 2026-09-01. The lesson is
the one already written into the standing rules: a summary is a hypothesis, the
repo is the source of truth.

The real roadmap, and its live status:

| # | Paper | Status |
|---|---|---|
| P1 | *LLMs as Systems Architects* | in peer review, Cureus art. 21832 |
| P2 | *The Rater Is Stale* | in progress, dataset done, harness pending |
| P3 | *We Asked the Models, Then We Built It* | **was blocked on hardware. The M6 arrived 2026-09-22. Now unblocked.** |
| P4 | *Reasoning Mode Beats Model Identity* | ready to run, needs no hardware |
| P5? | architecture-from-spec benchmark | conditional, gated on the v2 rubric proving portable |

Plus two unrelated papers under review: the browser-retrieval serialisation
ceiling, and the NZ road-crash severity analysis.

So the portfolio is six to seven papers, not four, and it was planned before I
arrived.

### 3.2 The thing that changes today's priorities

**P3 is the home-lab paper, and its clock has started.**

`portfolio.md` defines P3 as: build the P1-synthesised reference architecture on
a real 32 GB M6 and run it 24/7, reporting measurements nobody currently has.
Its definition of done requires:

- **at least 6 weekly eval runs**, so at least six weeks after the system works
- a failure taxonomy with **at least 20 real incidents** from real logs
- `memory_budget.py` predictions **checked against measured RSS**
- cheap A/Bs: MLX vs llama.cpp, 32K vs 64K context, speculative decoding on and
  off, one resident model vs swap-per-task

Three consequences worth being explicit about:

1. **The Phase 0 defect fixes are on a paper's critical path**, not just
   engineering hygiene. Six weekly runs cannot start until the supervisor stops
   duplicate-executing tasks.
2. **Instrumentation has to be in place before the first run, not retrofitted.**
   A failure taxonomy needs the logs that produced it. Incidents that happen
   before instrumentation exists are lost data.
3. **P3 was always the long pole.** `portfolio.md` said so on 2026-09-01 and
   told you to start procurement early. The hardware is here; the pole is now
   the build.

### 3.3 My three proposals, re-scored against the existing roadmap

`portfolio.md` carries an explicit **anti-salami-slicing rule**: every spin-off
must stand on its own new data or experiment. Re-checking my proposals against
it:

**Dropped. My "P7, what breaks in thirty days" was P3.** Silent failure
accumulation, recovery across restarts, intervention frequency: those are P3's
reliability measurements, already specified. Proposing it separately was exactly
the least-publishable-unit split the rule forbids. It is one section of P3.

**Survives: typed decision models as the authorization gate.** New experiment
(swap the authorizer, measure against the public 97-task / 629-security-test
injection environment), new data, no overlap with P1 to P4. Ten-day-old
technology, so it is the most time-sensitive thing on the list.

**Survives: does external policy enforcement prevent skill misevolution?** New
experiment on a public benchmark (SkillMisevo-Bench) against a named baseline
(SafeEvolve). No overlap. This is also the honest answer to the self-improving
agents request.

Both are candidates for the P5/P6 slots alongside the conditional
architecture-from-spec benchmark already noted in `portfolio.md`.

### 3.4 Recommended order

1. **Finish P2.** Dataset is done and twice-verified; it needs the harness,
   which is Senaka's half, or yours if Wednesday passes with no reply.
2. **Start P3 instrumentation now.** Not the writing, the logging. Every hour
   the machine runs uninstrumented is an hour of P3 data not collected.
3. **Slot in P4 whenever.** It needs no hardware, the v2 rubric anchors and
   prompt-v2/v3 already exist, and it is explicitly described as cheap. It is
   the best candidate for a fast output while P3's six weeks elapse.
4. **Typed decision models, in parallel with Phase 2.** Time-sensitive, and the
   experiment is a swap plus a measurement.
5. **Skill misevolution, after the policy plane exists**, since the intervention
   being tested is the policy plane.

### 3.5 The "publish something small every two weeks" question

Peer-reviewed publication in under two weeks essentially does not exist in
computer science. Verified:

| Venue | Realistic time |
|---|---|
| MDPI journals | ~2-4 weeks review, ~1-2 weeks publish, ~4-6 weeks total |
| IEEE Access | 6-8 weeks, the rigorous-fast gold standard |
| JOSS (software) | median **32 days**, fastest recorded 2 days |
| arXiv preprint | announced Sun-Thu, **1-3 days** allowing moderation |
| Zenodo artifact | **immediate** DOI |

The goal is reachable only by separating *output* from *peer-reviewed
publication*:

**Track A, fast, every 2 to 3 weeks.** A citable artifact: an arXiv preprint, a
Zenodo release with a DOI, or a dataset descriptor. Counts on a PhD application
as a preprint or released artifact, and establishes priority, which matters most
for the decision-model work.

**Track B, slow, months.** The peer-reviewed version of the same work.

`venue-strategy.md` already commits P2 to TMLR plus a NeurIPS-2026 eval-workshop
4-page short, and explicitly notes the workshop is non-archival so the full
paper can still go to TMLR. **That two-shot pattern is the template.** Apply it
to P4 and to the decision-model work as well.

Two additional fast formats worth adding:

- **JOSS software paper** for home-lab once it is real. Review happens openly on
  GitHub and rewards exactly the engineering rigour Phase 0 commits to.
- **Zenodo dataset descriptors** for the entity dataset, benchmark fixtures, and
  P3's telemetry. Immediate DOI, genuinely citable.

### 3.6 Extending four papers to six

`portfolio.md` plans P1 to P4 and names one conditional fifth. Two additions
reach six without violating the anti-salami rule, because each needs its own new
experiment rather than a re-cut of the existing corpus:

**P5, architecture-from-spec benchmark and leaderboard.** Already named in
`portfolio.md` as conditional. Its gate is that the v2 rubric proves portable,
which P1's v2/v3 runs and P4 both test. So P4 is not only cheap, it is the thing
that unlocks P5. Worth knowing when scheduling P4.

**P6, typed decision models as the authorization gate.** New experiment, new
data, and it plugs directly into P3's policy plane rather than competing with
it: the thing being measured is a component P3 needs anyway. Most
time-sensitive item in the whole portfolio, since the technology is ten days
old.

**Reserve, P7, skill misevolution under external enforcement.** Equally
legitimate, but it needs the policy plane to exist first, so it naturally
follows P6 rather than running beside it.

That gives six firm plus one reserve, all from the same line of work, each with
its own experiment.

### 3.7 Short-form writing: the actual fast track

This is the right answer to the two-week question, and better than chasing fast
journals. Public writing is same-day, compounds, and reaches the PIs being
emailed far more reliably than a workshop paper does.

**What can be written now, from work already finished:**

| Piece | Source | Ready |
|---|---|---|
| Eleven LLMs designed the same system. Here is what they agreed on. | P1 consensus matrix | now |
| An LLM flagged 14 real tools as hallucinations. Zero were fake. | P2 seed finding | now |
| I tested my own task queue and found it leases 19 jobs when it can run 1. | the defect confirmation above | now |
| OpenClaw uses 19x the memory of NanoBot for no completion advantage. | arXiv 2608.27886 | now |
| Every self-improving skill variant we have measured produced unsafe artifacts. | arXiv 2608.12851 | now |
| Build log, weekly | P3 in progress | from Phase 0 |

The build-log series is the highest-value one, because **it doubles as P3's raw
material.** P3 needs a failure taxonomy with 20+ real incidents from real logs.
Writing up each week's incidents as they happen produces both the blog post and
the paper's evidence, from the same work, with dates attached.

**Suggested cadence:** one short piece per two weeks, alternating between a
finding and a build-log entry. Cross-post the same text: personal site as
canonical, LinkedIn and X as distribution, Medium or dev.to optional.

**One real constraint to respect.** The browser-retrieval serialisation-ceiling
paper is under **double-blind review**. Posting identifiable content about it
during review can breach anonymity policy at some venues. Two rules:

1. Nothing public about the serialisation-ceiling paper until it is decided.
2. For everything else, check the venue's preprint and publicity policy before
   posting. TMLR explicitly permits preprints; conference policies vary and some
   restrict publicity during review windows.

Neither rule blocks any of the six pieces listed above, since none of them
touch the double-blind paper.

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

### Phase 5: 24/7 operation, and P3's measurement window opens

launchd, independent watchdog, backups with a tested restore, dashboard,
emergency stop.

**This is where P3's six-week clock starts**, so the instrumentation has to be
working before the first eval run, not added afterwards. Weekly eval suite,
failure taxonomy from real logs (target: 20+ incidents), `memory_budget.py`
predictions checked against measured RSS, and the four A/Bs named in
`portfolio.md`.

### On "uncensored"

Worth separating two things that the word blurs. **Model choice and
conversational style: yours, entirely.** Open-weight models, your own system
prompts, no vendor's content policy in the middle. **Unrestricted access to your
accounts, files and OS: a different thing**, and the measured 16.0% to 35.3%
skill-misevolution figure is the reason to keep the gate even when the model is
fully open. The gate is what makes leaving it running overnight reasonable.

---

## 5. Decisions needed from you

1. **Paper order.** P3 is unblocked and is the long pole; P4 is cheap and
   needs no hardware. My recommendation: finish P2, start P3 instrumentation
   immediately, slot P4 in during P3's six weeks, and run the decision-model
   experiment in parallel with Phase 2 because it is time-sensitive.
2. **Two-track publishing: agreed?** Specifically, are preprints and DOI'd
   artifacts acceptable as the fast-cadence output?
3. **Registered reports for P5 to P7?** Slower to start, removes null-result
   risk.
4. **Repo public when?** Enterprise-grade hardening is worth doing either way;
   this only changes timing and whether secret scanning becomes free.
5. **Heavy model**: still open from the original study, still needs deciding
   and benchmarking, still must not be chosen by default.
