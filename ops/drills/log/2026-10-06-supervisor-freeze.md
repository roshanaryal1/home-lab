# Drill: frozen supervisor replaced by the watchdog

- date: 2026-10-06, after 06:34:43 UTC (the session script did not timestamp the drills; the report is `docs/reviews/2026-10-06-mac-session.md`)
- machine: macOS 27.0 (26A428) arm64, deployed commit 7bf235c
- target: mac-mini
- result: FAIL
- counts as demonstrated: no

## Failure injected
`sudo kill -STOP` on the running supervisor (pid 47635).

## Expected
The watchdog (`StartInterval` 30 s, `DEFAULT_MAX_AGE` 90 s) kills the frozen process within about 120 s and launchd starts another.

## Actual
After 182 s pid 47635 was still there (the loop ends at 180 s). The script resumed it with `kill -CONT`; `watchdog --dry-run` then said `healthy pid 47635 (heartbeat 1s old)`. No replacement happened. The script's summary line says 'gone after 182 s' and 'new supervisor 47635', which is its fixed wording and not what happened.

## Follow-up
#271 (cause not known; needs the watchdog logs, which are in a lab-only folder)
