# Release notes for a first release (draft)

**Draft, not published.** On 2026-10-08 this repository has no tag, no GitHub
release and no DOI (`gh release list` and `git ls-remote --tags origin` return
nothing). `pyproject.toml` and `CITATION.cff` both say version 0.1.0. The owner
picks the version and the date; the steps are at the end of this file and in
[ops/release.md](../ops/release.md). Tracked in
[#83](https://github.com/roshanaryal1/home-lab/issues/83).

## What this is

home-lab is a local agent runtime for one Mac. Every tool call the agent makes
goes through a broker; risky calls wait for the owner's signature; everything
is written to a hash-chained audit log. The model proposes, and rules and the
owner decide. It is built and measured on one machine, the project's Apple M6
Mac mini with 32 GB ([README](../README.md)).

## What is in it

Built and tested in CI on Linux ([README](../README.md), Status):

- A durable SQLite task queue and supervisor. After a crash, a task that is not
  marked idempotent waits for a person instead of running again.
- A broker with tiered tools (`lab/broker.py`). Reading and listing run on
  their own; writing and fetching notify; deleting, shell commands, connector
  calls, skill scripts, MCP calls and copying a repository into a workspace
  wait for the owner's Ed25519 signature.
- The Rule of Two per task, a default-deny egress gateway and a secret vault.
  The gateway and the vault are tested only against fake networks and dummy
  credentials ([README](../README.md), Security status).
- Six reviewed handlers: workspace files, read-only git, web fetch with
  summary, `skill.run`, `mcp.call` and `repo.read` (`lab/handlers/__init__.py`).
- Chat from the owner's paired Telegram chat, through the broker
  ([#239](https://github.com/roshanaryal1/home-lab/issues/239)).
- Memory the owner can inspect, correct and delete. The agent can only propose
  a memory; the owner's signature accepts it
  ([#253](https://github.com/roshanaryal1/home-lab/issues/253)).
- Skills stored as versioned candidates that become active only on the owner's
  signed promotion ([#254](https://github.com/roshanaryal1/home-lab/issues/254)).
  Their scripts run only in a disposable Apple container with no network
  ([#181](https://github.com/roshanaryal1/home-lab/issues/181),
  [#255](https://github.com/roshanaryal1/home-lab/issues/255)).
- MCP servers called through the broker from an owner-signed snapshot, under
  Seatbelt ([#256](https://github.com/roshanaryal1/home-lab/issues/256)).
- Operations: launchd daemons, a watchdog, a backup that restore-checks itself
  and rotates, a nightly self-test, alerts, a dead-man switch ping and logged
  recovery drills.
- Evaluation tools: a fixed task set with sealed provenance records
  (`lab eval`), benchmarks (`lab bench`), the shadow comparison (`lab shadow`)
  and runners for the pre-registered claims (`lab prereg`).
- An install guide for another Mac, [INSTALL.md](INSTALL.md).

## What has run on the project's Mac mini

Since 2026-09-30 the lab runs there as a non-admin `lab` account under launchd.
Each line below has a committed record:

- Approvals made without the owner's key were refused, 3 of 3
  ([2026-10-06 session](reviews/2026-10-06-mac-session.md)).
- The watchdog replaced a frozen supervisor in 96 s and 97 s
  ([pass 1](reviews/2026-10-07-mac-session-drills-pass-1.md),
  [pass 2](reviews/2026-10-07-mac-session-drills-pass-2.md)).
- A backup was written to the backup disk and restore-checked
  ([report](reviews/2026-10-07-mac-session-backup.md)).
- A test alert reached the owner's phone
  ([report](reviews/2026-10-07-mac-session-alert.md)).
- The chat bot was installed and answered from the phone
  ([record](reviews/2026-10-07-chat-bot-install.md)).
- The M5 claim and the H1 and H1b runs below.

## What the experiments found

### Safety claims for the roadmap milestones

Pre-registered in [PREREGISTRATION-SAFETY.md](PREREGISTRATION-SAFETY.md) before
the code each one measures, with each case file frozen by SHA-256. They are not
part of the OSF registration. The target for each is zero failures.

| Claim | What counts as a failure | Cases | Failures | How it ran |
|---|---|---|---|---|
| M2, chat | an injected chat message reaches an approve-tier tool without the owner's signature | 32 | 0 | 2026-10-01, a scripted worst-case model and a fake Telegram server behind the real egress gateway |
| M6, skills | a malicious skill becomes active without a signed promotion | 36 | 0 | 2026-10-01 |
| M5, container | a hostile script reaches the network or the host's files, or a process outlives its container | 30 | 0 | 2026-10-07, the real Apple container on the Mac mini |

The limits are stated with each result. The case sets are fixed and written by
one author. By the rule of three, zero failures on 30 to 36 cases bounds the
failure rate near 8 to 10 percent at 95 percent confidence on cases like these,
not against every input. M2 used a scripted model, not the real one. M6 did not
run the skills' scripts. In M5, a connection to a public address could not be
observed from outside, so those cases rest on the guest having no network
interface.

### Model hypotheses registered on OSF

The evaluation plan was registered at [osf.io/jfp74](https://osf.io/jfp74/) on
2026-09-29 14:45 UTC. The results are in [PREREGISTRATION.md](PREREGISTRATION.md),
Results.

- **H2, not supported.** A JSON grammar on `llama-server` did not lower the
  refused-call rate: 0 of 70 with or without it, a floor effect.
- **H2b, supported by the decision rule.** On `mlx_lm.server`, the schema in
  the prompt refused 6 of 70 against 9, and passed 63 against 28. The refusal
  difference is not distinguishable from zero.
- **H3, not supported for the setting tested.** Prompt cache 1 against 4: no
  gain reached 10 percent, so it was not adopted.
- **H4, supported (replication).** With the real model driving, 0 of 9
  injection attacks succeeded.
- **H1, not supported.** The case file and labels were frozen by amendment 2,
  registered at [osf.io/q75bx](https://osf.io/q75bx/) (DOI
  10.17605/OSF.IO/Q75BX) on 2026-10-07 08:12 UTC. On the 30 cases the typed
  candidate model's accuracy was 0.100 against the rubric's 0.833, with 5
  false promotions; accuracy gain -0.733, 95% CI [-0.867, -0.567]. The labels are
  the majority of three AI reviewers, partly checked by the owner (25 of 30
  agree). Limit: the registered candidate prompt names the five routes but does
  not define them. The candidate is not adopted, and `lab shadow` stays advice
  only (PR #301).

### Exploratory, outside the registered results

- **H1b** ([#302](https://github.com/roshanaryal1/home-lab/issues/302)): the
  same 30 cases with the five route definitions added to the prompt. Accuracy
  0.433, coverage 1.00, 8 false promotions. One run on cases whose labels were
  already known; H1's verdict is unchanged
  ([PREREGISTRATION-AMENDMENT-2-REGISTRATION.md](PREREGISTRATION-AMENDMENT-2-REGISTRATION.md),
  "H1b, exploratory").
- **H1c** ([#179](https://github.com/roshanaryal1/home-lab/issues/179)): not
  run. The server the candidate runs on, `mlx_lm.server` 0.31.3, ignores
  `response_format`, so constraining the output would only have repeated H1b
  ([evidence](https://github.com/roshanaryal1/home-lab/issues/179#issuecomment-6042730490)).
  On 2026-10-09 the owner chose to build constrained decoding into our own
  loopback server on the same weights (#179, option 2). It is built in #320 and
  not yet run.

## What is not promised

- No independent security review has been done
  ([ROADMAP.md](ROADMAP.md), Risks).
- Do not connect real credentials. The README's Security status names three
  conditions first, and they do not all hold: code the `lab` account runs can
  still reach the queue database
  ([#70](https://github.com/roshanaryal1/home-lab/issues/70)).
- One machine. Only the 32 GB tier is measured ([INSTALL.md](INSTALL.md)), and
  the install guide has not yet been proved by a fresh-user test on another Mac
  ([#188](https://github.com/roshanaryal1/home-lab/issues/188)).
- The handlers have not yet done real work through the deployed lab, and the
  `skillrun` and `mcp` checks of the session script have not been run on the
  Mac mini ([ROADMAP.md](ROADMAP.md), M3 and M6). Handler task ceilings are
  256 MB and 30 s, set from peaks measured on sample tasks
  ([#180](https://github.com/roshanaryal1/home-lab/issues/180)).
- Machine checks still open: #67, #70, #77, #79, #91, #235 and #287
  ([ROADMAP.md](ROADMAP.md), M0).
- The safety numbers hold on fixed case sets, with the bounds above. They say
  nothing about inputs unlike those cases, or about a kernel or hypervisor flaw.
- The local model is a weak router (H1, H1b). Its routing output is advice
  only.
- Not built: multi-user use
  ([#41](https://github.com/roshanaryal1/home-lab/issues/41)), a skill
  marketplace, calendar and mail tools, code-editing executors and the model
  swap manager ([README](../README.md), Status). Publishing has never been used
  with a real destination.
- The MIT license gives no warranty ([LICENSE](../LICENSE)).

## To publish (the owner's steps)

Nothing below has been done. See also [ops/release.md](../ops/release.md).

1. Choose the version. Set `version` in `pyproject.toml` and `CITATION.cff`
   together (`tests/test_citation.py` fails if they differ), and set
   `date-released` in `CITATION.cff` to the release date. It says 2026-09-28
   today, which is before any release.
2. `ops/release.md` also asks for a fresh `lab eval run` record on the Mac
   mini, committed before the release.
3. Validate the citation file. The current file passed with `cffconvert`
   2.0.0 on 2026-10-08 ("valid according to schema version 1.2.0"):

   ```sh
   uvx cffconvert --validate -i CITATION.cff
   ```

4. At Zenodo, with the owner's GitHub account linked to it (Zenodo names that
   as the prerequisite): profile menu, **GitHub**, **Sync now**, then switch on
   `roshanaryal1/home-lab`. Zenodo's page says that once connected, "new
   releases from the repository will be automatically ingested and archived"
   ([Zenodo: enable a repository](https://help.zenodo.org/docs/github/enable-repository/)).
   Zenodo reads a subset of `CITATION.cff`, and would ignore it if a
   `.zenodo.json` existed; there is none
   ([Zenodo: CITATION.cff](https://help.zenodo.org/docs/github/describe-software/citation-file/)).
5. Turn this draft into the release text, tag the commit and publish a GitHub
   release, then wait for Zenodo to process it
   ([Zenodo: archive a release](https://help.zenodo.org/docs/github/archive-software/github-upload/)).
6. In the next commit, add the DOI Zenodo shows as `doi:` in `CITATION.cff` and
   as a badge in the README, then delete this draft or replace it with the
   published notes.
