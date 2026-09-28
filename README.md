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
| 3. Model adapter, one heavy + one light model | needs the M6 |
| 4. Concurrency semaphores (heavy=1, light=2-3) | done, in the supervisor |
| 5. Model swap manager with RAM/headroom policy | needs the M6 |
| 6. Aider/OpenHands executor adapters | not started |
| 7. Research evidence ledger and verification pipeline | not started, [#90](https://github.com/roshanaryal1/home-lab/issues/90) |
| 8. Dedicated-user permissions and task workspaces | checklist written |
| 9. launchd + watchdog + queue-aware caffeinate | checklist written |
| 10. Tailscale-only FastAPI dashboard and emergency stop | not started; metrics [#89](https://github.com/roshanaryal1/home-lab/issues/89), controls [#79](https://github.com/roshanaryal1/home-lab/issues/79) |
| 11. sqlite-vec / FTS retrieval | not started |
| 12. Benchmark and tune before adding anything else | not started |

Steps 1, 2 and 4 are machine-independent and run anywhere. Everything
touching model residency needs the 32 GB machine to mean anything.

The table above is the execution substrate: safe to leave running
unattended, but it has no opinion about what work should exist. Three
planes on top of it are what make this an operating system rather than
a task runner, and none of them exist yet:

| Plane | Issue | State |
|---|---|---|
| Observation: notice work from the event stream, emit proposals | [#32](https://github.com/roshanaryal1/home-lab/issues/32) | not started |
| Router: post / blog / paper by evidence weight | [#33](https://github.com/roshanaryal1/home-lab/issues/33) | not started |
| Publish: post/email on your behalf | blocked on [#15](https://github.com/roshanaryal1/home-lab/issues/15) | not started |

Build order and reasoning are in ADR 0004.

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

## Layout

```
lab/schema.sql       task queue and audit schema, WAL-backed
lab/queue.py         state machine, leases, retry, crash recovery
lab/supervisor.py    asyncio loop, concurrency slots, dispatch
ops/mac-mini-setup.md  setup checklist for the mini itself
tests/               pytest suite, no external dependencies
```

## Running the tests

```sh
uv sync --locked --extra dev   # exact Python from .python-version, deps from uv.lock
uv run python -m pytest tests/ -q
```

No dependencies beyond the standard library and pytest. Async tests run
through a small hook in `tests/conftest.py` rather than pulling in
`pytest-asyncio`, because this machine is meant to run unattended and
every dependency is a thing that can break at 3am.

## Setting up the mini

See [`ops/mac-mini-setup.md`](ops/mac-mini-setup.md). Do the accounts
and network sections before anything else; the dedicated non-admin user
is what keeps an agent mistake survivable.
