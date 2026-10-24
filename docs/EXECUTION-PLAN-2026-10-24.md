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
| P0 | [#79](https://github.com/roshanaryal1/home-lab/issues/79): emergency stop, alert and signed recovery | Review tests and document drill evidence format, including the split-key resume workflow fixed in [#408](https://github.com/roshanaryal1/home-lab/issues/408) | Stop an active dummy task, verify no later tool effect; network outage raises an alert within ten minutes; operator demonstrates a valid **signed** resume on the live database after stop, without disclosing its private key to the lab account | Timestamped on-target stop/no-effect/alert/**signed-resume** evidence; no unexpected side effects |
| P0 | [#91](https://github.com/roshanaryal1/home-lab/issues/91), [#77](https://github.com/roshanaryal1/home-lab/issues/77): power and interrupted work | Audit existing recovery scripts; do not rerun destructive drills remotely | Supervised power interruption, FileVault boot/unlock assessment, resumed task and artifact recovery | Verified logs and no blind replay of non-idempotent actions |
| P0 | [#235](https://github.com/roshanaryal1/home-lab/issues/235), [#287](https://github.com/roshanaryal1/home-lab/issues/287), [#318](https://github.com/roshanaryal1/home-lab/issues/318) | Check docs and tests against intended daemon identities and backup launcher | Confirm actual keepawake account, **only the dedicated backup launcher** has Full Disk Access, **Terminal's Full Disk Access is turned off and verified absent**, the subsequent scheduled overnight backup still passes, and root-owned log handling is reviewed | Runtime evidence for account privileges, privacy grants, actual backup and logging boundary; not only merged code |
| P1 | [#188](https://github.com/roshanaryal1/home-lab/issues/188): installability | Review docs and test checklist | At least one initial fresh-account or fresh-Mac run for #188, with pass/fail, safe teardown and skipped assumptions; then recruit **three people other than the owner** for v1.0 | #188 needs an actual signed-off validation record; **v1.0 independently requires three non-owner installs on their own Macs, each passing `lab doctor`**, not only green CI |
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

The existing [seven-month plan](PLAN-7-MONTHS.md), **What v1.0 must
pass**, remains the authoritative 18-item checklist. No subset of the
following can be treated as release readiness; record evidence for every item,
against the exact proposed release commit:

1. Clean wheel installation and module import in CI (#329).
2. `lab --version` reports the intended release version (#330).
3. **Three non-owner people** install on their own Macs using the guide and
   pass `lab doctor` (#188). One owner-run fresh-account test is insufficient.
4. Update from the prior release and one-command rollback without data loss
   are demonstrated, including actual operator ownership steps.
5. Migration tests run and pass from **every released schema version**.
6. **30 consecutive days** of release-candidate observation on the Mac mini,
   nightly self-test green throughout; each failure is recovered or catalogued
   with evidence of its fix.
7. Every monthly restore includes all required state and a verified
   restore **into service**, including task artifacts (#67).
8. Power-pull, network-unplug and interrupted-task drills pass on the Mac mini
   (#77, #79, #91), with a valid signed-resume path after emergency stop (#408).
9. M2, M5 and M6 are rerun on the **release commit** with **sealed records and
   zero failures**, not inferred from earlier commits.
10. Each new external-input/action feature has its safety claim registered
    before implementation and passed on the release candidate.
11. The operator/worker approval boundary in #70 is **decided and proved**.
12. An **outside security reviewer** has read the code and deployed boundary;
    every finding is fixed or has a documented release decision, with sensitive
    details kept private until safe to disclose.
13. CI includes wheel verification, locked dependency vulnerability audit,
    SBOM, release provenance and per-module safety coverage floors; required
    jobs actually gate merging in GitHub settings.
14. Known CLI errors yield bounded one-line errors, not raw tracebacks (#331).
15. Logs rotate in the deployed launchd jobs and the metrics endpoint is
    verified **loopback only**.
16. CHANGELOG, versioning and compatibility policy are approved by the owner.
17. Install, upgrade and troubleshooting documentation is **tried by a
    person who did not author it**, with observed behavior recorded.
18. SECURITY.md contains the actual vulnerability-reporting path and
    response commitment.

After these checks, **only the owner** chooses whether to tag, publish the
GitHub release and mint its DOI through Zenodo (#83). A draft paper, past green
tests, or CI running on a different SHA is not substitute evidence.

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
