# home-lab

An AI operating system for an Apple M6 Mac mini (32 GB unified, 512 GB
internal + 1 TB external SSD). Not a 24/7 worker pool that runs whatever
it's told: the target is a system that notices work, decides what it's
worth, and does it safely without waiting to be asked. See
[ADR 0004](docs/decisions/0004-operating-system.md) for the framing and
what's actually missing to get there, or
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for a current-state diagram.

This is the **build**. The **design** came first, from a controlled
study: one frozen prompt was given to eleven frontier LLM systems,
each answer was captured verbatim, and the points of agreement and
disagreement were adjudicated into a reference architecture. That study
is [roshanaryal1/llm-architects](https://github.com/roshanaryal1/llm-architects);
the spec this repo implements is its
`analysis/consensus/reference-architecture.md`.

So the unusual thing here is that the architecture is not one person's
opinion, and it is not one model's opinion either. Where the systems
agreed unanimously, that is recorded as consensus. Where they
disagreed, the disagreement is written down as an open decision rather
than quietly resolved.

## Status

| Build step | State |
|---|---|
| 1. SQLite schema: tasks, leases, events, agents, approvals | done |
| 2. Supervisor event loop and durable recovery | done, with known gaps listed in `SECURITY.md` |
| 3. Model adapter, one heavy + one light model | adapter built and tested against a mock ([#74](https://github.com/roshanaryal1/home-lab/issues/74)); the real model needs the M6 |
| 4. Concurrency semaphores (heavy=1, light=2-3) | done, in the supervisor |
| 5. Model swap manager with RAM/headroom policy | needs the M6 |
| 6. Aider/OpenHands executor adapters | not started |
| 7. Research evidence ledger and verification pipeline | ledger built: claim status is separate from task status, every claim opens its exact source ([#90](https://github.com/roshanaryal1/home-lab/issues/90)); the automated verification pipeline is not |
| 8. Dedicated-user permissions and task workspaces | code done (operator-signed approvals); account setup is a checklist for the mini, [#70](https://github.com/roshanaryal1/home-lab/issues/70) |
| 9. launchd + watchdog + queue-aware caffeinate | heartbeat, watchdog and plists built and tested; install and freeze test are on the M6 checklist, [#78](https://github.com/roshanaryal1/home-lab/issues/78) |
| 10. Tailscale-only FastAPI dashboard and emergency stop | `lab status`, `lab control` (pause, drain, stop) and `lab cancel` exist; dashboard, alerts and the dead-man switch are parked, [#79](https://github.com/roshanaryal1/home-lab/issues/79) |
| 11. sqlite-vec / FTS retrieval | FTS5 baseline built with inspect, correct, revoke and delete ([#85](https://github.com/roshanaryal1/home-lab/issues/85)); embeddings must beat it on a measured task first |
| 12. Benchmark and tune before adding anything else | not started, needs the M6 |

Steps 1, 2 and 4 are machine-independent and run anywhere. Everything
touching model residency needs the 32 GB machine to mean anything.

The table above is the execution substrate: safe to leave running
unattended, but it has no opinion about what work should exist. Three
planes on top of it are what make this an operating system rather than
a task runner:

| Plane | Issue | State |
|---|---|---|
| Observation: notice work from the event stream, emit proposals | [#32](https://github.com/roshanaryal1/home-lab/issues/32) | this repo's closed issues and merged PRs become proposals; the event log yields a proposal when tasks of one kind keep failing alike |
| Router: post / blog / paper by evidence weight | [#33](https://github.com/roshanaryal1/home-lab/issues/33) | rubric built over the evidence ledger: deterministic rules, refuses thin evidence upward, inspectable chain; still needs a human to review every route |
| Publish: post/email on your behalf | [#86](https://github.com/roshanaryal1/home-lab/issues/86) | reviewed publishing built and tested against a dummy provider: approval bound to destination and draft hash, write-ahead receipts, idempotency keys, reconciliation of lost responses; no real destination has been used |

Build order and reasoning are in ADR 0004; the rules that limit what an
agent may hold, and the staged rollout, are in ADR 0006.

## The two rules that shape the code

**One heavy inference slot.** All ten non-anchor systems in the study
agreed: a hundred logical agents does not mean a hundred resident
models. On 32 GB you get one heavy model, with light work alongside it.
The supervisor enforces this with a semaphore rather than trusting
convention.

**Never replay a destructive task after a crash.** A task is marked
idempotent or it is not. On restart, stranded tasks become
`interrupted`; idempotent ones requeue automatically, everything else
waits for a human. This is the difference between a crash costing you
five minutes and a crash sending the same email twice.

## Security status

Run the lab only with dummy data, review every draft by hand and connect
no real credentials until three things are true, because none of them can
be finished without the Mac mini: a separate non-admin lab account that
cannot read the operator's approval key ([#70](https://github.com/roshanaryal1/home-lab/issues/70)),
a per-task memory ceiling ([#16](https://github.com/roshanaryal1/home-lab/issues/16)),
and the Keychain path exercised on the mini. The egress gateway and the
secret broker now exist and are tested, but only against fake networks and
dummy credentials. [SECURITY.md](SECURITY.md) has the full list of what is
and is not enforced, and [THREATS.md](THREATS.md) maps it to the OWASP
agentic top 10 with the test or open issue behind every row.

Ideas taken from outside projects do not change that. They are reviewed
against these gaps first; see the
[agent-scripts review](docs/reviews/2026-09-29-agent-scripts.md).

## Skill library checks

`lab skills` is a read-only check on a directory of skills. It never runs,
imports or writes anything it scans.

```sh
uv run python -m lab.cli skills validate --root path/to/skills   # exit 1 on any problem
uv run python -m lab.cli skills inventory --root path/to/skills [--json]
```

`validate` checks that every skill has a SKILL.md with a safe frontmatter
(no aliases, anchors or tags), a non-empty name and description, a name that
matches its directory, no duplicate names, no symlink leaving the library,
size limits, and none of a short list of forbidden commands (permission
prompts disabled, a download piped into a shell) in SKILL.md, executable
files or a `scripts/` or `bin/` directory. `inventory` lists each
skill with a content hash so any change to a skill is visible. Syncing a
library between machines is deliberately not built: it waits until one
machine is named the canonical copy and changes to skills have an approval
step ([#87](https://github.com/roshanaryal1/home-lab/issues/87)).

## Operating the lab

```sh
uv run python -m lab.cli status [--json]          # queue, worker health, counters; exit 2 if unhealthy
uv run python -m lab.cli approvals                # what is waiting for a decision
uv run python -m lab.cli show <id>                # read the exact call before deciding
uv run python -m lab.cli approve <id> --by you --key operator.key --expect-hash <prefix>
uv run python -m lab.cli operator init --dir ~/.lab-operator   # create the approval signing key
uv run python -m lab.cli deny <id> --by you       # refuse it; the parked task is cancelled
uv run python -m lab.cli tasks                    # task counts by state
uv run python -m lab.cli control pause|resume|drain|stop --by you   # the whole-lab switch; `control show` reads it
uv run python -m lab.cli watchdog [--max-age 90] [--dry-run]   # kill a hung supervisor; launchd restarts it
uv run python -m lab.cli cancel <task> --by you   # cancel work that has not started
uv run python -m lab.cli tick [--repo owner/repo]     # one pass: observe, summarize with the model, route
uv run python -m lab.cli emit [--min-failures 3]  # queue proposals from patterns in the event log
uv run python -m lab.cli chain <task>             # the events that produced a proposal
uv run python -m lab.cli ops                      # operations of unknown outcome
uv run python -m lab.cli resolve <op> --happened|--not-happened --by you   # reconcile one
uv run python -m lab.cli audit verify             # walk the hash-chained event log
uv run python -m lab.cli audit checkpoint --key K --out DIR    # signed head, kept outside the lab
uv run python -m lab.cli artifacts verify         # re-hash every stored output
uv run python -m lab.cli backup --to DIR          # online snapshot, then restore-check to prove it
uv run python -m lab.cli restore-check <manifest> --into DIR   # restore into a fresh dir and verify everything
uv run python -m lab.cli drill crash              # inject a real failure and log it (ops/drills/)
uv run python -m lab.cli skillstore submit|promote|known-good|rollback|history|install   # versioned skills, operator-promoted, one-step rollback
uv run python -m lab.cli publish list|show <key>|reconcile <key> --connectors FILE   # receipts for credentialed sends; ask the provider about a lost response
uv run python -m lab.cli memory search|inspect|add-evidence|correct|revoke|delete   # inspectable FTS5 memory
uv run python -m lab.cli route <task> [--want paper]   # post, blog, paper or nothing, by evidence weight; thin evidence refused upward
uv run python -m lab.cli ledger show <task>       # claims, their evidence and status; review, verify
uv run python -m lab.cli eval run --endpoint URL --model M --revision H --tokenizer-revision H --weights-mb N   # 24 fixed tasks, sealed provenance record
uv run python -m lab.cli eval rerun <record>      # repeat a run from its record alone, then compare
uv run python -m lab.attacks                      # benign-plus-hostile scenarios against a stub model
```

Approvals are signed with the operator's private key and the supervisor
honours only signatures that verify against the public key it is given
(`LAB_OPERATOR_PUBKEY`). That is a boundary only once the key is out of the
agent account's reach.

## Layout

```
lab/migrations/      numbered SQL migrations for the queue, audit and origin schema
lab/queue.py         state machine, leases, retry, crash recovery
lab/supervisor.py    asyncio loop, concurrency slots, dispatch
lab/policy.py        capability tiers, approvals (operator-signed), the gate
lab/authority.py     the Rule of Two, enforced per task (ADR 0006)
lab/origin.py        where a task's input came from, and the taint that follows it
lab/broker.py        typed tools, workspaces, per-call audit
lab/egress.py        the only outbound path: default-deny, resolve-then-pin
lab/vault.py, connectors.py, publish.py   secrets injected per call, one destination each, receipts and reconciliation
lab/audit.py         append-only hash chain and signed checkpoints
lab/artifacts.py     content-addressed task outputs
lab/backup.py, drills.py      verifying backup and logged recovery drills
lab/metrics.py       `lab status`, derived from the event log
lab/control.py       the operator mode switch: pause, drain, stop
lab/service.py       heartbeat, watchdog and the launchd definitions (`ops/launchd/`)
lab/loop.py          the loop: summarizer handler, ledger claim, rubric route, `lab tick`
lab/emitter.py       proposals emitted from the event log, each with its event chain
lab/model.py         bounded model adapter: pinned revisions, admission, strict tool calls
lab/memory.py        inspectable memory: FTS5, provenance, expiry, revoke that reaches drafts
lab/rubric.py        the router's rules: evidence weight to post, blog, paper or nothing
lab/ledger.py        research claims with statuses separate from task state, evidence snapshots
lab/evals.py         fixed task set run against any endpoint, sealed provenance records
lab/attacks.py       injection harness
evals/tasks.jsonl    the 24 tasks (arithmetic, extraction, format, code, tool calls, injection)
lab/skills.py        read-only skill validator and inventory
lab/skillstore.py    skills as versioned artifacts: candidate, promote, known good, rollback
THREATS.md           OWASP agentic top 10 mapped to controls and tests
docs/decisions/      ADRs 0001 to 0007
ops/mac-mini-setup.md  setup and parked-hardware checklists for the mini itself
ops/drills/          recovery drill template and dated records
tests/               pytest suite
```

## Running the tests

```sh
uv sync --locked --extra dev   # exact Python from .python-version, deps from uv.lock
uv run python -m pytest tests/ -q
```

One runtime dependency, `cryptography`, for the operator's Ed25519
approval signatures: the standard library has no asymmetric signatures and
a shared secret would let the verifier forge. Async tests run through a
small hook in `tests/conftest.py` rather than pulling in `pytest-asyncio`,
because this machine is meant to run unattended and every dependency is a
thing that can break at 3am.

## Citing

`CITATION.cff` is validated in CI and kept in step with the package version.
A DOI needs the owner to connect Zenodo; the steps are in
[`ops/release.md`](ops/release.md).

## Setting up the mini

See [`ops/mac-mini-setup.md`](ops/mac-mini-setup.md). Do the accounts
and network sections before anything else; the dedicated non-admin user
is what keeps an agent mistake survivable.
