# Subsystem designs: research, skill evolution, and tool adoption

**2026-09-25.** Closes four gaps left open by `docs/PLAN.md`. Design only;
nothing here is built yet.

---

## 1. The research subsystem: Perplexity-like, but auditable

You asked for "Perplexity kinda work": ask a question, get a real researched
answer, not a guess. The reference architecture already specifies the shape,
store-then-verify plus a contradiction pass, every claim traceable to a stored
snippet. Current literature has sharpened that into something buildable.

### What the field settled on

*From Fluent to Verifiable: Claim-Level Auditability for Deep Research Agents*
([arXiv 2602.13855](https://arxiv.org/abs/2602.13855)) proposes claim-level
auditability as a first-class design target, measured on four axes:

- **provenance coverage**: what fraction of claims have a source at all
- **provenance soundness**: does the source actually support the claim
- **contradiction transparency**: are conflicts surfaced or quietly dropped
- **audit effort**: how hard is it for a human to check

The important structural idea is a **semantic provenance graph**: claims and
evidence as nodes, with *typed* edges carrying an entailment strength, not just
"cited". Edge types: `supports`, `contradicts`, `refines`, `prerequisite`.

The second important idea is timing. Validation happens **during** synthesis,
not as a review pass afterwards. Verifying at the end lets an early error
propagate through everything built on top of it.

### The design for us

Four stages, each writing to the evidence plane:

**Stage 1, decompose.** The question becomes explicit sub-questions with
acceptance criteria. Written down before any retrieval, so the shape of the
answer is not chosen after seeing convenient evidence.

**Stage 2, retrieve and store.** Every fetch is stored verbatim with URL,
retrieval timestamp and a content hash, before anything reads it. This is the
"store" in store-then-verify and it is what makes the result re-auditable
months later when the page has changed.

**Stage 3, extract claims with typed edges.** Each extracted claim points at a
stored snippet, with an edge type and a confidence. A claim with no stored
snippet behind it is not a claim, it is a guess, and is labelled as one.

**Stage 4, contradiction pass before writing.** Claims are checked against each
other. A `contradicts` edge is reported, never silently resolved by preferring
the more recent or more convenient source. Unresolved contradictions appear in
the output.

### The rule that makes it safe

**Retrieved content is data, never instruction.** A web page that says "ignore
your previous instructions" is a string we stored, not a command. This is the
same boundary as the tool broker: the model may reason about retrieved text and
may propose actions, but retrieved text cannot itself become an action.

### Why this is not just Perplexity

Perplexity gives you an answer with citations. This gives you a provenance
graph you can audit, contradictions surfaced rather than smoothed, and stored
snapshots so the answer stays checkable after the sources move. The point is
not to beat Perplexity at speed; it is to produce research whose evidence
survives scrutiny, which is what a paper needs.

**This subsystem is also P2's infrastructure.** Dating 195 entities against
primary sources is exactly this pipeline run by hand.

---

## 2. Safe self-improvement: skill evolution with a gate

You asked for agents that improve themselves and their skills. `PLAN.md` §1.4
established that unguarded self-improvement is measurably unsafe: across 25
configurations, **all 21 evolved skill variants produced unsafe artifacts**, and
malicious exposure raised unsafe carryover from 16.0% to 35.3%
([arXiv 2608.12851](https://arxiv.org/abs/2608.12851)).

The field's answer is not "don't", it is **governed skill lifecycles**. From
*SkillsVote* ([arXiv 2605.18401](https://arxiv.org/abs/2605.18401)) and the
lifecycle surveys:

- candidate changes are **proposed and validated**, never applied directly to
  active skills;
- admission is **evidence-gated**: a candidate must demonstrably improve
  performance on held-out tasks before it is retained;
- every skill carries **lineage**: origin, revision history, audit evidence,
  risk class, reuse outcomes;
- periodic maintenance **retires** skills that cross an unsafe-reuse threshold.

### The design for us

This is the approval gate, applied to skills instead of tool calls. Same
architecture, same reason.

```
agent completes a task
        |
        v
proposes a skill candidate        <- never writes to the active library
        |
        v
lineage recorded: which task, which trajectory, what it claims to do
        |
        v
held-out validation: does it improve performance on tasks it has not seen?
        |
        v
policy check: what capability tier does this skill require?
        |
   +----+----+
   |         |
autonomous   approve
tier         tier
   |         |
   v         v
auto-admit   wait for a human
   |         |
   +----+----+
        v
versioned into the active library, with a rollback point
        |
        v
reuse outcomes tracked; unsafe-reuse threshold retires it
```

**Four rules, each with a reason:**

1. **A skill never writes itself into the active library.** It proposes a
   candidate. This is the single most important rule, and it is the one the
   misevolution paper shows gets skipped.
2. **A skill inherits the highest capability tier of any tool it calls.** A
   skill that sends email is `approve` tier forever, no matter how routine it
   becomes. Frequency of use is not evidence of safety.
3. **Validation is on held-out tasks.** A skill that worked once on the task
   that produced it has demonstrated nothing. This is what separates evolution
   from unconstrained self-editing.
4. **Every skill is versioned with a rollback point**, because the failure mode
   is a skill that was fine and becomes harmful after a later revision.

### And this is paper P7

The open research question is whether enforcement *outside* the model beats the
in-band wrapper (SafeEvolve) that the literature currently proposes. We will
have built the external enforcement anyway. SkillMisevo-Bench is public. That
is a paper falling out of work already required.

---

## 3. Tool adoption: what to take, what to skip, what to watch

Systematic pass over everything named, with a decision rather than a summary.

### Adopt

| Tool | Where it goes | Why |
|---|---|---|
| **Laya or Kev-9B** (open Jev) | policy plane, router | 421M params, ~35 ms, typed output. Right tool for an authorization decision, and 10 days old so also paper P6 |
| **MLX** | intelligence plane | unanimous in the study for this hardware |
| **sqlite-vec + FTS5** | memory | study plurality; also what Hermes independently chose |
| **OpenHands** *or* **Aider** | coding executor, step 6 | study plurality was OpenHands, Aider 5/12. Pick one after benchmarking, do not run both |
| **Browser Use** | browser executor, later | only behind an isolated profile with a domain allowlist |

### Skip, with reasons

| Tool | Why not |
|---|---|
| **AutoGen** | in maintenance mode as of mid-2026. Building on it is choosing a dead dependency |
| **CrewAI** | role-playing abstraction over a problem we do not have. Our concurrency limits are hardware-driven, not role-driven |
| **LangGraph** | genuinely good, and the closest framework to what we built by hand. But we already have durable state, leases and recovery. Adopting it now means rewriting working code to gain checkpointing we already implemented. **Reconsider if the supervisor gets hard to maintain** |
| **AutoGPT** | superseded; independent testing found it does not recover from failure, it drifts |
| **n8n** | excellent for automation-shaped work, wrong shape for a policy-gated agent platform. Worth revisiting as a *trigger* source later |
| **OpenClaw, NanoClaw, ZeroClaw, PicoClaw, Nanobot** | reference points, not adoptions. Measured: OpenClaw completes 31%, NanoBot 25%, and OpenClaw uses 19.4x the peak memory for that |

### Hermes Agent: the one that deserves a real decision

[Hermes Agent](https://github.com/NousResearch/hermes-agent) (Nous Research,
released 2026-02-25) is the closest existing thing to what you asked for:
self-hosted persistent daemon, memory across sessions, scheduled tasks, 90+
skills, MCP support in both directions, runs local models, **and it writes its
own reusable skills from experience.** 248k stars.

It also chose SQLite with FTS5 for memory, independently arriving at the same
answer this study's consensus did. That is corroboration worth noting.

**The honest position:** Hermes already does most of what you described
wanting. If the goal were only "have a capable agent running", installing
Hermes would be the rational move and would save months.

**Why build anyway, stated plainly so the decision is deliberate:**

1. **The papers need the instrument.** P3 is a paper about building and
   measuring this. Installing someone else's daemon does not produce P3.
2. **Self-written skills are exactly the measured hazard.** Hermes writes its
   own skills. Unless it gates promotion the way §2 describes, the
   misevolution finding applies to it directly. That is not a criticism of
   Hermes, it is an open research question, and it is P7.
3. **Authority outside the model is the thesis.** It is easier to build that in
   from the start than to retrofit it into a 90-skill daemon.

**What to do about it concretely:** install Hermes on the mini as a *measured
baseline*, not as the platform. P3 needs comparison numbers and "we compared
against the leading self-hosted agent" is far stronger than "we built a thing".
This costs a day and strengthens a paper.

---

## 4. The X links: still unverified

Both links (`callanxai` and `hanakoxbt`) return **HTTP 402 Payment Required** to
automated fetching. Tried again 2026-09-25, same result. X requires paid API
access for programmatic reads.

I have not seen either. Nothing in any plan derives from them. If they matter,
paste the text or a screenshot and I will fold the content in.

---

## What this does not cover

Deliberately out of scope here, tracked in `PLAN.md`:

- The execution broker itself (Phase 1)
- Model adapter and swap manager (Phase 2, needs the M6)
- The dashboard and emergency stop (Phase 4)
- Phase 0's two confirmed defects, which block all of the above
