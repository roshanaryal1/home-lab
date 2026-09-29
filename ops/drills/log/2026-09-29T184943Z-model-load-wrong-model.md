# Drill: model-load-wrong-model

- date: 2026-09-29T18:49:43+00:00
- machine: Darwin 27.0.0 arm64, python 3.13.15, sqlite 3.53.1
- target: mac-mini
- result: FAIL
- counts as demonstrated: no

## Failure injected
the server at http://127.0.0.1:8080/v1 serves a different model

## Expected
ModelMismatch, and the heavy slot is free afterwards

## Actual
ModelError: inference server returned 404; slot free=True

## Follow-up
none
