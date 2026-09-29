# Drill: crash-idempotent

- date: 2026-09-29T08:15:43+00:00
- machine: Darwin 27.0.0 arm64, python 3.13.15, sqlite 3.53.1
- target: mac-mini
- result: PASS
- counts as demonstrated: yes

## Failure injected
SIGKILL of the process while a idempotent task was running

## Expected
the task returns to the queue

## Actual
process killed=True; recovery {'interrupted': 1, 'requeued': 1, 'held_for_review': 0}; states {'queued': 1}

## Follow-up
none
