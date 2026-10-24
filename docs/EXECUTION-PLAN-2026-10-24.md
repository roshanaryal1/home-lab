# HomeLab execution plan: 24 October 2026

**Scope:** order the remaining work, preserve research provenance, and turn the
existing seven-month roadmap into a reviewable backlog. This plan is a snapshot,
not a replacement for [PLAN-7-MONTHS.md](PLAN-7-MONTHS.md), the decisions in
[FEATURE-PLAN.md](FEATURE-PLAN.md), or the owner-only handoff in
[STATUS.md](STATUS.md). GitHub issue checkboxes are not proof of deployment.
Nothing in this PR merges, deploys, publishes or authorizes a privileged action.

## Rules for every workstream

1. **Keep each change on a focused branch and open a PR. Do not merge.** Do
   not directly push implementation changes to main. Do not change release
   tags, repository protection, machine settings or public destinations.
2. **Evidence before claims.** A passing unit test proves the simulated case,
   not the boundary on the Mac. Use the exact commit, environment, date,
   test method, expected result and observed result when recording a drill.
   Do not invent measurements or describe CI as an independent security audit.
3. **Research protocol wins.** Never edit frozen case sets, labels, hashes or
   preregistration text to improve a result. H1 is not supported. H1b is
   exploratory and H1c has not run. Keep the AI-assigned-label limitation.
4. **Do not build through a security decision.** For #70, first agree
   which identity owns the queue and which executes untrusted code. For
   browser/hosted model/cloud runner, settle the open design decisions and
   register the required safety claim before any execution feature ships.
5. **Green means the complete required matrix passed on the PR head.** Check
   each PR's final Linux and macOS test run, package, workflow audit and
   secret scan. Additional workflows are not required merge checks until
   repository protection explicitly requires them.
6. **Exit each stage only with evidence.** Track `ready for review`,
   `CI green`, `owner decision pending`, `Mac-only proof pending` and
   `released` separately; never use `done` to mean all five.

## Stage A: stabilize the existing PR queue (now)

The following PRs exist and remain **unmerged** at the snapshot:

