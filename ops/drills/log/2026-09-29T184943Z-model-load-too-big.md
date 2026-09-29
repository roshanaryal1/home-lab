# Drill: model-load-too-big

- date: 2026-09-29T18:49:43+00:00
- machine: Darwin 27.0.0 arm64, python 3.13.15, sqlite 3.53.1
- target: mac-mini
- result: PASS
- counts as demonstrated: yes

## Failure injected
a model larger than the memory budget

## Expected
AdmissionRefused before the server is called

## Actual
AdmissionRefused: this request would need 20504 MB resident against a 20500 MB budget; server calls=0

## Follow-up
none
