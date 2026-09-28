# System architecture, current state

**2026-09-28.** What this build actually looks like right now: substrate
plus the three planes ADR 0004 names as missing. Not a design document; the
design is `llm-architects/analysis/consensus/reference-architecture.md`
(frozen, the P1 research artifact) and `docs/decisions/0004-operating-system.md`
(the current target, prose). This page exists so one diagram answers "what
is actually built" without reading either.

Update this when a component's status changes, in the same commit or PR as
the change, the way `README.md`'s status table already works. A diagram that
drifts from the code is worse than no diagram.

## Diagram

```mermaid
flowchart TB
    subgraph substrate["Execution substrate: built, 114 tests, CI on 2 platforms"]
        direction TB
        queue["SQLite WAL queue<br/>lab/queue.py<br/>leases, fencing, idempotency"]
        supervisor["Supervisor<br/>lab/supervisor.py<br/>bounded worker pool"]
        gate["Capability gate<br/>lab/policy.py, lab/broker.py<br/>4 tiers, per task and per tool call"]
        sandbox["Seatbelt sandbox<br/>lab/sandbox.py<br/>verified macOS 26.5.1 + 27"]
        queue --> supervisor --> gate --> sandbox
    end

    subgraph model["Model layer: ADR 0001, unbenchmarked"]
        heavy["Qwen3-Coder-30B-A3B, MLX 4-bit<br/>~16.7 GB, ONE heavy inference slot"]
    end

    subgraph memory["Memory: ADR 0003, FTS5 baseline built"]
        mem["SQLite FTS5 (built)<br/>sqlite-vec later, only if it beats FTS5"]
    end

    subgraph planes["Three planes ADR 0004 names as missing"]
        direction TB
        obs["Observation plane<br/>issue #32, first slice only<br/>watches event log, emits proposals"]
        router["Artifact router<br/>issue #33, rubric built over the evidence ledger<br/>post / blog / paper by evidence weight"]
        publish["Publish plane<br/>connectors, secret broker, receipts and reconciliation built<br/>tested against a dummy provider only"]
        obs -->|"proposal, as an ordinary queue row"| router
        router -->|"draft + evidence chain"| publish
    end

    supervisor -.->|"executes tasks with"| heavy
    supervisor -.->|"reads/writes"| memory
    obs -.->|"reads"| queue
    router -->|"through the same gate"| gate
    publish -->|"through the same gate"| gate

    classDef built fill:#d4edda,stroke:#2d6a4f,color:#1b4332
    classDef missing fill:#f8d7da,stroke:#842029,color:#58151c
    classDef designonly fill:#fff3cd,stroke:#997404,color:#664d03
    class queue,supervisor,gate,sandbox built
    class obs,router,publish missing
    class heavy,mem designonly
```

Green: built and tested. Yellow: designed, not built. Red: not started.
Dashed arrows are read/write relationships; solid arrows are the flow a
proposal actually takes.

## The constraint the diagram exists to keep visible

**One heavy inference slot**, not one per plane and not one per proposal in
flight. Every box in `planes` competes for the same slot the substrate uses
to execute ordinary tasks. Hundreds of *queued* proposals is fine; hundreds
of *concurrent* model calls is not achievable on this hardware. See ADR
0004's correction section, which this diagram exists partly to stop people
re-losing.

## Status table

