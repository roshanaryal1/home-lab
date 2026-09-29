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
| 3. Model adapter, one heavy + one light model | adapter built and tested against a mock, then run against the real heavy model on the M6 with measured memory ([#74](https://github.com/roshanaryal1/home-lab/issues/74), ADR 0001); the light model is not chosen yet |
| 4. Concurrency semaphores (heavy=1, light=2-3) | done, in the supervisor |
| 5. Model swap manager with RAM/headroom policy | not built; the budget it needs is now measured on the M6 (ADR 0001) |
| 6. Aider/OpenHands executor adapters | not started |
| 7. Research evidence ledger and verification pipeline | ledger built: claim status is separate from task status, every claim opens its exact source ([#90](https://github.com/roshanaryal1/home-lab/issues/90)); the automated verification pipeline is not |
| 8. Dedicated-user permissions and task workspaces | code done (operator-signed approvals); account setup is a checklist for the mini, [#70](https://github.com/roshanaryal1/home-lab/issues/70) |
| 9. launchd + watchdog + queue-aware caffeinate | heartbeat, watchdog and plists built and tested; install and freeze test are on the M6 checklist, [#78](https://github.com/roshanaryal1/home-lab/issues/78) |
| 10. Tailscale-only FastAPI dashboard and emergency stop | `lab status`, `lab control` (pause, drain, stop), `lab cancel` and a read-only `lab dashboard` on loopback exist; alerts and the dead-man switch are parked, [#79](https://github.com/roshanaryal1/home-lab/issues/79) |
| 11. sqlite-vec / FTS retrieval | FTS5 baseline built with inspect, correct, revoke and delete ([#85](https://github.com/roshanaryal1/home-lab/issues/85)); embeddings must beat it on a measured task first |
| 12. Benchmark and tune before adding anything else | benchmarked on the M6 (`evals/bench/`, setup section 14); first tuning test run as pre-registered H3 (prompt cache 1 against 4: no gain, not adopted, `docs/PREREGISTRATION.md`) |

Steps 1, 2 and 4 are machine-independent and run anywhere. Everything
touching model residency needs the 32 GB machine to mean anything, and
that machine is now running (below).

The table above is the execution substrate: safe to leave running
unattended, but it has no opinion about what work should exist. Three
planes on top of it are what make this an operating system rather than
a task runner:

| Plane | Issue | State |
|---|---|---|
| Observation: notice work from the event stream, emit proposals | [#32](https://github.com/roshanaryal1/home-lab/issues/32) | this repo's closed issues and merged PRs become proposals; the event log yields a proposal when tasks of one kind keep failing alike, when closed issues look alike, or when a measurement has no artifact |
| Router: post / blog / paper by evidence weight | [#33](https://github.com/roshanaryal1/home-lab/issues/33) | rubric built over the evidence ledger: deterministic rules, refuses thin evidence upward, inspectable chain; still needs a human to review every route |
| Publish: post/email on your behalf | [#86](https://github.com/roshanaryal1/home-lab/issues/86) | reviewed publishing built and tested against a dummy provider: approval bound to destination and draft hash, write-ahead receipts, idempotency keys, reconciliation of lost responses; no real destination has been used |

Build order and reasoning are in ADR 0004; the rules that limit what an
agent may hold, and the staged rollout, are in ADR 0006.

## Running on the Mac mini

Since 2026-09-30 the lab's heavy model runs on the M6 itself:
`Qwen3-Coder-30B-A3B-Instruct`, the 4-bit DWQ build (the plain 4-bit build
corrupts copied text, ADR 0001), served by `mlx-lm` on loopback only and kept
up by a user LaunchAgent (after a `kill -9` it was back in 16 seconds). The supervisor, the lab account and the lab's own launchd
daemons are not installed yet (setup sections 11 and 16). Until the lab
account exists, reviewed handlers run in a worker process as the same
macOS user (`lab/worker.py`), so the process is the boundary; only the
broker's `shell.run` is sandboxed (`lab/sandbox.py`).

**Reaching it.** The Mac mini and the owner's devices share a private
Tailscale network. Nothing listens on the internet and no router port is
forwarded. SSH works over the tailnet; the model and the dashboard stay on
the Mac mini's loopback and are reached through an SSH tunnel, never by
widening what they bind to:

```sh
ssh -N -L 8080:127.0.0.1:8080 <user>@<mac-mini>   # the model, at http://127.0.0.1:8080/v1
ssh -N -L 8765:127.0.0.1:8765 <user>@<mac-mini>   # lab dashboard, once the lab runs
```

**What has been measured there so far**, each with its record:

| What | Result | Where |
|---|---|---|
| Apple `container`, measured for the planned untrusted-code tier (not used by the lab yet) | 0.64 s median start; inside a container the host's accounts and files were not visible; network is on by default, so that executor must turn it off | ADR 0007 |
| Heavy model memory and speed | 17.2 GB loaded, about 200 KB per token of context, about 16K tokens under the 20.5 GB budget, about 67 tok/s | ADR 0001 |
| Utility evaluation, 24 tasks | heavy 19, 4B baseline 20; reruns identical | setup section 14, `evals/runs/` |
| Prompt injection with the real model driving | 0 of 9 attacks succeeded; the model tried 2, the broker stopped both | `SECURITY.md` |
| Backup and recovery | encrypted external backup disk; crash drill passed | setup section 10, `ops/drills/log/` |

**Pre-registered tests.** The evaluation plan is registered on OSF
([osf.io/jfp74](https://osf.io/jfp74), 2026-09-29 14:45 UTC) at commit
`d8726b43`. Runs made before that are listed in its Amendment 1 as
exploratory; the confirmatory runs come after it.

**One caveat for always-on.** FileVault is on, so after a power cut the
Mac restarts and waits at the login screen; nothing, the model included,
runs until someone logs in.

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

The repository is public: nothing in it is a credential by design, and the
history is scanned for secrets in CI. See [SECURITY.md](SECURITY.md) for the
repository controls and how to report a vulnerability privately.

Run the lab only with dummy data, review every draft by hand and connect
no real credentials until three things are true. All three are work on the
Mac mini, which is now running, and none is done yet: a separate non-admin lab account that
cannot read the operator's approval key ([#70](https://github.com/roshanaryal1/home-lab/issues/70)),
per-task memory and CPU ceilings sized on the mini (the mechanism exists, [#16](https://github.com/roshanaryal1/home-lab/issues/16)),
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
uv run python -m lab.cli status [--json] [--alert-config F]   # queue, worker health, counters; exit 2 if unhealthy
uv run python -m lab.cli approvals                # what is waiting for a decision
uv run python -m lab.cli show <id>                # read the exact call before deciding
uv run python -m lab.cli approve <id> --by you --key operator.key --expect-hash <prefix>
uv run python -m lab.cli operator init --dir ~/.lab-operator   # create the approval signing key
uv run python -m lab.cli deny <id> --by you       # refuse it; the parked task is cancelled
uv run python -m lab.cli tasks                    # task counts by state
uv run python -m lab.cli control pause|resume|drain|stop --by you   # the whole-lab switch; resume takes --key operator.key; `control show` reads it
uv run python -m lab.cli watchdog [--max-age 90] [--dry-run]   # kill a hung supervisor; launchd restarts it
uv run python -m lab.cli cancel <task> --by you   # cancel work that has not started
uv run python -m lab.cli selftest [--alert-config F]   # chain, backup restore, health, safety tests; alerts on failure
uv run python -m lab.cli setup-plan [--apply]      # print the lab-account setup; --apply needs root on macOS
uv run python -m lab.cli keepawake [--once] [--grace 600]   # hold caffeinate only while work is pending
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
uv run python -m lab.cli shadow --cases evals/shadow_cases.jsonl   # measure the rubric on labeled cases; a model candidate is compared in shadow, never applied
uv run python -m lab.cli dashboard [--port 8765]   # read-only status page on loopback; no controls, GET only, everything escaped
uv run python -m lab.cli ledger show <task>       # claims, their evidence and status; review, verify
uv run python -m lab.cli eval run --endpoint URL --model M --revision H --tokenizer-revision H --weights-mb N   # 24 fixed tasks, sealed provenance record; --db notes it as a measurement
uv run python -m lab.cli eval rerun <record>      # repeat a run from its record alone, then compare
uv run python -m lab.cli bench run --endpoint URL --model M --revision H --tokenizer-revision H --weights-mb N   # cold start, first token, decode speed, server memory
uv run python -m lab.cli bench tune <baseline> <candidate>   # recommend a setting only on a measured gain with no task lost
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
lab/selftest.py, alert.py   nightly self-test; the operator-configured alert hook
lab/accountplan.py   the lab-account setup written as a plan, dry-run by default
lab/shadow.py        candidate routing model measured against the rubric on labeled cases; advice only
lab/grammar.py       tool-call JSON Schema generated from the broker table, for constrained decoding
lab/dashboard.py     read-only status page on loopback
lab/keepawake.py, logsetup.py   queue-aware sleep prevention; rotating private JSON logs
lab/loop.py          the loop: summarizer handler, ledger claim, rubric route, `lab tick`
lab/emitter.py       proposals emitted from the event log, each with its event chain
lab/model.py         bounded model adapter: pinned revisions, admission, strict tool calls
lab/memory.py        inspectable memory: FTS5, provenance, expiry, revoke that reaches drafts
lab/rubric.py        the router's rules: evidence weight to post, blog, paper or nothing
lab/ledger.py        research claims with statuses separate from task state, evidence snapshots
lab/bench.py         benchmark of one endpoint and the tuning gate
lab/evals.py         fixed task set run against any endpoint, sealed provenance records
lab/attacks.py       injection harness
evals/tasks.jsonl    the 24 tasks (arithmetic, extraction, format, code, tool calls, injection)
evals/shadow_cases.jsonl   12 labeled research cases for the shadow experiment
docs/PREREGISTRATION.md    draft plan to register before the first real-model run
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