| PR | Change | Gate to finish review | Owner action |
|---|---|---|---|
| [#427](https://github.com/roshanaryal1/home-lab/pull/427) | Citation date follows publication | Final CI, inspect release metadata and checklist | Choose when to publish; DOI/Zenodo #83 |
| [#429](https://github.com/roshanaryal1/home-lab/pull/429) | Read-only `config show` | Final CI, confirm secrets cannot appear in output | None until merge/deployment |
| [#431](https://github.com/roshanaryal1/home-lab/pull/431) | Locked dependency audit and SBOM | Final CI, verify SBOM includes dev dependencies and audit failures remain failures | None until merge |
| [#433](https://github.com/roshanaryal1/home-lab/pull/433) | Shell syntax and static checks | Final CI, confirmed ShellCheck job and expected script scope | Configure required status check #434 |
| [#436](https://github.com/roshanaryal1/home-lab/pull/436) | Install troubleshooting and validation template | Final CI, link/document review | Run the fresh-Mac test #188 |
| [#438](https://github.com/roshanaryal1/home-lab/pull/438) | Redact credential keys inside approval arrays | Full security/regression suite, review remaining redaction limits | Deploy/recheck only after explicit approval |
| [#425](https://github.com/roshanaryal1/home-lab/pull/425), [#426](https://github.com/roshanaryal1/home-lab/pull/426) | Dependency bumps | Independent lockfile and CI checks; avoid overlapping upgrades | None until merge |

Do not silently assume these PRs have been merged. Issue #434 remains an
explicit settings change even when the ShellCheck workflow itself is green.
Avoid branching dependent changes from these PRs unless intentionally stacking
them and documenting that relationship.

## Stage B: close security and operational release blockers

| Priority | Task and dependency | Work possible in GitHub before the owner acts | Required hardware/owner proof | Exit gate |
|---|---|---|---|---|
| P0 | [#70](https://github.com/roshanaryal1/home-lab/issues/70): operator/worker database boundary | Review privilege architecture, present separated-identity options and regression test design *without assuming a deployment decision* | Choose OS/account/database architecture; prove untrusted worker cannot forge/consume an operator approval on the deployed Mac | Signed approvals still work; unsigned, fabricated, modified, expired and replayed approvals fail; agent has no route around the trusted verifier |
| P0 | [#79](https://github.com/roshanaryal1/home-lab/issues/79): emergency stop and alert | Review tests and document drill evidence format | Stop an active dummy task, verify no later tool effect; network outage reaches alert within the promised window | Timestamped on-target drill, no unexpected side effects |
| P0 | [#91](https://github.com/roshanaryal1/home-lab/issues/91), [#77](https://github.com/roshanaryal1/home-lab/issues/77): power and interrupted work | Audit existing recovery scripts; do not rerun destructive drills remotely | Supervised power interruption, FileVault boot/unlock assessment, resumed task and artifact recovery | Verified logs and no blind replay of non-idempotent actions |
| P0 | [#235](https://github.com/roshanaryal1/home-lab/issues/235), [#287](https://github.com/roshanaryal1/home-lab/issues/287), [#318](https://github.com/roshanaryal1/home-lab/issues/318) | Check docs and tests against intended daemon identities and backup launcher | Confirm actual keepawake account, Full Disk Access grant, subsequent overnight backup and root-owned log handling | Runtime evidence and security boundary written up, not only merged code |
| P1 | [#188](https://github.com/roshanaryal1/home-lab/issues/188): installability | Review docs and test checklist | Separate fresh account or Mac; record pass/fail, safe teardown and skipped assumptions | Real signed-off install record, not simply a green CI build |
| P1 | Supply chain and release trust (#83, #341, #434) | Audit/SBOM/ShellCheck PRs, provenance *design* and documentation | Enable required check, publish/tag/Zenodo only when owner authorizes | Version provenance, DOI and complete checks on release commit |

**Stop rule:** no security claim, signed release, broad external connector,
browser deployment, or public beta while P0 boundaries lack direct evidence.
No code-only PR can settle a physical permission boundary.

## Stage C: finish the research without rerunning registered hypotheses

This stage follows [#321](https://github.com/roshanaryal1/home-lab/issues/321).
Work in parallel only where the instrument/protocol permits it.

| Sequence | Research task | Cloud/container work | Owner/Mac proof |
|---|---|---|---|
| 1 | H1 results figure | Generate an accessible, deterministic figure from committed H1 and H1b records, verify counts, record hashes, explicitly label H1b exploratory. **No rerun.** | Independent *human* H1 label-check reviewer still to be chosen |
| 2 | H1c | Review frozen run plan and provenance fields only | Freeze and run the already-chosen constrained decoding option on Mac; report as exploratory, not a replacement |
| 3 | M5 and M6 | Prepare evidence schema/checklist, preserving registered claim definitions | M5 sealed record and real-container M6 second condition, with machine and commit |
| 4 | Operations paper P3 | Consolidate existing 27 incidents and existing memory/eval evidence into a draft with explicit missing cells | Six actual weekly eval records; measured 8K, 16K and 37K; four A/B runs only after methodology margins are agreed |
| 5 | Registrations | If OSF can be read, compare public registration IDs and immutable archives read-only; record source/date | Owner verifies OSF when access is unavailable; no registration alteration |
| 6 | Interpretation | Label registered failures and departures, report exploratory work separately | Independent review and publication decision |

The H1 record is 3/30 correct, 5 false promotions against 25/30 baseline;
H1b is 13/30 correct, 8 false promotions. The labels were AI-assigned and
are **not** independent human ground truth. Do not claim improvement over
the deterministic rubric or change registered H1's failure verdict.

## Stage D: feature implementation, only behind prerequisites

- **Already in code, not necessarily validated on the deployment:** owner
  schedules, loopback metrics, stalled-task check, public injection suite,
  doctor, update/restore plan, log rotation and audited event handling.
  Before new implementations, verify the target Mac has deployed them.
- **Design decision first:** browser-in-container (#369), hosted/cloud
  model pathway and one-line installer. Obtain explicit choices on network
  isolation, data sensitivity, attack surfaces and supported Macs. Register
  and run the safety claim before an input channel can call an effectful tool.
- **Feature PR order after the gate:** small, testable vertical slices with
  threat test -> code -> CI -> owner inspection -> Mac drill. A second
  channel must preserve pairing and M2. No feature bypasses the broker.
- **Defer:** multi-tenant enterprise stack #41, optional memory/profile/voice
  upgrades, and feature polish until baseline reliability, independent review
  and measured research work are not blocking a release.

## Stage E: release readiness and launch

The existing [seven-month plan](PLAN-7-MONTHS.md) remains the governing
schedule. Release requires a fresh install, reproducible wheel, upgrade and
rollback proof, 30-day candidate observation, successful power/network/restore
drills, registered safety-claim records on the release commit, independent
security review, and owner-approved release metadata. A draft paper and code
unit tests are not substitutes for these.

For every milestone, the handoff should contain: issue/PR links, exact SHA,
CI status, changed interfaces, security assumptions, evidence filenames,
measured outcomes, known limitations, and the next human decision.

## Next actions without merging

1. Open this execution-plan PR for review, **do not merge**.
2. Open a separate H1 figure PR using the committed records, no changes to
   sealed inputs.
3. Review and correct code-review findings on the open PRs. Leave each PR
   open until the owner authorizes merging.
4. For #70 and the hardware-only research/drills, prepare decision/evidence
   materials but never claim a physical test ran remotely.

**Scope control:** no background operation is implied by this document.
The checklist is an execution order for future explicit working sessions,
not a claim that they are already running.
