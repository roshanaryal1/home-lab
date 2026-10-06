# Drill: frozen supervisor replaced by the watchdog (pass 2 of 2)

- date: 2026-10-06, 12:53 UTC onward (the session script did not timestamp each drill; the report is `docs/reviews/2026-10-07-mac-session-drills-pass-2.md`)
- machine: macOS 27.0 (26A428) arm64, deployed commit 4bfa910
- target: mac-mini
- result: PASS
- counts as demonstrated: yes (this is the second of two required passes after the fix for #271)

## Failure injected
`sudo kill -STOP` on pid 75030, the supervisor started by the preceding `kill -9` drill, after its first heartbeat was seen (`watchdog: healthy pid 75030 (heartbeat 2s old)`) and checked again immediately before the freeze.

## Expected
The watchdog (`StartInterval` 30 s, `DEFAULT_MAX_AGE` 90 s) kills the frozen process within about 120 s and launchd starts another.

## Actual
The frozen pid was gone after 97 s and a new supervisor (pid 75960) was running at that check. `watchdog --dry-run` then said `healthy pid 75960 (heartbeat 2s old)`. Together with pass 1 (96 s, `2026-10-07-supervisor-freeze.md`) the freeze drill passed twice on the deployed fix.

## Follow-up
none
