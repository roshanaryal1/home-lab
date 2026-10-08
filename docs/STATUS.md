# Status and handoff

Last updated 2026-10-09 (NZDT). Read this first when picking the work up in a new session,
on any account or machine. It says what is done, what is waiting and on whom, and what must not
be redone. The detail lives in the documents it links; this page only points.

## Where things stand

- `main` has everything merged. Open pull requests: **#312 only** (keep-awake as the `lab`
  account, [#235](https://github.com/roshanaryal1/home-lab/issues/235)). Do **not** merge it until
  the owner has run `./ops/mac-session.sh --only caffeinate` from that branch and it passed; the
  steps are in its runbook section "Moving keep-awake to the lab account".
- #312 must also be up to date with `main` before it merges. It was brought up to date on
  2026-10-09 with a merge commit, and every later merge to `main` puts it behind again. Merge
  `main` into it (a merge commit, never a rebase or force-push) and wait for green checks. This
  changes what the caffeinate check runs only if `main` changed `ops/mac-session.sh`.
- Merged on 2026-10-08 (NZDT): #300 H1 run record, #301 H1 result, #303 `lab shadow
  --system-file`, #304 H1b run, #305 drills (interrupted task, restore from a backup), #306
  approvals bound to the intent the operator sees, #307 backup launcher that holds Full Disk
  Access, #308 H1c not run, #309 watchdog checks it is signalling the supervisor, #310 install
  guide fixes, #311 roadmap and Mac-work status, #313 backup file handling checks, #314 this page.
  Of these, #303, #305, #306, #307, #309 and #313 change code the Mac mini runs.
- Nothing merged since 2026-10-07 is deployed on the Mac mini yet. Deploying is the owner's step.

## Research results (do not redo)

- **H1: not supported** (registered, run once, [docs/PREREGISTRATION.md](PREREGISTRATION.md)
  Results). The registration allows one accuracy run per configuration, so it is not rerun.
- **H1b** (exploratory, route definitions in the prompt): 0.433 against the rubric's 0.833, 8
  false promotions. Reported beside H1, never replaces it
  ([registration note](PREREGISTRATION-AMENDMENT-2-REGISTRATION.md)).
- **H1c** (constrained decoding, #179): **not run**. The served `mlx_lm.server` 0.31.3 ignores
  `response_format`. #179 lists three options; the owner has not chosen.
- M2, M5 and M6 safety claims: 0 failures ([docs/PREREGISTRATION-SAFETY.md](PREREGISTRATION-SAFETY.md)).

## Waiting on the owner (needs sudo, hardware or a decision)

In this order:

1. Read last night's job logs: `sudo tail -n 20 /var/log/homelab/backup.log /var/log/homelab/selftest.log`.
   If the self-test passed, close [#270](https://github.com/roshanaryal1/home-lab/issues/270); comment the
   backup result on [#67](https://github.com/roshanaryal1/home-lab/issues/67).
2. Redeploy `main`: runbook section "Updating the deployed code"
   ([ops/runbook-lab-account-and-daemons.md](../ops/runbook-lab-account-and-daemons.md)).
3. Move the backup's Full Disk Access to its launcher: runbook section "Moving the backup's Full
   Disk Access to its launcher" ([#287](https://github.com/roshanaryal1/home-lab/issues/287)). It ends
   by turning Terminal's Full Disk Access off.
4. The #312 check above, then merge and follow its runbook section.
5. Replace the outside heartbeat check. The heartbeat job and its outside check were set up on
   2026-10-07 and the job pinged it, but the check's URL was exposed once, so make a new check and
   rerun the heartbeat URL step. The Telegram alert hook is installed and a test alert reached the
   phone. Also turn off browser Apple Events and Accessibility permissions used for the OSF work.
6. Drills: interrupted task with a power pull (`lab drill interrupted`, [#91](https://github.com/roshanaryal1/home-lab/issues/91), [#77](https://github.com/roshanaryal1/home-lab/issues/77)),
   network unplug ([#79](https://github.com/roshanaryal1/home-lab/issues/79)), `/stop` from the chat bot,
   monthly restore drill (setup section 10).
7. Fresh-account install test on another Mac ([#188](https://github.com/roshanaryal1/home-lab/issues/188),
   [ops/install-validation.md](../ops/install-validation.md)).

Decisions:

- [#70](https://github.com/roshanaryal1/home-lab/issues/70): which account owns the database and which
  runs workers, so a worker cannot open it.
- [#179](https://github.com/roshanaryal1/home-lab/issues/179): which constrained-decoding option, if any.
- [#189](https://github.com/roshanaryal1/home-lab/issues/189): product direction (an options memo is on
  the issue).
- [#83](https://github.com/roshanaryal1/home-lab/issues/83): when to publish the first release; steps
  are on the issue and in [RELEASE-NOTES-DRAFT.md](RELEASE-NOTES-DRAFT.md).
- The owner keeps private security review notes on the Mac mini, outside this repository. Ask the
  owner before acting on anything security-related that is not in an issue.

## What a new session can do without the owner

Little: most open work needs the Mac mini with sudo, or a decision. Safe without the owner:
answer review comments on #312, keep docs in step with merged work, and prepare (not publish) work
for the decisions above once the owner has chosen. Never deploy, use sudo, publish a release or
send alerts without being asked.

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
