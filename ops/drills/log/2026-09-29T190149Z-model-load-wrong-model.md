# Drill: model-load-wrong-model

- date: 2026-09-29T19:01:49+00:00
- machine: Darwin 27.0.0 arm64, python 3.13.15, sqlite 3.53.1
- target: mac-mini
- result: PASS
- counts as demonstrated: yes

## Failure injected
the server at http://127.0.0.1:8080/v1 is asked for a model it does not serve

## Expected
refused, never answered: ModelMismatch if the server answers as another model, or the server's own 404 for an unknown model; the heavy slot is free afterwards

## Actual
ModelError: inference server returned 404; slot free=True

## Follow-up
none
