# Drill: crash-non-idempotent

- date: 2026-09-29T08:15:43+00:00
- machine: Darwin 27.0.0 arm64, python 3.13.15, sqlite 3.53.1
- target: mac-mini
- result: PASS
- counts as demonstrated: yes

## Failure injected
SIGKILL of the process while a non-idempotent task was running

## Expected
the task is held for a person; it is never replayed blindly

## Actual
process killed=True; recovery {'interrupted': 1, 'requeued': 0, 'held_for_review': 1}; states {'interrupted': 1}

## Follow-up
none
