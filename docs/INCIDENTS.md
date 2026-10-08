# Incident catalog for P3

This file lists the real incidents in the repository's records. P3 needs a failure taxonomy with at least 20 real incidents from real logs (`docs/PLAN.md`, section 3.2). This catalog lists 27.

## What counts as an incident

An incident is a real failure or defect found on the Mac mini or in real use. A fault injected on purpose does not count. The rules used here:

- Drill records in `ops/drills/log/` do not count, because their faults are injected on purpose.
- A rate from a benchmark or evaluation run is a result, not an incident. A single real failure counts.
- A finding from a test or a code review counts only when a record shows that it happened on the machine or in real use.
- Each row cites a record that was read. If the record is unclear, the item is listed at the end instead.
- A row about a security weakness has one neutral line and the issue or PR number only.

Dates are the dates the record gives. Session reports use the UTC start date. New Zealand time was 13 hours ahead in October, so a run that began on 2026-10-06 UTC was 2026-10-07 in New Zealand.

Commit hashes come from `origin/main`. The clone used for this file is shallow, so some older records are cited by file and section.

## Taxonomy

- **Install and deploy.** A failure while installing, pinning or deploying the code, its interpreter, its tests or its services on the Mac mini.
- **Permissions, keys and secrets.** A macOS privacy grant, file or account permission, approval key or bot token that did not keep its intended limit or was handled wrongly.
- **Data and backup.** A failure of the backup volume, the backup job or a stored copy of the database.
- **Supervision, scheduling and recovery.** A failure of the supervisor, the watchdog, a launchd job or the restart after a fault. No real incident is recorded here yet, because the failures in this area so far came from injected drills.
- **Model runtime.** A failure of the served model, its server or its memory budget in real use.
- **Measurement and research method.** A defect in how a run, label, reviewer reply, drill output or registered study was made, checked or reported.
- **Documentation and runbook.** A written or registered instruction that is wrong, ambiguous or missing a step, found on read-back or in use.

## Incident table

Oldest first, by the date the record gives the problem as found.

