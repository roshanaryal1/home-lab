# Publication pipeline

**Living document. Uncapped backlog, gated output.**
Last updated 2026-09-25.

**Superseded for tracking, 2026-09-28.** The paper pipeline below (P1-P7)
is now tracked live in `roshanaryal1/research-pipeline` as `LA-P1` through
`LA-P7`, with a gate-checking bot (`scripts/check.mjs`) that fails on a
broken link, a stale status, or an overdue date. This file stays as the
original record and is still the right place for the reasoning behind
each paper; it is not where you check what is current. If the two ever
disagree, `research-pipeline` is correct.

This is the shared working file for everything publishable across the
llm-architects line and the home-lab build. Add freely, promote carefully.
Short-form (Track B, below) is **not** ported to `research-pipeline`; this
file and `roshanaryal1/writing` remain its source of truth.

## Why two tracks with different rules

**Short-form is uncapped.** Blog, LinkedIn, X, Medium. Fifty pieces is a
reasonable target. They cost hours, compound, reach people directly, and carry
no scarcity. Write as many as there are things worth saying.

**Papers are uncapped in the backlog, disciplined at the gate.** Not because
ambition is bad, but because `paper/portfolio.md` is right that reviewers reject
least-publishable-unit splits, and because for a PhD application six solid
papers beat twenty thin ones. The backlog can hold fifty ideas; what graduates
is what passes the gate.

### The gate a paper idea must pass

Roshan's standing instruction, 2026-09-25: **anything submitted to a journal or
conference must be PhD-level. Short-form can be any quality.** That asymmetry is
deliberate and the gates encode it.

1. **Own new data or experiment**, not a re-cut of an existing corpus.
2. **A verified gap**, checked against current literature, not assumed.
3. **An existing benchmark or named baseline** where possible, so we are not
   also building the measurement apparatus.
4. **A venue that fits**, identified before the work starts.
5. **PhD-level rigour**, which is not the same as scale. A four-page workshop
   paper can clear this bar and a long paper can fail it. Operationally it
   means all five of:
   - a claim stated precisely enough to be **falsifiable**, with the
     falsification condition written down;
   - **controls** that separate the claimed effect from the obvious
     alternative explanations;
   - a **pre-registered** analysis plan where results could otherwise be
     fished for, as P2 does;
   - **released code and data** sufficient for someone else to re-run it;
   - **threats to validity stated by us**, before a reviewer finds them.

Gate 5 is the one that decides whether a paper is worth writing at all. If an
idea cannot clear it, it is either a short-form piece or a section of a paper
that can.

Anything failing 1 is a section of an existing paper. Anything failing 2 gets
re-checked before effort goes in. Anything failing 3 is more expensive than it
looks and should be scheduled accordingly.

---

## Track A: papers

### Committed

| # | Paper | Status | Next action | Blocked on |
|---|---|---|---|---|
| P1 | *LLMs as Systems Architects* | peer review, Cureus art. 21832, step 5 of 7 | wait for editor | nothing |
| P2 | *The Rater Is Stale* | dataset done and twice-verified, harness pending | harness, issues #10 to #15 | Senaka, or go solo after 2026-10-01 |
| P3 | *We Asked the Models, Then We Built It* | **unblocked 2026-09-22, hardware arrived** | fix Phase 0 defects, then instrument | the two confirmed supervisor defects |
| P4 | *Reasoning Mode Beats Model Identity* | ready, needs no hardware | run the model x mode grid | nothing. This is the cheapest open item |

### Proposed, gate-checked

| # | Paper | Gate status | Why now |
|---|---|---|---|
| P5 | architecture-from-spec benchmark and leaderboard | conditional: needs the v2 rubric to prove portable, which P4 tests | P4 unlocks it, so schedule P4 first |
| P6 | typed decision models as the authorization gate | passes all four | **most time-sensitive item in the portfolio.** Jev shipped 2026-09-15, Laya 2026-09-18 |
| P7 | skill misevolution under external policy enforcement | passes all four | needs the policy plane to exist, so it follows P6 |

### Idea inbox, not yet gate-checked

Add anything here. No commitment implied. Promote only after checking the gate.

- Advertised model parameters vs measured resident memory on Apple Silicon.
  Small, fast, and P3 collects the data anyway. Possibly a section of P3 rather
  than its own paper: check gate 1 before promoting.
  **First data, 2026-09-30** (ADR 0001, "Measured on the M6"): the model
  matched its advertised size (17.2 GB) but not its advertised speed (about
  67 tok/s against "~100+"); cache costs about 200 KB per token, invisible
  to RSS because it sits in Metal allocations; Metal's 24.96 GiB ceiling,
  not total RAM, caps context (37K tokens fit, 75K failed); `mlx_lm.server` keeps old
  caches by default and ran out of memory on a request that fits alone.
  One machine, one model, one day: a build-log entry now, a finding only
  after the second candidate and repeat runs.
- A 4B model tied a 30B mixture-of-experts on the lab's own 24-task
  utility set (20 vs 19 of 24, 2026-09-30, `ops/mac-mini-setup.md` section
  14), and both fell for the same one-line injection (`inject-1`). Small
  task set, so a build-log observation, not a claim; worth a post only if
  it survives a bigger set.
