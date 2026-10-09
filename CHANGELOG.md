# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/). The versioning
rules are in [ops/release.md](ops/release.md#versioning-and-compatibility-draft).
They are a draft until the owner approves them.

## [Unreleased]

### Added

- `GET /metrics` on the read-only status page: the queue, health, leases, task
  outcomes and counters in Prometheus text format, on loopback only, with fixed
  names and no task ids or titles. A failure that is retried stays counted as an
  outcome ([#380](https://github.com/roshanaryal1/home-lab/issues/380))
- Every chat message to the person ends with the label `[home-lab AI agent]`, added
  at the one send, so no reply text can remove it
  ([#375](https://github.com/roshanaryal1/home-lab/issues/375)).
- `lab status` flags a task that writes no event for 30 minutes while its lease
  keeps renewing. The task is listed in the text output, the `--json` output and
  the status page, and the status check alerts with its id. The task is only
  flagged, never cancelled or requeued. `--stalled-minutes` sets the limit
  ([#376](https://github.com/roshanaryal1/home-lab/issues/376)).
- `lab security-audit`: one read-only check of the lab's own boundary (file owners
  and modes, the operator key, the loopback model URL), with `--details` to list
  the offending paths ([#362](https://github.com/roshanaryal1/home-lab/issues/362)).
- `lab doctor`: one read-only health check of an install, with how to fix each
  failure ([#347](https://github.com/roshanaryal1/home-lab/issues/347)).
- A `lab` command with `--version`, and a built wheel that holds the whole
  package and is installed and imported in CI
  ([#329](https://github.com/roshanaryal1/home-lab/issues/329),
  [#330](https://github.com/roshanaryal1/home-lab/issues/330)).
- Before a migration, the database is copied beside itself as
  `<name>.pre-vN.bak`. Every upgrade step is tested against a fresh schema, and
  the shipped migrations are pinned by checksum
  ([#348](https://github.com/roshanaryal1/home-lab/issues/348)).
- Log records keep their traceback, `LAB_LOG_LEVEL` sets the level, and the
  scheduled jobs' launchd logs rotate
  ([#349](https://github.com/roshanaryal1/home-lab/issues/349)).
- For the P3 study: a weekly eval job that shares the model slot, and
  `lab memory-budget` ([#321](https://github.com/roshanaryal1/home-lab/issues/321)).
- `lab migrate --check` tries the new code's migrations on a private copy of
  the database before an update restarts anything, and the update steps stop
  the supervisor and `lab tick` first and say how to roll back
  ([#356](https://github.com/roshanaryal1/home-lab/issues/356)).
- `lab update --plan COMMIT` prints the update steps for this install, with the
  commit, the deployed commit and the installed jobs filled in. It is read-only
  and runs no sudo ([#370](https://github.com/roshanaryal1/home-lab/issues/370)).
- `lab injection-suite`: a versioned public injection suite, 27 fixed cases of
  hidden instructions in a fetched page, run in throwaway labs through the real
  broker and graded on state. The case file is pinned by SHA-256
  ([#368](https://github.com/roshanaryal1/home-lab/issues/368)).
- `lab schedule add|list|remove`: owner-signed schedules that start a task on
  a daily, weekly or every-N-minutes rule. A schedule can start work but never
  approve it. An unsigned or altered schedule is refused and logged
  ([#361](https://github.com/roshanaryal1/home-lab/issues/361)).

### Fixed

- A known database or file error in the CLI prints one line, not a traceback.
  `--debug` keeps the traceback
  ([#331](https://github.com/roshanaryal1/home-lab/issues/331)).
- The deploy steps keep the test tools installed, so the nightly self-test can
  run its safety tests, and the self-test says why when pytest cannot start
  ([#270](https://github.com/roshanaryal1/home-lab/issues/270)).
- A backup now holds every file the database refers to: task artifacts,
  evidence snapshots and the files of each skill version. Before, it held only
  task artifacts, and its restore check passed without the others
  ([#358](https://github.com/roshanaryal1/home-lab/issues/358)).

## [0.1.0] - unreleased

The date is set when the owner tags this release. The notes below are taken
from [docs/RELEASE-NOTES-DRAFT.md](docs/RELEASE-NOTES-DRAFT.md), which also
states the limits of each claim.

### Added

- A durable SQLite task queue and supervisor. After a crash, a task that is
  not marked idempotent waits for a person instead of running again.
- A broker with tiered tools (`lab/broker.py`). Reading and listing run on
  their own. Writing and fetching notify. Deleting, shell commands, connector
  calls, skill scripts, MCP calls and copying a repository into a workspace
  wait for the owner's Ed25519 signature.
- The Rule of Two per task, a default-deny egress gateway and a secret vault.
  The gateway and the vault are tested only against fake networks and dummy
  credentials.
- Six reviewed handlers: workspace files, read-only git, web fetch with
  summary, `skill.run`, `mcp.call` and `repo.read` (`lab/handlers/__init__.py`).
- Chat from the owner's paired Telegram chat, through the broker
  ([#239](https://github.com/roshanaryal1/home-lab/issues/239)).
- Memory the owner can inspect, correct and delete. The agent can only propose
  a memory. The owner's signature accepts it
  ([#253](https://github.com/roshanaryal1/home-lab/issues/253)).
- Skills stored as versioned candidates that become active only on the owner's
  signed promotion
  ([#254](https://github.com/roshanaryal1/home-lab/issues/254)). Their scripts
  run only in a disposable Apple container with no network
  ([#181](https://github.com/roshanaryal1/home-lab/issues/181),
  [#255](https://github.com/roshanaryal1/home-lab/issues/255)).
- MCP servers called through the broker from an owner-signed snapshot, under
  Seatbelt ([#256](https://github.com/roshanaryal1/home-lab/issues/256)).
- Operations: launchd daemons, a watchdog, a backup that restore-checks itself
  and rotates, a nightly self-test, alerts, a dead-man switch ping and logged
  recovery drills.
- Evaluation tools: a fixed task set with sealed provenance records (`lab eval`),
  benchmarks (`lab bench`), the shadow comparison (`lab shadow`) and runners for
  the pre-registered claims (`lab prereg`).
- An install guide for another Mac, [INSTALL.md](docs/INSTALL.md).

### Security

- Approvals made without the owner's key were refused, 3 of 3, in the
  2026-10-06 session ([record](docs/reviews/2026-10-06-mac-session.md)).
- Safety claims M2 (chat, 32 cases), M6 (skills, 36 cases) and M5 (container,
  30 cases) had zero failures on fixed case sets. The limits of each run are in
  the draft notes.
- With the real model driving, 0 of 9 prompt injection attacks succeeded in the
  registered replication (H4).
- No independent security review has been done.
- Do not connect real credentials. The README's Security status section explains
  why.
