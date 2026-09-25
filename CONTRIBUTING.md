# Contributing

## Before anything else

Read `docs/PLAN.md`. This project implements a specific reference architecture
that was adjudicated from a controlled study, not invented ad hoc. Changes that
contradict the architecture need to argue with the architecture, not route
around it.

## Ground rules

1. **Every change goes through a pull request.** `main` is protected.
2. **Tests must pass**, and behaviour changes need tests. Run
   `python3 -m pytest tests/ -q`.
3. **Two safety properties are load-bearing** and must keep passing:
   a crash never blindly replays a non-idempotent task, and the heavy
   inference slot is never breached. If a change touches `lab/queue.py`
   `recover()` or the supervisor semaphores, say so in the PR body.
4. **No new runtime dependencies without justification.** This runs unattended;
   every dependency is a thing that can break at 3am. The test suite
   deliberately avoids `pytest-asyncio` for this reason.
5. **Verify, do not assert.** If a PR claims something works, say what was run.

## Style

- Line length 100, enforced by `ruff`.
- Match the surrounding code's comment density and naming.
- Comments explain *why*, not *what*.

## Commit messages

Describe what changed and why it changed. Prose over bullet soup. No tool or
assistant attribution anywhere: not in commits, PR descriptions, issues, code
comments or docs.
