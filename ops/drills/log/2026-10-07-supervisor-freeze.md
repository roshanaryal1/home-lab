# Drill: frozen supervisor replaced by the watchdog

- date: 2026-10-06, 12:10 UTC onward (the session script did not timestamp each drill; the report is `docs/reviews/2026-10-07-mac-session-drills-pass-1.md`)
- machine: macOS 27.0 (26A428) arm64, deployed commit 4bfa910
- target: mac-mini
- result: PASS
- counts as demonstrated: yes (pass 1 of 2 for the freeze drill; a second pass is required before #78 and #271 close)

## Failure injected
`sudo kill -STOP` on pid 66280, the supervisor started by the previous drill, after its first heartbeat was seen and checked again immediately before the freeze.

## Expected
The watchdog (`StartInterval` 30 s, `DEFAULT_MAX_AGE` 90 s) kills the frozen process within about 120 s and launchd starts another.

## Actual
The frozen pid was gone after 96 s and a new supervisor (pid 66453) was running at that check. `watchdog --dry-run` then said `healthy pid 66453 (heartbeat 2s old)`. This is the first drill since the watchdog fallback for a supervisor that never wrote a heartbeat was deployed (#275); the 2026-10-06 failure was a supervisor frozen before its first heartbeat.

## Follow-up
#271 (needs a second passing run)