| Component | File / doc | Status | Owner |
|---|---|---|---|
| Queue | `lab/queue.py`, `lab/migrations/` | Built, tested. The schema is versioned: numbered SQL migrations run one transaction each and record the version in `PRAGMA user_version`; a failed one rolls back whole, and a database from a newer build is refused, `tests/test_migrations.py` (item 3.1). CHECK constraints cap payload at 64 KiB and result at 1 MiB and keep the attempt counters sane. Commits are durable (synchronous=FULL, fullfsync) and start-up refuses a SQLite with the WAL-reset bug, `tests/test_durability.py`. Races, a Hypothesis state machine and SIGKILL of a real process mid-transaction are covered in `tests/test_races.py` (item 2.4); power-loss durability is not, and waits for the Mac mini. The audit log is append-only by trigger, hash-chained, checkpointed with a signed head and written for every broker call, `lab/audit.py`, `tests/test_audit.py` (item 3.2). Task outputs are stored content-addressed with a descriptor row before success is recorded, `lab/artifacts.py`, `tests/test_artifacts.py` (item 3.3). Online backup and a restore that re-verifies hashes, chain and artifacts, `lab/backup.py`; drills logged under `ops/drills/` (items 3.4, #91) | n/a |
| Supervisor | `lab/supervisor.py` | Built, tested | n/a |
| Capability gate | `lab/policy.py`, `lab/broker.py` | Rule of Two enforced before the gate, `lab/authority.py` (item 4.1). Approvals are operator-signed (Ed25519) and verified by the supervisor, `lab/operator.py` (item 4.5). Every task carries a derived origin and taint through lineage, `lab/origin.py`, `lab/untrusted.py` (item 4.2). Task-level and per-tool-call: built, tested (item 1.1). Handlers call through a session bound to their task and lease (1.2); reviewed handlers run in a worker process (`lab/worker.py`); separate OS account pending, [#70](https://github.com/roshanaryal1/home-lab/issues/70) | n/a |
| Sandbox | `lab/sandbox.py` | Built, tested on macOS 26.5.1 and 27 | n/a |
| Model adapter | `lab/model.py` | Built against a mock and a stub loopback server: pinned revisions, admission control (tokens, time, residency, one heavy slot), strict tool-call parsing. Real model and measured budget wait for the M6 | [#74](https://github.com/roshanaryal1/home-lab/issues/74) |
| Heavy model | Qwen3-Coder-30B-A3B, MLX 4-bit | Chosen, **unbenchmarked** on the M6 | [ADR 0001](decisions/0001-heavy-model.md) |
| Python runtime | uv-managed | Chosen; mini needs patch-version pin | [ADR 0002](decisions/0002-python-runtime.md) |
| Memory | `lab/memory.py` (SQLite FTS5) | FTS5 baseline built: provenance, trust, expiry, inspect, correct, revoke (reaches the ledger), delete. `sqlite-vec` embeddings not built and must beat this baseline first | [ADR 0003](decisions/0003-memory.md) |
| Observation plane | `lab/observe.py`, `lab/slice.py` | GitHub slice (#39, #46): closed issues and merged PRs become queued proposals. Event-log emitter (`lab/emitter.py`, `lab emit`, #32): the `repeated_failure` rule queues a `notify`-tier, event-origin proposal naming the event ids behind it (`lab chain`). Other rules need signals nothing emits yet | [#32](https://github.com/roshanaryal1/home-lab/issues/32) |
| Artifact router | `lab/rubric.py`, `lab/route.py` | Rubric built (7.2): routes a research task by evidence weight over the ledger, refuses thin evidence upward, states conflicts, bounded stop rule. The single-signal v0 in `route.py` remains for the vertical slice. Human review of every route | [#33](https://github.com/roshanaryal1/home-lab/issues/33) |
| Secret broker and connectors | `lab/vault.py`, `lab/connectors.py` | Built and tested with dummy credentials and a fake transport; Keychain path unexercised until the mini | [#15](https://github.com/roshanaryal1/home-lab/issues/15) |
| Publish plane | `lab/publish.py`, `lab/broker.py` | Reviewed publishing built against a dummy provider: approval bound to destination and draft hash, write-ahead receipts, idempotency keys, reconciliation of lost responses. No real destination yet | [#86](https://github.com/roshanaryal1/home-lab/issues/86) |
| Network egress control | `lab/egress.py` | Built, tested with a fake resolver and transport (metadata-address redirect, DNS rebinding, IP-literal spellings); not yet exercised against a real host | [#14](https://github.com/roshanaryal1/home-lab/issues/14) |
| Resource ceilings per task | n/a | Open | [#16](https://github.com/roshanaryal1/home-lab/issues/16) |
| `dscl` account enumeration | `lab/sandbox.py` | Accepted, not narrowed; untrusted code is routed to a disposable container instead | [ADR 0007](decisions/0007-isolation-for-untrusted-code.md), [#27](https://github.com/roshanaryal1/home-lab/issues/27) |
| Operational metrics | `lab/metrics.py`, `lab status` | Built: queue depth and ages, worker health, counters for denials, retries, lease losses and forced terminations, all queries over the event log with no second store; model metrics wait for 5.1 | [#89](https://github.com/roshanaryal1/home-lab/issues/89) |
| Operator controls | `lab/control.py`, `lab control`, `lab cancel` | Built: pause, resume, drain and stop as one database row obeyed by the supervisor; stop runs the emergency stop and persists across restart. Watchdog, alerts and dead-man switch need the M6 | [#79](https://github.com/roshanaryal1/home-lab/issues/79) |
| Evidence ledger | `lab/ledger.py`, `lab ledger` | Built: research-task record, claims with a status separate from the task's, quote-checked evidence snapshots, a review pass required before a draft is reviewable; the router (7.2) is not yet built on it | [#90](https://github.com/roshanaryal1/home-lab/issues/90) |
| Utility evals | `lab/evals.py`, `evals/tasks.jsonl` | 24 fixed tasks graded deterministically; sealed provenance record; rerun from the record alone. Tested against a stub endpoint; real-model runs wait for the M6 | [#81](https://github.com/roshanaryal1/home-lab/issues/81) |
| Skill store | `lab/skillstore.py`, `lab/skills.py` | Skills as immutable versions with lineage, operator-signed promotion, tier that a skill cannot lower, known-good marks and one-step rollback. The validator/inventory (8.7a) feeds it. No agent loads from it yet | [#87](https://github.com/roshanaryal1/home-lab/issues/87) |
| Injection harness | `lab/attacks.py` | 9 benign-plus-hostile scenarios against a worst-case stub model, graded on files, database and network; real-model run parked | [#72](https://github.com/roshanaryal1/home-lab/issues/72) |
| Tailscale-only dashboard | n/a | Not started | reference architecture §11 |
| launchd + watchdog | `lab/service.py`, `ops/launchd/`, `lab watchdog` | Built: heartbeat written from the event loop, a periodic watchdog that kills a supervisor whose heartbeat is stale (pid and start time checked), and generated LaunchDaemon definitions. Not yet installed or exercised on the M6 | [#78](https://github.com/roshanaryal1/home-lab/issues/78), `ops/mac-mini-setup.md` §16 |

## Related documents

- [`docs/decisions/0004-operating-system.md`](decisions/0004-operating-system.md), the framing this diagram implements
- [`docs/decisions/0005-multi-tenant-proposal-review.md`](decisions/0005-multi-tenant-proposal-review.md), what's adopted vs deferred from a larger enterprise-platform proposal
- [`docs/decisions/0006-authority-rules-and-rollout.md`](decisions/0006-authority-rules-and-rollout.md), the Rule of Two per plane and the staged rollout of research agents tied to gates G1 to G6
- [`THREATS.md`](../THREATS.md), the OWASP agentic top 10 mapped to controls, tests and open issues
- [`docs/PLAN.md`](PLAN.md), the research and defect history that led here
- [`docs/PIPELINE.md`](PIPELINE.md), which papers each piece of this feeds
- [`docs/SUBSYSTEMS.md`](SUBSYSTEMS.md), design detail for four specific subsystems
- `llm-architects/analysis/consensus/reference-architecture.md`, the frozen P1 spec this build originally set out to implement
