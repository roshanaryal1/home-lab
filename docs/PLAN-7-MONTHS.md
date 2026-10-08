# Seven-month plan: v1.0 and a public launch

Written 2026-10-09 (NZDT) for [#328](https://github.com/roshanaryal1/home-lab/issues/328). Month 1 is October 2026 and month 7 is April 2027. On #189
the owner chose to ship v0.1 first, then add features month by month through the broker. This
plan says what "production grade" and "launch" mean, what happens in each month, and who does
each part.

Three kinds of worker appear below:

- **Container**: a coding session without the Mac mini. It writes code, tests and docs, and opens
  pull requests.
- **Owner**: anything that needs `sudo`, the Mac mini, a decision, a release, money or another
  person.
- **Mac mini**: a run on the target machine, started by the owner.

The features come from [FEATURE-PLAN.md](FEATURE-PLAN.md) (#326). The research comes from #321.
Effort guesses are guesses, not measurements.

## What v1.0 must pass

v1.0 ships only when every check below passes. Each is a yes or no.

**Install and upgrade**

1. A built wheel installs and imports in a clean environment, checked in CI (#329).
2. `lab --version` prints the release version (#330).
3. Three people who are not the owner install it on their own Macs from the written guide, and
   `lab doctor` passes for each (#188, feature 1).
4. `lab update` moves a running lab from the previous release to this one without losing data,
   and rolls back with one command (feature 2).
5. Migration tests start from every released schema version, and all of them pass.

**Running and recovery**

6. The Mac mini runs the release candidate for 30 days in a row. The nightly self-test is green
   every night (#270), and every failure in that time recovered without the owner's hands, or is
   in the incident catalog with its fix.
7. A backup holds everything needed to restore the lab, and a restore into service is drilled
   once a month on the Mac mini (#67).
8. The power-pull, network-unplug and interrupted-task drills pass on the Mac mini (#77, #79,
   #91).

**Safety**

9. M2, M5 and M6 are rerun on the release commit, each with a sealed record and 0 failures.
10. Every v1.0 feature that adds a new way for outside text to cause an action has a claim
    registered before its code, and that claim passed.
11. #70 is decided and done.
12. An outside security review has read the code and the boundary. Every finding is fixed or
    has a public decision. Details stay private until the fix ships.

**Engineering**

13. CI has the wheel check, a dependency audit, a software bill of materials, release
    provenance, and coverage floors on the safety modules.
14. The CLI never prints a raw traceback for a known error (#331).
15. Logs rotate, and metrics are on loopback (feature 4).

**Docs and policy**

16. A CHANGELOG and the versioning and compatibility rules, approved by the owner (#332).
17. Install, upgrade and troubleshooting docs, each tried by someone who did not write it.
18. SECURITY.md states how to report a problem and how fast the owner answers.

## What "launch" means

home-lab collects no telemetry, so the launch is judged by what people choose to report.

- **Release.** v1.0 tagged by the owner, with release notes and a DOI (#83, `ops/release.md`).
- **Story.** One write-up: the personal agent whose safety boundary is on by default and
  measured in public. It shows the measured claims with their records, and states the
  weaknesses from [COMPARISON.md](COMPARISON.md).
- **Demo.** A short screen recording: install, pair the chat, ask for a task, approve it with
  the signature, read the audit log.
- **Where.** The owner picks the places to post. Candidates: Show HN, the owner's blog, and
  local-model communities.
- **Support.** Issue templates for bug reports and install reports, and a stated response time.
- **Measures, set by the owner before launch.** Proposed: 10 beta users by March, 3 install
  reports from strangers in the first month after launch, and no unfixed high-severity security
  report older than 14 days.

## The plan by month

Features 1 to 6 in [FEATURE-PLAN.md](FEATURE-PLAN.md) are required for v1.0. A missing one holds
v1.0 back until it is merged and its claim, if it has one, has passed. Features 7 to 16 go in if
they are on time. The cut rule is for them: one that is not merged, with its claim passed, by
15 March 2027 moves to after v1.0.

### Month 1: October 2026. Close the base and ship v0.1

Tracked in [#338](https://github.com/roshanaryal1/home-lab/issues/338).

Container:

- Merge the open research and docs work: the H1b interval (#323), sealed M2 and M6 records
  (#324), the incident catalog (#325), the comparison and feature plan (#327).
- Fix the first audit findings: the wheel (#329), the `lab` command (#330), one-line CLI errors
  (#331), CHANGELOG and versioning rules (#332), and the self-test code half of #270.
- P3 tools: the weekly eval job, the memory budget check, and drafts for the A/B tests and an
  exploratory H2 rerun (#321).

Owner and Mac mini:

- The steps in [STATUS.md](STATUS.md), "Waiting on the owner", in order: job logs, redeploy, the
  backup's Full Disk Access (#287), the two #312 checks, the heartbeat check, the drills.
- Install the weekly eval job, so P3's six weeks start.
- Run H1c ([H1C-RUN-PLAN.md](H1C-RUN-PLAN.md)).
- Choose a person for the independent H1 label check (#321).
- Tag v0.1 when the base is closed (#83).

Done when: v0.1 is tagged, and the weekly eval job has made its first record.

### Month 2: November 2026. Install, update, diagnose

Tracked in [#339](https://github.com/roshanaryal1/home-lab/issues/339).

Container:

- Features 1 to 3: `lab doctor` (#347), `lab update` with backup and rollback, `lab security audit`.
- Upgrades: a backup before every migration, and migration tests from each released version
  (#348).
- Backups: hold everything a restore needs, and restore into service, not only to a file.

Owner and Mac mini:

- Weekly eval runs 2 to 5.
- Memory measurements at 8K, 16K and 37K context, for the memory budget check.
- A/B 1, MLX against llama.cpp, once its design is approved.
- The M5 rerun with a sealed record, and M6's second condition in the real container.
- The first fresh-account install on a second Mac (#188).

Done when: v0.2 has `lab doctor` and `lab update`, and one fresh install has passed.

### Month 3: December 2026. Metrics, schedules, a second channel

Tracked in [#340](https://github.com/roshanaryal1/home-lab/issues/340).

Container:

- Feature 4, metrics on loopback. Feature 5, owner-defined schedules, with its claim registered
  first. Feature 6, a second chat channel, with the M2 cases extended to it first.
- Coverage floors on the safety modules. Log rotation for the launchd jobs (#349). A
  `config show` command.
- Code for A/B 3 (a draft model for speculative decoding) and A/B 4 (a model swap manager).

Owner and Mac mini:

- Weekly eval run 6, which closes P3's six weeks if the job started in October.
- A/B 2, 32K against 64K context. It needs the owner's call on the admission budget.
- The decision on P4 (#321).

Done when: v0.3 ships features 4 to 6 with their claims passed, and P3 has six weekly records.

### Month 4: January 2027. Memory and skills, supply chain, writing

Tracked in [#341](https://github.com/roshanaryal1/home-lab/issues/341).

Container:

- Features 7 to 10 if on time: session search, the user profile file, safe skill drafting, a
  curated skill index.
- Supply chain: a dependency audit, a software bill of materials, release provenance and a
  shell script check in CI.
- The model server under the lab's own supervision.
- Draft the P3 paper from the records.

Owner and Mac mini:

- A/Bs 3 and 4.
- Choose and book the outside security reviewer, and agree the scope.
- Start the 30-day run on the release branch.

Done when: v0.4 ships, the P3 draft exists, and the review is booked.

### Month 5: February 2027. Beta

Tracked in [#342](https://github.com/roshanaryal1/home-lab/issues/342).

Container:

- Features 11 to 14 if on time: more local runtimes, a hosted model behind opt-in, bounded
  delegation with its claim, and voice notes.
- A written macOS support matrix, and a runtime check for it.
- Fix what the beta users report.

Owner and Mac mini:

- Invite 5 to 10 beta users, each on their own Mac.
- The security review runs.
- The 30-day run finishes.
- Submit the P3 preprint.

Done when: v0.5 beta is out, the 30-day run has passed, and at least 3 beta installs are
reported.

### Month 6: March 2027. Release candidate

Tracked in [#343](https://github.com/roshanaryal1/home-lab/issues/343).

Container:

- Features 15 and 16 if on time: the one-line install script and the browser in the container.
- Fix the review findings.
- Finish the docs: install, upgrade, troubleshooting, a shorter README. Move owner-only scripts
  and session records out of the shipped package.
- Draft the launch write-up and the release notes.

Owner and Mac mini:

- No new features after 15 March.
- Rerun M2, M5 and M6, and every new claim, on the release candidate commit.
- Record the demo.

Done when: v1.0-rc1 passes every check in "What v1.0 must pass" except the tag itself.

### Month 7: April 2027. v1.0 and launch

Tracked in [#344](https://github.com/roshanaryal1/home-lab/issues/344).

Owner:

- Tag v1.0, publish the release and its DOI (#83).
- Post the write-up where the owner chose.
- Answer reports within the stated time.

Container:

- Triage incoming issues, and prepare a patch release within two weeks if one is needed.
- Update [ROADMAP.md](ROADMAP.md) and this plan with what actually happened.

Done when: v1.0 is public and the launch measures are being counted.

## Research to finish

From #321, in order. None of these reruns or edits a registered result.

| Item | What finishes it | Who | Month |
|---|---|---|---|
| H1c | One run per [H1C-RUN-PLAN.md](H1C-RUN-PLAN.md), reported beside H1 | Owner, Mac mini | 1 |
| H1b interval | Merged with its sealed input (#323) | Container | 1 |
| M2, M6 records | Merged (#324) | Container | 1 |
| Independent H1 labels | A second person's blind labels and an agreement table | Owner chooses the person | 1 to 2 |
| M5 record, M6 second condition | Sealed records from the Mac mini | Owner, Mac mini | 2 |
| H4 provenance | The record, or a dated note that it is lost | Owner, Mac mini | 2 |
| OSF check | What the registration holds, read on the OSF site | Owner | 2 |
| P3 weekly runs | Six sealed records, at least six days apart | Mac mini | 1 to 3 |
| P3 memory budget | Predictions against measured footprint, in a record | Container, Mac mini | 2 |
| P3 incidents | At least 20 real incidents (#325 lists 27) | Container | 1 |
| P3 A/Bs | Four records against a design approved before the runs | Owner, Mac mini | 2 to 4 |
| H2 key order | Exploratory run, if the owner approves the draft | Owner, Mac mini | 3 |
| P3 paper | A draft in month 4, a preprint in month 5 | Container drafts, owner submits | 4 to 5 |

## Risks

- **The owner's time on the Mac mini.** Most month 1 and 2 work needs `sudo` or the machine. If
  it slips, P3's six weeks and the 30-day run slip with it. The container keeps every owner step
  as a short, ordered list in STATUS.md.
- **One machine.** The Mac mini is the only target. A hardware fault stops the runs. The
  install test on a second Mac also checks that the guide works away from it.
- **Scope.** Sixteen features is more than one owner can review in five months. The cut rule
  above protects the date.
- **The model.** Features must not depend on the model's judgment for safety. Rules and the
  owner's signature decide.
- **Fast competitors.** OpenClaw and Hermes Agent ship weekly. home-lab does not race them on
  features. It competes on a boundary that is on by default and measured.
- **The review's cost.** An outside security review may cost money. The owner decides the
  budget in month 4. Without a review, check 12 fails and v1.0 waits.