- Real-model injection run: a coder model obeyed 2 of 8 hidden
  instructions (delete a file, fetch the cloud metadata address) and the
  broker stopped both (SECURITY.md, 2026-09-30). "The model will fall for
  it, so the controls cannot depend on it" with a measured example. Short
  post candidate for Track B.
- The inference server silently ignored the JSON-schema `response_format`
  (identical output with and without it), while the same schema pasted
  into the system prompt cut invalid tool calls from 23.5% to 2.0%.
  "Check that your constraint is enforced, not just accepted." Build-log
  entry, 2026-09-30, setup section 20.
- Vendor documentation as an unreliable narrator: the M6 bandwidth claim
  (170 GB/s real vs a surveyed system's "300+ GB/s") is one data point;
  P2's cutoff-documentation findings are another. May be a thread, may be thin.
- What a 24/7 agent actually attempts: a security review of real logged tool
  calls over N weeks. Currently specified as a P3 measurement.

---

## Track B: short-form

Target: one piece every two weeks, alternating a finding and a build-log entry.
Personal site canonical, LinkedIn and X for distribution.

### Writable now, from finished work

| # | Working title | Source | Status |
|---|---|---|---|
| S1 | Eleven LLMs designed the same system. Here is what they agreed on. | P1 consensus matrix | ready |
| S2 | An LLM flagged 14 real tools as hallucinations. Zero were fake. | P2 seed finding | ready |
| S3 | I tested my own task queue. It leases 19 jobs when it can run 1. | defect confirmation, 2026-09-25 | ready |
| S4 | The heavyweight agent uses 19x the memory for no completion advantage. | arXiv 2608.27886 (preprint) | ready |
| S5 | Every self-improving skill variant measured produced unsafe artifacts. | arXiv 2608.12851 (preprint) | ready |
| S6 | Why the model must not be the system administrator. | the architecture's central rule | ready |

### Build log, weekly once Phase 0 lands

**This series is the highest-value one**, because it doubles as P3's raw
material. P3's definition of done needs a failure taxonomy with 20+ real
incidents from real logs. Writing each week's incidents up as they happen
produces both the post and the paper's evidence, dated, from one pass of work.

Suggested spine: one entry per phase gate, plus one per real incident worth
explaining.

---

## Pre-registration

The plan for the home-lab evaluation (hypotheses, metrics, failure
categories, analysis plan) is `docs/PREREGISTRATION.md`, registered on OSF as
[osf.io/jfp74](https://osf.io/jfp74) on 2026-09-29 14:45:00 UTC at commit
`d8726b43`. First confirmatory results (same file, Results): H2 not
supported, H2b supported by its rule, H4 supported, H3 not supported for the
setting tested (prompt cache 1 against 4, no gain); H1 not yet run.
Two findings worth a build-log entry each: grammar-constrained decoding made
a rerun less repeatable, and the MLX 4-bit build corrupts text the GGUF
build of the same model copies correctly.

## Publicity constraints

**Hard rule: nothing public about the serialisation-ceiling paper until it is
decided.** It is under double-blind review, and identifiable public content
during review can breach anonymity policy.

For everything else, check the target venue's preprint and publicity policy
before posting. TMLR explicitly permits preprints. Conference policies vary and
some restrict publicity during the review window.

None of S1 to S6 touch the double-blind paper, so none are blocked.

---

## How to use this file

- **Adding an idea:** put it in the inbox. No gate check needed to add.
- **Promoting an idea:** run the five gates, record the answers, move it up.
- **Finishing something:** update the status here in the same commit as the work.
- **Disagreeing with a gate result:** say so. The gates are a default, not a
  verdict.

## Where things live, resolved 2026-09-25

| Repo | Visibility | Holds |
|---|---|---|
| `roshanaryal1/llm-architects` | public | the P1 paper artifact, data, analysis |
| `roshanaryal1/llm-architects-planning` | private | strategy: `paper/portfolio.md`, `paper/venue-strategy.md`, DMLR package, submission logistics |
| `roshanaryal1/home-lab` | public (since 2026-09-29) | the build, which is paper P3 |
| `roshanaryal1/the-rater-is-stale` | private | paper P2 |
| `roshanaryal1/writing` | private | short-form drafts, see below |

The earlier open question about which repo was canonical is resolved. The local
directory `llm-architecture-eval` turned out to have an **orphaned git history**
with no common ancestor with the live public repo, so nothing committed there
could ever have merged. It is archived at
`_archive-orphaned-llm-architecture-eval-2026-09-25`; the live repo is cloned at
`~/RnD/llm-architects/llm-architects`, matching its repo name. All ten files
unique to the orphan were verified byte-identical in the planning repo first.

## How short-form gets written

A dedicated private repo, `roshanaryal1/writing`, rather than drafting inside a
project repo. Reasons:

1. **Writing spans projects.** A post about the queue defect and a post about
   judge staleness come from different repos. Burying drafts in one of them
   makes the other awkward.
2. **Drafts should stay private until posted**, but the repo history is worth
   keeping afterwards: it is a dated record of the writing cadence.
3. **Cross-posting needs one canonical source.** Personal site, LinkedIn, X and
   Medium all render from the same file rather than three drifting copies.

Structure:

```
drafts/     work in progress
published/  posted, with date, canonical URL, and where it was cross-posted
assets/     images and diagrams
```

Each published piece carries front matter recording where it went and when, so
the cadence is measurable rather than remembered.
