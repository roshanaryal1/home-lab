# Publication pipeline

**Living document. Uncapped backlog, gated output.**
Last updated 2026-09-25.

This is the shared working file for everything publishable across the
llm-architects line and the home-lab build. Add freely, promote carefully.

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

1. **Own new data or experiment**, not a re-cut of an existing corpus.
2. **A verified gap**, checked against current literature, not assumed.
3. **An existing benchmark or named baseline** where possible, so we are not
   also building the measurement apparatus.
4. **A venue that fits**, identified before the work starts.

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
| S4 | The heavyweight agent uses 19x the memory for no completion advantage. | arXiv 2608.27886 | ready |
| S5 | Every self-improving skill variant measured produced unsafe artifacts. | arXiv 2608.12851 | ready |
| S6 | Why the model must not be the system administrator. | the architecture's central rule | ready |

### Build log, weekly once Phase 0 lands

**This series is the highest-value one**, because it doubles as P3's raw
material. P3's definition of done needs a failure taxonomy with 20+ real
incidents from real logs. Writing each week's incidents up as they happen
produces both the post and the paper's evidence, dated, from one pass of work.

Suggested spine: one entry per phase gate, plus one per real incident worth
explaining.

---

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
- **Promoting an idea:** run the four gates, record the answers, move it up.
- **Finishing something:** update the status here in the same commit as the work.
- **Disagreeing with a gate result:** say so. The gates are a default, not a
  verdict.

## Open question for Roshan

`llm-architects-planning` (private) and `llm-architecture-eval` (public) hold
diverged copies of the same planning documents. The public one is newer; the
private one stops at the venue decision. Two repos with the same files and
different contents will cause a wrong-version error eventually. Worth deciding
which is canonical and retiring or re-syncing the other.
