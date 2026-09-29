# Drill: model-load-server-down

- date: 2026-09-29T18:50:21+00:00
- machine: Darwin 27.0.0 arm64, python 3.13.15, sqlite 3.53.1
- target: mac-mini
- result: PASS
- counts as demonstrated: yes

## Failure injected
the inference server is not listening

## Expected
a ModelError within 10 s, and the heavy slot is free afterwards

## Actual
ModelError after 0.00 s: inference server unavailable: [Errno 61] Connection refused; slot free=True

## Follow-up
none
