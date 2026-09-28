# ADR 0006: authority rules (Rule of Two) and the rollout of research agents

**Status:** accepted. **Date:** 2026-09-29. Item 4.1 (#68). Builds on ADR
0004 (proposals, never actions) and feeds gates G1 to G6 of
`docs/reviews/Home_Lab_Improvement_Plan_v2_2026-09-28.pdf`.

## Context

The lab is about to read content it did not write (issue text, pages,
documents) and later to act on the world. The failure that matters is
prompt injection: hostile text steering a process that can see something
valuable and can send it somewhere.

Meta's *Agents Rule of Two* (October 2025) states the constraint plainly:
in one session, an agent should hold **at most two** of

- **A** untrusted input: content an outsider could have written;
- **B** sensitive data: a secret, or private data;
- **C** external action: the power to change or send something outside
  the lab.

With all three, an injection can read and exfiltrate or act. With two,
one leg is missing and the attack has nowhere to go. Where a task truly
needs all three, a human is the missing leg, per action.

Effective authority is the **intersection** of operator delegation, agent
scope, task scope, input origin, tool policy and current approval. A
model or classifier may *suggest* a route; it never widens that
intersection.

## Decision 1: which two each plane may hold

| Plane | A untrusted input | B sensitive data | C external action | Notes |
|---|---|---|---|---|
| Observation (reads events, closed issues, merged PRs) | yes | no | no | Emits proposals only (ADR 0004). No tool beyond its own workspace. |
| Artifact router (drafts, evidence chain) | yes | no | no | Output is a draft plus evidence; reviewed by a human. |
| Research worker (fetches pages through the gateway, 4.3) | yes | no | yes, fetch only | No secret is ever present. Egress is deny-by-default and audited. |
| Coding worker (own repository) | no | yes | limited | Input comes from operator tasks. Push and delete need a per-call approval. Untrusted code to run goes to a container (ADR 0007). |
| Publish plane (8.6) | no | yes | yes | Input is only a hash-approved draft: review severs leg A. Any edit needs a new approval. |
| Operator CLI (approvals) | no | yes | no | Approval power is the operator's alone (4.5). |

Reading the table: no row has a yes in all three columns. A row that
needs a third leg is a design change and a new ADR.

## Decision 2: enforcement, not documentation

`lab/authority.py` encodes the rule and `Supervisor._run_task` applies it
before the approval gate:

- `sensitive_data` and `external_action` are declared at handler
  registration (trusted code) and by the tools granted (`TOOL_LEGS`, a
  table that tests require to cover every broker tool; an unlisted tool
  counts as external). A task cannot remove either.
- `untrusted_input` is true unless the task carries an explicit operator
  mark. Real origin, lineage and a checkable mark are item 4.2 (#69).
  **Until then the mark is only as strong as the queue's write access**,
  so the rule guards against a mis-wired handler, not against an
  attacker who controls the queue.
- A task that would hold all three is cancelled with an
  `authority_refused` audit event. No approval is offered: approving one
  action does not make the combination safe.

## Decision 3: rollout stages for research agents

Each stage is entered only when its gate has passed, and each has a
condition that returns the system to the previous stage.

| Stage | What agents may do | Legs held | Enters after | Evidence required to advance | Falls back if |
|---|---|---|---|---|---|
| 0 Read-only summarizer | Read the queue and event log, write a summary into the workspace. Dummy data. | none of A, B, C beyond local files | G1: core guarantees hold, R01 to R11 green | Summaries reproduce from stored events | any tool call outside the workspace |
| 1 Tools with traced evidence | Fetch external pages through the gateway, record each claim with its source (#90). Proposals only. | A, C (fetch); never B | G2: attack tests pass, fetch gateway and connectors in place | Attack harness (4.7) shows zero approval-required attack successes; every claim opens its exact source | a claim without a source; an egress denial not audited |
| 2 Supervised execution | Run code and tests in isolation, produce patches and drafts. Every effect approved. | B with C limited by approval; A only through the container tier | G4: runs unattended, restore drill passed; G3 model measured | Two weeks of unattended runs with no unreviewed effect; drills logged | a lease loss or unknown operation that is not reconciled |
| 3 External actions | Publish reviewed drafts through scoped connectors with receipts. | B and C, A severed by review | G5 (one event gives one reviewed draft) and G6 chosen items | Lost-response test never duplicates a post; every publish has a receipt | any edit after approval; a receipt mismatch |

Research completion uses bounded, observable stop rules, not the model's
own say-so: a search budget, a wall-clock ceiling, every material claim
supported, and no unresolved high-severity conflict. "Insufficient
evidence" is a first-class outcome (#33).

## Consequences

- New tools must be classified in `TOOL_LEGS` in the same change that adds
  them; the test fails otherwise.
- The fetch gateway (#14) and connectors (#15) land in stage 1 and 3
  respectively and must not carry a secret and untrusted input at once.
- Nothing here changes what is built now: no external tool or secret
  exists, so the rule cannot fire in production yet. It has a test that
  fires it deliberately.

## Not decided here

- How origin is proven (4.2).
- Whether the container tier is affordable next to the heavy model (ADR
  0007, parked on the M6).
