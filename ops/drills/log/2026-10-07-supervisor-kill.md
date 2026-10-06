# Drill: supervisor kill under launchd

- date: 2026-10-06, 12:10 UTC onward (the session script did not timestamp each drill; the report is `docs/reviews/2026-10-07-mac-session-drills-pass-1.md`)
- machine: macOS 27.0 (26A428) arm64, deployed commit 4bfa910
- target: mac-mini
- result: PASS
- counts as demonstrated: yes (pass 1 of 2 for the freeze drill; a second pass is required before #78 and #271 close)

## Failure injected
`sudo kill -9` on the running supervisor (pid 53081), started by the `com.homelab.supervisor` LaunchDaemon as `lab`.

## Expected
launchd starts a new supervisor within its throttle interval, about 30 s.

## Actual
A new supervisor (pid 66280) was running at the first check, 0 s after the kill (under the 2 s poll). Then its first heartbeat appeared: `watchdog: healthy pid 66280 (heartbeat 2s old)`.

## Follow-up
none
