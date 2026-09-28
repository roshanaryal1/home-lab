# ADR 0005: what we take from the multi-tenant enterprise platform proposal

**Status:** accepted, mixed. **Date:** 2026-09-28.

Two large external documents were brought for review, plus a stale local
snapshot checked and set aside. This records what survives contact with
home-lab's actual state: one user, one machine, a research programme
(LA-P3, LA-P6, LA-P7) that this infrastructure exists to serve, not to
outgrow before it produces a result.

## What was reviewed

**Document A**, a proposal that home-lab's architecture should become "a
zero-trust, multi-tenant enterprise AI operations platform": tenant and
organization identity, RBAC plus ABAC plus capability-based authorization,
SSO/MFA, a six-plane split (access, control, intelligence, evidence,
policy enforcement, adapters), per-tenant encryption, PostgreSQL as the
persistence target, and governance modeled on the NIST AI Risk Management
Framework.

**Document B**, a maturity assessment rating home-lab **3/10 toward "an AI
operating system that can operate a computer and handle essentially any
task a human can perform on it,"** plus a 7-phase roadmap (kernel,
computer perception and action, browser-as-subsystem, cognitive
architecture, universal skill system, multimodal world model, full AIOS),
and, within the same document, **a self-correction** arguing for a
narrower near-term boundary: "an AI-native control plane over an existing
OS," a typed primitive vocabulary (principal, capability grant, intent,
plan, action, observation, evidence, policy decision, resource lease,
checkpoint), and one concrete end-to-end vertical slice as the next real
increment, in preference to building the 7 phases as isolated layers.

**A stale snapshot**, `~/Downloads/Home Lab Architecture.html`, dated
2026-09-25. Predates the Phase 0 defect fixes and ADR 0004: it still shows
two open defects that are closed, and describes the heavy model as
unchosen when ADR 0001 has since chosen one. Superseded by
`docs/ARCHITECTURE.md`. Not adopted as a live document; noted here only
so it is not later mistaken for current.

## The single most important test applied to every proposed item

**Does this serve the one user this machine actually has, or does it serve
a hypothetical population of users that does not exist?** ADR 0004 already
corrected one instance of this exact mistake: reading "hundreds of logical
agents" as license to plan for hundreds of concurrent model calls, when
the hardware has one heavy inference slot. Document A's tenancy model is
the same shape of error at a larger scale: real, well-reasoned enterprise
architecture, aimed at a population of tenants that is currently one.

## Decision

### Adopted now

**1. Identity separation, formalized rather than newly built.**
`ops/mac-mini-setup.md` already specifies a dedicated non-admin account for
agent workloads. What Document A adds worth keeping: name the distinction
explicitly. **Platform owner** (Roshan, break-glass authority: rotating
secrets, changing policy, emergency stop) is not the same identity as
**operator** (what agents and workers run as day to day), even though both
map to one human. The rule is "elevate intentionally, temporarily, and
visibly," and it costs nothing new to state, since the non-admin account
was already the plan.

**2. The typed primitive vocabulary**, for design docs and future schema
work, not a rewrite today: **principal**, **capability grant**, **intent**,
**plan**, **action**, **observation**, **evidence**, **policy decision**,
**resource lease**, **checkpoint**. This gives ADR 0004's three planes
(observation, router, publish) and the existing gate (`lab/policy.py`,
`lab/broker.py`) one shared language instead of three independent
namings. Where the existing schema already has an equivalent (`action_hash`
in `lab/policy.py` is close to a capability grant; a task's `owner` field
is close to a principal), keep the existing name; use the new vocabulary
where nothing exists yet.

**3. Verification as evidence, not narration.** Every consequential
action should carry expected state, observed state, and a written
verification, matching the existing events table's shape rather than
trusting a model's claim that something worked. This directly shapes the
acceptance criteria for #32, #33, and #15.

**4. Build order correction: one vertical slice before building planes
in isolation.** Document B's self-correction is right, and it changes
what #32 and #33 should actually ship first. Filed as issue #39: observe
one real signal, propose it, route it by evidence weight, draft it, and
**stop before publishing**, which conveniently means this slice needs
nothing from #15. Proves the whole path is real before either plane is
generalized against an imagined interface.

### Deferred, not rejected

**Full multi-tenancy**: `tenant_id`/`org_id`/`project_id` on every row,
RBAC plus ABAC policy engine, SSO/MFA, per-tenant encryption domains,
PostgreSQL migration, two-person approval for policy changes, dev/staging/
production environment separation, SBOM inventory, a formal NIST AI RMF
governance process.

Every one of these is legitimate enterprise practice. None of them is
justified by a platform with one user. The cost is not abstract: RBAC,
ABAC, SSO, a multi-tenant schema and a PostgreSQL migration are months of
infrastructure work, and every month spent on it is a month not spent on
LA-P3's measurement window or LA-P6's typed-decision-gate experiment,
which are what this machine exists to produce.

**The full 7-phase AIOS roadmap**: computer perception (OCR, accessibility
trees), a universal computer action layer, three-tier browser automation
(DOM, accessibility, vision), a multi-agent role hierarchy, a multimodal
world model. Genuinely the right long-run direction, and not contradicted
by anything here. Not started now because it is a multi-quarter
commitment nobody has asked this machine to make yet, and issue #39's
vertical slice is the right-sized first real step toward it rather than a
7-phase commitment made in one sitting.

**The 3/10 maturity score itself**: useful as a candid gap-check, not a
number to chase. Document B's own **AIOS-Bench** idea, a leveled task
benchmark from deterministic through autonomous, is worth keeping as a
future measurement instrument for LA-P3 rather than something to build
now.

### Rejected, corrected

**"AI OS" does not mean a replacement operating system.** Document B
argues this against its own first framing, and it is right to: home-lab
does not need to own device drivers, a kernel, or a window compositor to
validate anything. It needs safe, portable, auditable authority over the
resources macOS and its applications already expose. This was already
home-lab's implicit position (`launchd`, Seatbelt, no custom kernel); it
is worth stating explicitly here so it is not re-litigated the next time
a proposal like this arrives.

## What would change this

- **A second real user.** Not a hypothetical one; someone who actually
  wants to use the platform. That is the trigger for pulling tenancy
  forward, not a calendar date or a sense that the architecture "should"
  support it eventually.
- **LA-P6 or LA-P7 specifically needing multi-principal isolation to be
  measured.** Possible but not currently the case; both experiments are
  scoped to a single-principal gate.
- **Issue #39's vertical slice revealing the three-plane split cannot
  work without tenant-level isolation.** That would be real, evidence-based
  grounds to pull tenancy forward rather than defer it. Absent that
  evidence, deferring is the decision.

## Related documents

- [`docs/decisions/0004-operating-system.md`](0004-operating-system.md), the framing this refines rather than replaces
- [`docs/ARCHITECTURE.md`](../ARCHITECTURE.md), current-state diagram, unchanged by this ADR
- [#39](https://github.com/roshanaryal1/home-lab/issues/39), the vertical slice this ADR commits to building next
