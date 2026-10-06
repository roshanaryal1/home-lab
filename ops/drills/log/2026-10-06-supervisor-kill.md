# Drill: supervisor kill under launchd

- date: 2026-10-06, after 06:34:43 UTC (the session script did not timestamp the drills; the report is `docs/reviews/2026-10-06-mac-session.md`)
- machine: macOS 27.0 (26A428) arm64, deployed commit 7bf235c
- target: mac-mini
- result: PASS
- counts as demonstrated: yes

## Failure injected
`sudo kill -9` on the running supervisor (pid 46080), started by the `com.homelab.supervisor` LaunchDaemon as `lab`.

## Expected
launchd starts a new supervisor within its throttle interval, about 30 s.

## Actual
A new supervisor (pid 47635) was already running at the first check, 0 s after the kill (the loop polls every 2 s and the first poll ran at once). The exact restart time is therefore under 2 s plus the poll, not measured more finely.

## Follow-up
none