| id | date found | category | what failed | how found | impact | fix | evidence |
|---|---|---|---|---|---|---|---|
| INC-001 | 2026-09-26 | Install and deploy | The Mac mini's only interpreter was Python 3.9.6, which cannot run the project, because the project targets 3.13. | Interpreter check on the mini, recorded in the ADR. | The project could not run on the system Python. | ADR 0002, amendment 2026-09-29 item 2.1: uv managed Python 3.13.15. | `docs/decisions/0002-python-runtime.md`, "What prompted this" |
| INC-002 | 2026-09-26 | Install and deploy | `uv python pin 3.13` wrote only the minor version, so the pin did not fix the patch release. | Found on the deployment target and recorded as an amendment. | The rebuild property the ADR claimed was not delivered. | ADR 0002, amendment 2026-09-29 item 2.1: the pin names 3.13.15. | `docs/decisions/0002-python-runtime.md`, amendment dated 2026-09-26 |
| INC-003 | 2026-09-29 | Model runtime | The plain MLX 4-bit build of the heavy model changed text it only had to copy, such as file paths, by splicing in extra tokens. | Pre-registered H2b run, then a cause check the same day. | The plain H2b condition passed 28 of 70 tool calls. | Served build changed to the DWQ build on 2026-09-30. No PR in record. | `docs/decisions/0001-heavy-model.md`, "Measured on the M6 (2026-09-30)", point 5, `docs/PREREGISTRATION.md`, Exploratory |
| INC-004 | 2026-09-29 | Install and deploy | 10 of 1045 tests failed inside the container guest, because the slim image and the exported workspace lacked ps, process groups or a git checkout. | Test run inside the guest, measured on the mini. | The guest run was not clean. The record says these are not isolation faults. | None. The record gives no code change. | `docs/decisions/0007-isolation-for-untrusted-code.md`, "Measured on the M6 (2026-09-29)" |
| INC-005 | 2026-09-29 | Measurement and research method | A repeat of the grammar condition at temperature 0 passed 58 of 70, against 69 of 70 in the first run. | Rerun in the registered H2 run on the mini. | The H2 pass count is not repeatable at temperature 0. | None in code. Amendment 1, item 8 requires each rerun to be reported beside its first run. | `docs/PREREGISTRATION.md`, Results, H2 table |
| INC-006 | 2026-09-30 | Measurement and research method | Real-model evaluation runs were made before registration, because the setup checklist ran its sections out of order. | Noticed when the deviation was written up. | Results from those runs are exploratory and support no hypothesis. | Amendment 1, dated before registration. No PR in record. | `docs/PREREGISTRATION.md`, Deviations, Amendment 1 |
| INC-007 | 2026-09-30 | Model runtime | The served `mlx_lm.server` 0.31.3 never reads `response_format`, so its output was the same with and without it. | Source check and output comparison, recorded in Amendment 1. | Constrained decoding (H1c) could not run as designed. | None yet. H1c not run (commit 272b361, #308). #179 is open. | `docs/PREREGISTRATION.md`, Deviations, Amendment 1, item 1, `docs/PREREGISTRATION-AMENDMENT-2-REGISTRATION.md`, "H1b, exploratory (2026-10-08, #302)", commit 272b361 |
| INC-008 | 2026-09-30 | Model runtime | With the default prompt cache, one server ran out of Metal memory at 37K prompt tokens. The same size fits when run alone. | Memory series on the mini, one server, default settings. | The admission accounting assumed the cache was freed after each request, which was wrong. | Run the server with `--prompt-cache-size 1` (ADR 0001). No PR in record. | `docs/decisions/0001-heavy-model.md`, "Measured on the M6 (2026-09-30)" |
| INC-009 | 2026-09-30 | Model runtime | A request of about 75K tokens failed with Metal "Insufficient Memory" and pushed about 3 GB into swap. | Measured run on the mini. | The usable context is about 16K tokens, not the model's maximum. | Admission check refuses requests past the budget before the server is called (ADR 0001). No PR in record. | `docs/decisions/0001-heavy-model.md`, "Measured on the M6 (2026-09-30)" |
| INC-010 | 2026-09-30 | Permissions, keys and secrets | A recurring loop ran without the approval key during setup. Security item, neutral line only. | Found while setting up the lab account. | Security item. Fixed the same day. | #190 | `README.md`, "Security status" |
| INC-011 | 2026-10-06 | Permissions, keys and secrets | The lab account could list the operator's home folder. Security item, neutral line only. | Listing as the lab account, step 2 of the Mac session. | Security item. Fixed by a permission change. | #225, permission change run by the operator during the session. | `docs/reviews/2026-10-06-mac-session.md`, section 2 |
| INC-012 | 2026-10-06 | Install and deploy | The nightly self-test logged FAIL for the safety tests on each run, because pytest is not installed in the deployed environment, as the record states. | Self-test log read in step 5 of the Mac session. | The morning self-test reported FAIL. The alert hook sent a message for each failing run in the log. | #270, open. A by-hand run passed on 2026-10-07. The nightly run had not yet been seen. | `docs/reviews/2026-10-06-mac-session.md`, section 5, `README.md`, build step 10 |
| INC-013 | 2026-10-06 | Measurement and research method | The freeze drill's summary said the frozen supervisor was gone and a new one had started, while the same process was still running and the script resumed it. | Read from the drill output in the Mac session. | The summary contradicted the observed state. | Wording changed by pass 1 of the drill. #271 tracks the cause. | `docs/reviews/2026-10-06-mac-session.md`, section 7 and reading note 2, `docs/reviews/2026-10-07-mac-session-drills-pass-1.md`, section 1 |
| INC-014 | 2026-10-06 | Data and backup | The backup volume was mounted without owners, so root `chown` was refused and the backup failed. | Three failed backup attempts within one hour on the mini. | No backup was written in those attempts. | Owners turned on with `diskutil enableOwnership`, as the runbook says. Commit ee86cf0 (#282). | `docs/reviews/2026-10-07-mac-session-backup.md`, reading note 2, `ops/runbook-lab-account-and-daemons.md`, Step 4, "The backup folder (#67)" |
| INC-015 | 2026-10-06 | Data and backup | The encrypted backup volume had to be unlocked again after a remount. | The same three failed attempts. | The backup could not reach the volume until it was unlocked. | Unlock with `diskutil apfs unlockVolume`, as the runbook says. Commit ee86cf0 (#282). | `docs/reviews/2026-10-07-mac-session-backup.md`, reading note 2 |
| INC-016 | 2026-10-06 | Permissions, keys and secrets | Root `chown` on the backup volume was refused until the terminal app had Full Disk Access. | The same three failed attempts. | The backup folder could not be set up. | #67 for the one-off change. The terminal grant was still on at 2026-10-08. Turning it off is an open owner step. | `docs/reviews/2026-10-07-mac-session-backup.md`, reading note 2, `ops/runbook-lab-account-and-daemons.md`, Step 4, "The backup folder (#67)" and "Moving the backup's Full Disk Access to its launcher", step 7 |
| INC-017 | 2026-10-06 | Permissions, keys and secrets | A bot token was pasted into a chat by mistake. Secret item, neutral line only. | Noted in the alert session report. | The token was revoked and replaced before setup. | None in code. The replacement token is in use. | `docs/reviews/2026-10-07-mac-session-alert.md`, reading note 3 |
| INC-018 | 2026-10-07 | Install and deploy | The chat service, started before its config held the token and chat ID, exited with code 2 and logged that no chat was paired. | The operator saw the service state and log on the mini. | The service was down until the config was written and it was restarted. | None in code. The config was written and the service restarted. The install is recorded in #289 (commit 931a7a7). | `docs/reviews/2026-10-07-chat-bot-install.md`, "What was done" |
| INC-019 | 2026-10-07 | Permissions, keys and secrets | The first scheduled backup under launchd failed. macOS refused the lab interpreter access to the removable backup disk. | The failure alert reached the phone. The log showed the unable-to-open error. | No scheduled backup until the grant was given. | Interim grant on the interpreter, documented in commit 66b2427 (#284). Replaced by a launcher with its own grant (#287, PR #307). The move is an open owner step. | `README.md`, build step 9, `ops/runbook-lab-account-and-daemons.md`, Step 4, "The backup folder (#67)" |
| INC-020 | 2026-10-07 | Documentation and runbook | The registered Amendment 2 text has a missing space in a URL in the Study design field, and two Description paragraphs begin with spaces. | Read back through OSF's API on 2026-10-07. | Registered text cannot be edited, so the defect stays on record. | Errata note, commit 81663ca (#295). | `docs/PREREGISTRATION-AMENDMENT-2-REGISTRATION.md`, Errata 1 and 2 |
| INC-021 | 2026-10-07 | Measurement and research method | The first H1 run stopped before the candidate answered any case, because the ledger could not build case-14. | Failure of the H1 run on 2026-10-07, recorded in the registration note. | H1 needed a replacement case, decided before any model output. | 4308645 (#299) | `docs/PREREGISTRATION-AMENDMENT-2-REGISTRATION.md`, "Departure: one case could not be built (2026-10-07)" |
| INC-022 | 2026-10-07 | Measurement and research method | One AI reviewer's first attempt on the main cases was made with Search turned on. | Recorded in the registration note. | The record sets that attempt aside under a rule fixed before any comparison. | None in code. Set aside under the rule fixed on #84. | `docs/PREREGISTRATION-AMENDMENT-2-REGISTRATION.md`, "What happened (2026-10-07)", Retries and departures, item 1 |
| INC-023 | 2026-10-07 | Measurement and research method | A reviewer's second attempt turned the pasted message into an attachment that lost text in nine cases. | The attachment was compared with the frozen sheet. | The reviewer did not see the frozen sheet, so a third attempt was a departure. | None in code. The third attempt ran in a new chat, and the owner checked that every case was complete. | `docs/PREREGISTRATION-AMENDMENT-2-REGISTRATION.md`, "What happened (2026-10-07)", Retries and departures, item 3 |
| INC-024 | 2026-10-07 | Measurement and research method | An AI reviewer's reply for the spare cases was malformed. It was an essay, not one JSON object. | Format check of the reply. | One permitted retry was used. The first reply was not used. | None in code. The retry was made under the protocol. | `docs/PREREGISTRATION-AMENDMENT-2-REGISTRATION.md`, "What happened (2026-10-07)", Retries and departures, item 2 |
| INC-025 | 2026-10-07 | Documentation and runbook | A registered context sentence said that no AI reviewer had seen any case and no model had produced a label or proposal. The same registration discloses draft labels written earlier, so the sentence reads more widely than intended. | Read back through OSF's API on 2026-10-07. | Registered text cannot be edited, so the reading is recorded as an erratum. | Errata note, commit 81663ca (#295). | `docs/PREREGISTRATION-AMENDMENT-2-REGISTRATION.md`, Errata 4 |
| INC-026 | 2026-10-08 | Permissions, keys and secrets | The backup's Full Disk Access grant was given to a shared interpreter, so it reached more processes than the backup job. Security item, neutral line only. | Recorded in the fix commit, dated 2026-10-08. | Security item. | #287, PR #307 (commit b581a71). | Commit b581a71, message body |
| INC-027 | 2026-10-08 | Permissions, keys and secrets | macOS refused a granted interpreter that was started by an ungranted parent, such as the new launcher. | Test on the mini, recorded in the runbook as tested on 2026-10-08. | The grant has to sit on the launcher, not on the interpreter. | #287, PR #307 (commit b581a71). The runbook records the parent rule. | `ops/runbook-lab-account-and-daemons.md`, Step 4, the backup launcher paragraph ("tested 2026-10-08, #287") |

## How to add an incident

Fill in all eight fields. Use the next free INC number and never reuse one.

- **id**: the next INC number, three digits.
- **date found**: YYYY-MM-DD, from the record. Session reports use the UTC start date.
- **category**: one of the seven names in the taxonomy. If none fits, add a category to the taxonomy first.
- **what failed**: one plain sentence about the real failure or defect.
- **how found**: where it was first seen, such as a log, a session step, a run or a read-back. It must be on the machine or in real use.
- **impact**: what was lost, delayed or exposed.
- **fix**: a commit hash, a PR or issue number, or "none" with the reason. Name open items as open.
- **evidence**: a file path and section, or a commit hash. The record must have been read.

Rules: never add a drill fault. For a security weakness, write one neutral line and the issue or PR number only. If the record is unclear, list the item under "Left out, source unclear" instead. Keep the table oldest first and update the counts.

## Count per category

| Category | Count | Incidents |
|---|---|---|
| Install and deploy | 5 | INC-001, INC-002, INC-004, INC-012, INC-018 |
| Permissions, keys and secrets | 7 | INC-010, INC-011, INC-016, INC-017, INC-019, INC-026, INC-027 |
| Data and backup | 2 | INC-014, INC-015 |
| Supervision, scheduling and recovery | 0 | none |
| Model runtime | 4 | INC-003, INC-007, INC-008, INC-009 |
| Measurement and research method | 7 | INC-005, INC-006, INC-013, INC-021, INC-022, INC-023, INC-024 |
| Documentation and runbook | 2 | INC-020, INC-025 |
| **Total** | **27** | |

## Left out, source unclear

- The 2026-10-06 freeze drill, where the watchdog did not replace a supervisor that had not yet written a heartbeat (`docs/reviews/2026-10-06-mac-session.md`, section 7, `ops/drills/log/2026-10-06-supervisor-freeze.md`). The drill injected the fault, so it is not counted. The defect was addressed in commit 4bfa910 (#275). The owner may decide to count it as a watchdog defect.
- All drill records in `ops/drills/log/`, from 2026-09-29 to 2026-10-07. Their faults are injected. This includes the first wrong-model drill, which failed on a 404 from the server.
- Benchmark and evaluation rates, such as 11 of 51 refused tool calls in `docs/decisions/0001-heavy-model.md`, and the pass counts in `docs/PREREGISTRATION.md`. These are results, not single incidents.
- The speed in `docs/decisions/0001-heavy-model.md`, about 67 tokens per second against an estimate of about 100. This was an estimate that was off, not a failure.
- Commit 454517c (#313) and commit 03ec265 (#306). Both are hardening changes, and the record does not show a real occurrence. Security items, neutral line only: backup path and state file checks (#313), and approval display and signing (#306).
- Commit cf72e33 (#309). A watchdog signal change. The record does not show a real occurrence.
- Commit 16a1c24 (#319 and #316). A memory reading rounding change. #316 records that it was found in a test run in a cloud container, not on the machine or in real use.
- Commit d2866ba (#180 and #292). Task ceilings set from measured peaks. No failure is recorded.
- Commit ab51f53 (#188 and #310). An install guide read-through by its author. The record does not say it was on a new Mac, and the fresh-user test (#188) has not been run.
- `SECURITY.md`, Fixed items 1 to 6 (#7, #8, #9, #10, #17 and #61). The record does not show real use.
- A check URL was exposed once (`docs/STATUS.md`, waiting item 5). Security item. The record does not say when.
- The setup-plan key check printed a literal `~operator` until 2026-09-30 (`ops/runbook-lab-account-and-daemons.md`, Step 2). The record does not say how it was found.
- `docs/reviews/2026-09-29-agent-scripts.md`. The validator's findings on an outside skills library. These are not failures of this lab.
