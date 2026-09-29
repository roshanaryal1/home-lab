# ADR 0004: this is an operating system, not a worker pool

**Status:** accepted as the framing. Component decisions still open.
**Date:** 2026-09-26.

Prompted by Roshan stating the goal plainly: *"We're not making 24/7 AI
workers that work on my Mac mini, I am making an AI operating system."*

That is a different target from what is currently built, and the
difference is worth stating precisely rather than treating it as
enthusiasm for the same thing.

## What is built today

A task execution substrate. It is genuinely good at what it does:

- a SQLite queue with expiring leases, fencing and idempotency
- a bounded worker pool that cannot be starved by runaway spawning
- a capability gate with four tiers and parameter-bound approvals
- Seatbelt process isolation, verified on macOS 26.5.1 and 27
- 114 tests, CI on two platforms

Every one of those is a **mechanism**. You hand it a task, it runs the
task safely. It has no opinion about what task should exist.

## What an operating system adds

An OS does not wait to be told what to run. It notices that something
happened, decides what that means, and schedules work accordingly. Three
subsystems are missing, and they are missing entirely rather than being
half-built.

### 1. The observation plane: nothing currently notices

Roshan's example: *"we are working on something and then we found a very
interesting blog or work, maybe there will be a thing that needs to be
posted."*

Today that noticing is done by a human, in a chat window. Nothing in the
lab watches the work stream. There is an append-only event log in
`schema.sql`, and nothing reads it looking for meaning.

What has to exist: agents subscribed to the event stream that emit
**proposals**, not actions. A merged PR that closed a bug with a
reproduction is a proposal for a post. Three related bugs with a common
root cause is a proposal for a blog. A measurement nobody has published
is a proposal for a paper.

The proposal is a queue row like any other, and it goes through the same
capability gate. That is the whole trick: autonomy comes from generating
work, not from bypassing controls.

### 2. The artifact router: deciding what a thing is worth

Roshan already specified the tiers in the `writing` repo: post, blog,
journal or conference paper. The routing rule is **evidence weight**, and
it is not a judgement call:

| Evidence | Route |
|---|---|
| One incident, one commit, a lesson | post |
| A pattern across several incidents, with a mechanism | blog |
| A measurement, a baseline, a result that survives a control | paper |

The router's output is a draft plus a route plus the evidence chain that
justified the route. Roshan's own rule applies unchanged: journal and
conference output is PhD level, short form can be any quality. So the
router must refuse to route thin evidence upward, and the *gate* for that
is the evidence chain, not a quality score.

### 3. The publish plane, which is blocked on a real problem

Roshan: *"if it needs to be logged in you can just ask ID and password,
if you give ID and password you should log in yourself."*

The instinct is right and the mechanism should be better than asked for.
Handing an agent a password puts the password in the agent's context,
where it reaches the model, the logs, and any page the agent later reads.
That is the worst possible place for it.

**Issue #15, the secret broker, is exactly this and is already filed.**
The shape: Roshan authorises a credential once, into the broker. The
broker mints a scoped, short-lived token per invocation and injects it at
the tool boundary. The agent never holds the secret and cannot print it.
The CLI already redacts secret-looking keys, which is the same boundary
seen from the other side.

So the honest answer on self-publishing: **it is blocked on #15, and #15
is the right thing to be blocked on.** Not on willingness.

The second half of Roshan's rule is already implemented and does not need
changing: *"the agent suddenly asks if the work is irreversible."* That is
the four-tier capability gate. Posting publicly is `APPROVE`. Sending mail
is `APPROVE`. Reading and drafting are not. The gate's job is to keep the
approval surface small enough that Roshan is only interrupted for things
that actually matter.

## The correction that matters

Roshan: *"there are hundreds of agents working on one project."*

**Hundreds of concurrent agents is not achievable on this machine, and
the framing needs adjusting before it drives a design.**

ADR 0001 sets the budget: 32 GB unified memory, 11.5 GB reserved for OS,
browser, workers and indexes, leaving **20.5 GB for weights and cache**.
Measured on the M6 (2026-09-30), the chosen model holds 17,180 MB with its
weights loaded, leaving about 3.3 GB, roughly 16K tokens, of context. That
is **one** heavy inference slot. Not two.

So the real ceiling:

- **hundreds of queued tasks**: yes, trivially, the queue is built for it
- **hundreds of concurrent inferences**: no, and no scheduler fixes it
- **useful parallelism**: a small number of workers, where only some need
  the heavy model and the rest do I/O, retrieval, formatting and checks

This is not a limitation to route around. It is the reason the bounded
worker pool exists, and it is why "spawn an agent per idea" was rejected
in the first place. The *appearance* of hundreds of agents comes from
depth over time, not width at an instant: a queue that stays full, work
that generates work, and nothing needing a human to start it.

If genuinely wide parallelism becomes the requirement, the answer is a
second machine or a cloud burst tier for non-sensitive work, decided
explicitly. It is not something the M6 will do because the scheduler got
cleverer.

## Interconnection

Roshan: *"everything should be interconnected."*

The mechanism is the evidence base in `SUBSYSTEMS.md` §1, with typed
edges: supports, contradicts, refines. If P2 produces a finding and a P3
incident contradicts it, that contradiction is an edge, and an edge that
contradicts published work is itself a proposal, routed by the same
router.

ADR 0003 already settled the store: SQLite plus `sqlite-vec` plus FTS, no
graph database as a runtime dependency, graphify offline on corpora. That
decision stands and this changes nothing about it. Temporal validity
matters more here than anywhere: a proposal built on a fact that expired
is how an autonomous system publishes something wrong.

## Decision

**Accept the OS framing. Build the three planes in order, each behind the
existing gate. Correct the concurrency expectation now.**

Order, and why:

1. **Observation plane.** Nothing else can be autonomous until something
   notices. Cheapest to build, and safe by construction because it only
   emits proposals.
2. **Artifact router.** Needs proposals to route. Output is a draft plus
   an evidence chain, reviewed by a human at first so the routing rule can
   be corrected before it is trusted.
3. **Publish plane.** Blocked on #15. Do not build it before the broker,
   and do not work around the broker by putting credentials anywhere an
   agent can read them.

## What would change this

- Measured memory materially below 16.7 GB, which would allow a second
  inference slot and change the concurrency story.
- The routing rule misclassifying in practice, which would mean evidence
  weight is the wrong signal and it needs a human in the loop for longer.
- #15 proving that scoped tokens are unavailable for a platform Roshan
  wants to publish to, in which case that platform stays manual rather
  than getting an exception.

## Still open, not decided by this ADR

- ADR 0001 is now measured for memory, speed and tool calls on the M6
  (2026-09-30); the 50-call comparison against a second candidate model is
  still open.
- Issues #14, #16, #27 are open and are all controls this design leans on.
- The mini has no dedicated non-admin account yet.
