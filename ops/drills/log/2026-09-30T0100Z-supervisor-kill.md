# Drill: supervisor kill under launchd

- date: 2026-09-30, about 01:00 UTC (the commands did not print timestamps; the restore drill that followed is stamped 01:06:36 UTC)
- machine: macOS 27.0 (26A428), Darwin 27.0.0 arm64, python 3.13.15, sqlite 3.53.1, deployed commit bf7fda69
- target: mac-mini
- result: PASS
- counts as demonstrated: yes

## Failure injected
`sudo kill -9` on the running supervisor (pid 31101), started by the
`com.homelab.supervisor` LaunchDaemon as the `lab` account.

## Expected
launchd should restart the supervisor after the process exits. `ThrottleInterval`
configures launch-rate throttling; it is not a guaranteed restart deadline.

## Actual
A new supervisor was observed 40 seconds later as pid 32017. The queue was
empty, so no task needed recovery; requeue after a crash was shown on the
mini by the 2026-09-29 crash drills.

## Follow-up
none
