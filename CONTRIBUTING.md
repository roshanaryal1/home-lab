# Contributing

## Before anything else

Read `docs/PLAN.md`. This project implements a specific reference architecture
that was adjudicated from a controlled study, not invented ad hoc. Changes that
contradict the architecture need to argue with the architecture, not route
around it.

## The workflow, in order

**Issue first. Always. No exceptions for "small" or "obvious".**

```
issue  ->  branch  ->  PR  ->  CI  ->  merge  ->  issue closes
```

1. **Open an issue before writing code.** Bug, task or research question.
   The template forces the parts that are easy to skip: measured evidence
   rather than a theory, and acceptance criteria that decide when it is done.
2. **Branch from the issue.** `fix/`, `feat/`, `docs/`, `chore/`.
3. **PR links the issue** with `Closes #N`, and shows evidence: what was run
   and what it printed, not "should work".
4. **CI must pass.** Lint, `mypy` strict, tests with a 90% coverage floor on
   queue, policy, broker and sandbox, and the safety-marker guard.
5. **Merge squashes**, so `main` reads one commit per issue.

### Why this is not bureaucracy

Three concrete reasons, each learned here rather than imported:

- **An issue makes you state the evidence before the fix.** Two defects were
  filed on 2026-09-25 as "observations from reading the source". Testing them
  turned one from a theory into a measured 19-against-1, and revealed the
  second was worse than described. Writing the issue is where that happens.
- **Acceptance criteria stop a fix from being declared done too early.** The
  lease fix passed its unit tests while the heartbeat silently did nothing;
  the regression criterion caught it.
- **A closed issue is the only durable record of why.** Six months on, the
  commit says what changed. The issue says what was broken, how it was
  measured, and what was ruled out.

## Ground rules

1. **Every change goes through a pull request.** `main` is protected.
2. **Tests must pass**, and behaviour changes need tests. Run
   `uv sync --locked --extra dev && uv run python -m pytest tests/ -q`.
   If you change dependencies, commit the updated `uv.lock`; CI fails
   when it is out of date. Before pushing also run `uv run ruff check .`,
   `uv run mypy` and `uv run coverage run -m pytest tests -q`.
3. **Tests marked `@pytest.mark.safety` are load-bearing**: a crash never
   blindly replays a non-idempotent task, the heavy inference slot is never
   breached, and the reproduced defects R01 to R11 stay fixed. CI fails if
   fewer than 43 are collected, so removing one is never a silent green
   build; raise the floor in `.github/workflows/check.yml` when adding
   more. If a change touches `lab/queue.py` `recover()` or the supervisor
   semaphores, say so in the PR body.
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
