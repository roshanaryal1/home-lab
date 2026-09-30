# Drill: frozen supervisor replaced by the watchdog

- date: 2026-09-30, about 01:01 UTC (the commands did not print timestamps; the restore drill that followed is stamped 01:06:36 UTC)
- machine: macOS 27.0 (26A428), Darwin 27.0.0 arm64, python 3.13.15, sqlite 3.53.1, deployed commit bf7fda69
- target: mac-mini
- result: INCOMPLETE
- counts as demonstrated: no

## Failure injected
`sudo kill -STOP` on the running supervisor (pid 32017): the process stays
alive, so launchd's `KeepAlive` alone would not notice.

## Expected
The `com.homelab.watchdog` job (every 30 s) should replace the frozen
supervisor within two minutes. A check at or before 120 seconds is required
to demonstrate that target.

## Actual
The first recorded check was at 150 seconds: the frozen pid 32017 no longer
existed and a new supervisor ran as pid 33939. `lab watchdog --dry-run` then
printed `healthy pid 33939 (heartbeat 6s old)`. This demonstrates recovery by
the 150-second check, but does not establish recovery by 120 seconds.

## Follow-up
none
