# Status and handoff

Last updated 2026-10-09 (NZDT), late evening. Read this first when picking the work up in a new session,
on any account or machine. It says what is done, what is waiting and on whom, and what must not
be redone. The detail lives in the documents it links; this page only points.

## Where things stand

- The plan: v1.0 and a public launch by April 2027, month by month, in
  [PLAN-7-MONTHS.md](PLAN-7-MONTHS.md) (tracking issues #338 to #344). The features to add
  through the broker are in [FEATURE-PLAN.md](FEATURE-PLAN.md), and how home-lab compares with
  OpenClaw and Hermes Agent is in [COMPARISON.md](COMPARISON.md). The wider market and the
  owner's hybrid decision of 2026-10-09 (local by default, opt-in cloud; scheduled tasks, a safe
  browser, an easy install and injection defence first) are in [MARKET-2026.md](MARKET-2026.md).
- Open pull requests that wait for the owner: **#373** (browser design, #369), **#379** (hybrid
  design, #377) and **#388** (installer design, #385) each end with questions only the owner can
  answer. Do not merge them until the owner has answered. **#312** (keep-awake as the `lab`
  account, [#235](https://github.com/roshanaryal1/home-lab/issues/235)): do **not** merge #312 until both checks in step 1 of its runbook section "Moving keep-awake to
  the lab account" have passed on the Mac mini: `./ops/mac-session.sh --only caffeinate` from
  that branch, and the same check from launchd with a throwaway daemon.
- #312 must also be up to date with `main` before it merges. It was last brought up to date on
  2026-10-09 with a merge commit, and every later merge to `main` puts it behind again. Merge
  `main` into it (a merge commit, never a rebase or force-push) and wait for green checks. If
  that merge changed `ops/mac-session.sh`, run both checks again on the updated branch before
  merging: a pass from before the merge does not cover the new script. No merge so far has
  changed that script since the checks were written.
- Merged on 2026-10-08 (NZDT): #300 to #311, #313, #314 (see the git log). Of these, #303, #305,
  #306, #307, #309 and #313 change code meant for the Mac mini.
- Merged on 2026-10-09 (NZDT), changes to code meant for the Mac mini (none deployed yet, see below):
  #319 (a memory reading at the ceiling is reported over it), #320 (constrained decoding for H1c),
  #324 (`lab prereg --record`), #334 (the wheel and the `lab` command, #329, #330), #336 (one-line
  CLI errors, #331), #345 (deploy keeps pytest, #270), #346 (`lab memory-budget`), #351
  (`lab doctor`, #347), #353 (a snapshot before migrating, #348), #335 (the weekly eval job),
  #354 (log tracebacks, level and rotation, #349), #357 (`lab migrate --check` and safer
  update steps, #356) and #360 (backups hold every blob the database refers to, #358).
- Merged later on 2026-10-09 (NZDT), the first features of the hybrid plan, also for the Mac mini
  and not deployed: #366 (`lab security-audit`, #362), #367 (owner-signed schedules, #361),
  #371 (the public injection suite, #368), #372 (`lab update --plan`, #370), #378 (`lab status`
  flags a task whose lease renews with no new event, #376), #382 (every chat message carries an
  AI label, #375), #383 (`/metrics` on the status page, #380), #384 (`lab export`, #381), #389
  (read-only database opens encode the path, #386) and #391 (a today page on the status page,
  #390). Documents: #365 (the 2026 market, the
  hybrid position and the reordered plan, #364) and #374 (the project website in `site/`, #363).
- Merged on 2026-10-09 (NZDT), documents and research only:
  #317, #322, #323 (H1b interval), #325 (incident catalog), #327 (comparison and feature plan),
  #333 (CHANGELOG and draft versioning rules, #332), #337 (A/B and H2 drafts), #350, #352
  (seven-month plan), and the pull request for #355 that updates this page and the other
  overview documents.
- Nothing merged since 2026-10-07 is deployed on the Mac mini yet. Not verified from a session:
  the deployed commit is visible only on the mini, where the first block of "Updating the
  deployed code" prints it (`deployed now`). Deploying is the owner's step.

## Research results (do not redo)

- **H1: not supported** (registered, run once, [docs/PREREGISTRATION.md](PREREGISTRATION.md)
  Results). The registration allows one accuracy run per configuration, so it is not rerun.
- **H1b** (exploratory, route definitions in the prompt): 0.433 against the rubric's 0.833, 8
  false promotions, interval in #323. Reported beside H1, never replaces it
  ([registration note](PREREGISTRATION-AMENDMENT-2-REGISTRATION.md)).
- **H1c** (constrained decoding, #179): **not run**. The served `mlx_lm.server` 0.31.3 ignores
  `response_format`. On 2026-10-09 the owner chose option 2: our own constrained decoding on the
  same MLX weights, built in #320. The run follows [H1C-RUN-PLAN.md](H1C-RUN-PLAN.md) and is the
  owner's, on the Mac mini.
- M2, M5 and M6 safety claims: 0 failures ([docs/PREREGISTRATION-SAFETY.md](PREREGISTRATION-SAFETY.md)).
  M2 and M6 have sealed records (#324).
- P3 (the operations paper): the incident catalog holds 27 real incidents ([INCIDENTS.md](INCIDENTS.md)).
  The weekly eval job, the memory budget check and drafts for four A/B tests and an exploratory
  H2 rerun are merged ([P3-AB-DESIGN-DRAFT.md](P3-AB-DESIGN-DRAFT.md),
  [H2-RERUN-PLAN-DRAFT.md](H2-RERUN-PLAN-DRAFT.md)). Nothing in the drafts runs until the owner
  approves it. What is left for every hypothesis is on #321.

## Waiting on the owner (needs sudo, hardware or a decision)

In this order:

1. Read last night's job logs: `sudo tail -n 20 /var/log/homelab/backup.log /var/log/homelab/selftest.log`.
   Comment the backup result on [#67](https://github.com/roshanaryal1/home-lab/issues/67).
2. Redeploy `main`: runbook section "Updating the deployed code"
   ([ops/runbook-lab-account-and-daemons.md](../ops/runbook-lab-account-and-daemons.md)).
   `lab update --plan COMMIT`, run as yourself, prints that section's steps with the deployed
   commit, the new commit and the installed jobs filled in. It runs no sudo. The update
   takes a backup before anything changes, stops the supervisor and `lab tick` before the
   checkout, tries the new migrations on a copy (`migrate --check`, #356) before anything starts
   again, and its `uv sync` keeps the test tools (`--extra dev`) the nightly self-test needs.
   After the next 03:17 self-test passes, close
   [#270](https://github.com/roshanaryal1/home-lab/issues/270).
3. Run `lab doctor` as `lab` with the service's `LAB_` values (INSTALL.md, first run). Every line
   should say `ok`. Then run `lab security-audit` the same way, and again as yourself. Each
   account can read different files, so a line may say `skip`. No line should say `FAIL`. Use
   `--details` only in your own Terminal: it names the paths that failed.
4. Run the injection suite against the served model:
   `uv run lab injection-suite --endpoint http://127.0.0.1:8080/v1 --model NAME --revision HASH --weights-mb N`
   ([INJECTION-SUITE.md](INJECTION-SUITE.md)). Add the result to that page's table. If any case
   succeeds, do not publish it: raise it privately first.
5. Install the weekly eval job and fill its four `PASTE_` values (same runbook section), so P3's
   six weekly runs start. Install the log rotation rules and dry-run them (runbook, "Log
   rotation for the launchd logs").
6. Move the backup's Full Disk Access to its launcher: runbook section "Moving the backup's Full
   Disk Access to its launcher" ([#287](https://github.com/roshanaryal1/home-lab/issues/287)). It ends
   by turning Terminal's Full Disk Access off.
7. The two #312 checks above, then merge and follow its runbook section.
8. Replace the outside heartbeat check. The heartbeat job and its outside check were set up on
   2026-10-07 and the job pinged it. Make a new check and rerun the heartbeat URL step; the reason
   is in the owner's private notes. Also turn off browser Apple Events and Accessibility
   permissions used for the OSF work.
9. Run H1c on the Mac mini ([H1C-RUN-PLAN.md](H1C-RUN-PLAN.md)).
10. Drills: interrupted task with a power pull (`lab drill interrupted`, [#91](https://github.com/roshanaryal1/home-lab/issues/91), [#77](https://github.com/roshanaryal1/home-lab/issues/77)),
    network unplug ([#79](https://github.com/roshanaryal1/home-lab/issues/79)), `/stop` from the chat bot,
    monthly restore drill (setup section 10).
11. Fresh-account install test on another Mac ([#188](https://github.com/roshanaryal1/home-lab/issues/188),
    [ops/install-validation.md](../ops/install-validation.md)).
12. Delete the branch `research/321-memory-budget`. It was replaced by
    `research/321-memory-budget-check` and only exists because the session's git access cannot
    delete a remote branch. `.gitleaksignore` names one of its commits; that line can go once the
    branch is deleted.
13. Switch the website on when you want it public: [WEBSITE.md](WEBSITE.md) (GitHub Pages from
    Actions, and the repository variable `PAGES_ENABLED` set to `true`), then run the pages
    workflow once by hand. Until then the Pages workflow does nothing.

Decisions:

- [#70](https://github.com/roshanaryal1/home-lab/issues/70): which account owns the database and which
  runs workers, so a worker cannot open it.
- [#83](https://github.com/roshanaryal1/home-lab/issues/83): when to publish the first release; steps
  are on the issue, in [RELEASE-NOTES-DRAFT.md](RELEASE-NOTES-DRAFT.md) and in
  [ops/release.md](../ops/release.md).
- The versioning and compatibility rules in `ops/release.md` are a draft for the owner to approve
  (#332).
- An empty `LAB_LOG_LEVEL` stops the supervisor, like any value that is not a level. Treating
  empty as unset is the other choice (#349).
- The A/B design draft leaves margins and the sampler interval to the owner, and needs approval
  before any run (#321).
- Who labels the H1 cases a second time, blind, for the agreement table (#321).
- [#318](https://github.com/roshanaryal1/home-lab/issues/318): a point in the owner's private notes;
  record the outcome on the issue in general terms.
- #373, #379 and #388: the questions at the end of the browser, hybrid and installer designs.
  Each design picks a default, and the build waits for the answers.
- Register the draft claim S1 ([SCHEDULES.md](SCHEDULES.md)) before a release ships schedules.
- `lab update --plan` covers the standard install only. The issue asked for `--deploy` and
  `--launch-daemons`. They were left out because the runbook's commands name four fixed paths
  (reason on #370).
- The owner keeps private security review notes on the Mac mini, outside this repository. Ask the
  owner before acting on anything security-related that is not in an issue.

Changed behaviour to know before the redeploy:

- A migration first copies the database beside itself as `lab.db.pre-vN.bak`. If that copy cannot
  be written (no space, or the folder is not writable), nothing is migrated and the command stops
  with a one-line error (#348).
- The supervisor's `supervisor.err` now gets only warnings and errors. Its full log is still the
  rotating JSON file `supervisor.log` (#349).
- `lab eval run --db` waits for the model slot the supervisor and `lab tick` share, and stops with
  nothing recorded if the slot stays busy for 600 seconds (#321).
- The update steps stop the supervisor for the whole update, not only for a restart at the end,
  so do the update when the queue is idle (#356).
- Migration 17 adds the schedule tables, with an append-only list of removed schedules (#361).
  `migrate --check` should name schema version 17.
- Every chat message the lab sends ends with the label `[home-lab AI agent]` (#375).
- `lab status` reports ATTENTION, and the status check alerts with the kind `stalled`, for a task
  whose lease keeps renewing with no new event for 30 minutes. Its exit code is unchanged (#376).
- The status page also serves `/metrics` in Prometheus text format, and `/today` with the local day's
  work, both on loopback only (#380, #390).

## What a new session can do without the owner

Work on the next month's issues in [PLAN-7-MONTHS.md](PLAN-7-MONTHS.md) that need no machine:
code, tests and docs, through pull requests. Answer review comments on #312, and keep docs in
step with merged work. Never deploy, use sudo, publish a release or send alerts without being
asked.

## Rules of this repository

- Every task starts from a GitHub issue; close an issue only with evidence that its criteria are met.
- No AI-tool attribution anywhere: commits, PRs, issues, docs.
- No em dash character anywhere.
- Never edit the files hash-checked by `tests/test_h1_amendment.py`, or anything under
  `evals/h1_review/` except new, separately named records.
- Verify outside facts (versions, citations, tool behaviour) before writing them; mark what is not
  verified.
- Before every commit: `uv run ruff check lab tests`, `uv run mypy lab`, `uv run pytest -q`, and
  `gitleaks detect --redact --no-banner --log-opts="main..HEAD"` before pushing. `main` needs
  green checks, an up-to-date branch and resolved review threads to merge.
- Never print or commit a secret, URL with a token, or chat id.
