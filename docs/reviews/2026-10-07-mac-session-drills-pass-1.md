# Mac mini session report

- started: 20261006T121012Z
- host: <mac-mini>
- mode: live
- REPO=/Users/<operator>/Research and Development /home-lab
- DB=/var/homelab/lab.db
- PY=/opt/homelab/.venv/bin/python
- MODEL_URL=http://127.0.0.1:8080/v1
- BACKUP_VOLUME=/Volumes/labbackup
- PUBKEY=/etc/homelab/operator.pub
- ALERT_CONFIG=/etc/homelab/alert.json
- LOG_DIR=/var/log/homelab
- LAB_CONTAINER_IMAGE=not set
- MCP_CONFIG=/etc/homelab/mcp.json
- steps: drills

What each step proves, and which issue to paste it into: ops/mac-session.md.

> **Reading notes, added after the run (the transcript below is otherwise unedited).**
> 1. The hostname and the operator's home path were replaced with `<mac-mini>` and `/Users/<operator>`; nothing else was changed.
> 2. This is **pass 1 of 2** of the timed drills for #78 and #271, run on deployed commit `4bfa910` (the watchdog fallback and the drill that waits for the first heartbeat). The first drill killed pid 53081; launchd started 66280; the script waited until the watchdog reported `healthy pid 66280`, checked it again, froze exactly that pid, and the watchdog replaced it with 66453 after **96 s**, under the 120 s target. The earlier failure (`docs/reviews/2026-10-06-mac-session.md`) was a supervisor frozen before its first heartbeat.
> 3. The `kill -9` line says the new supervisor appeared after 0 s: the first poll ran immediately and launchd had already restarted it, so the restart time is under the 2 s poll interval, not measured more finely.
> 4. Nothing was run here besides the drills (`--only drills`).

## 1. Timed kill and freeze drills (#78)

Step `drills`. The two timed drills of runbook step 6, with its commands. A kill -9 must bring a new supervisor within about 30 seconds; a frozen supervisor must be gone and replaced within 120 seconds, and the watchdog must then say healthy.

- asked: These drills kill the supervisor twice. Work running now is interrupted. Check that the queue is idle. Run them? [y/N]; answer: y
```text
$ sudo -v
[exit 0]
```
```text
$ pgrep -f lab.supervisor
53081
[exit 0]
```
```text
$ OLD=$(pgrep -f lab.supervisor)
[exit 0]
```
```text
$ T0=$(date +%s)
[exit 0]
```
```text
$ sudo kill -9 "$OLD"
[exit 0]
```
```text
$ for i in $(seq 1 45); do NEW=$(pgrep -f lab.supervisor) && [ "$NEW" != "$OLD" ] && break; sleep 2; done
[exit 0]
```
```text
$ echo "killed $OLD; new supervisor ${NEW:-none} after $(( $(date +%s) - T0 )) s"
killed 53081; new supervisor 66280 after 0 s
[exit 0]
```
```text
$ for i in $(seq 1 30); do sudo -u lab "$PY" -m lab.cli --db "$DB" watchdog --dry-run 2>&1 | grep -q "healthy pid $NEW " && break; sleep 2; done
[exit 0]
```
```text
$ sudo -u lab "$PY" -m lab.cli --db "$DB" watchdog --dry-run
watchdog: healthy pid 66280 (heartbeat 2s old)
[exit 0]
```
```text
$ sudo -v
[exit 0]
```
```text
$ OLD=$VERIFIED
[exit 0]
```
```text
$ sudo -u lab "$PY" -m lab.cli --db "$DB" watchdog --dry-run
watchdog: healthy pid 66280 (heartbeat 2s old)
[exit 0]
```
```text
$ T0=$(date +%s)
[exit 0]
```
```text
$ sudo kill -STOP "$OLD"
[exit 0]
```
```text
$ for i in $(seq 1 90); do ps -p "$OLD" >/dev/null || break; sleep 2; done
[exit 0]
```
```text
$ T1=$(date +%s)
[exit 0]
```
```text
$ ps -p "$OLD" >/dev/null && { echo "FAIL: $OLD is still there after $((T1 - T0)) s; resuming it"; sudo kill -CONT "$OLD"; }
[exit 1]
```
```text
$ for i in $(seq 1 30); do NEW=$(pgrep -f lab.supervisor) && [ "$NEW" != "$OLD" ] && break; sleep 2; done
[exit 0]
```
```text
$ if ps -p "$OLD" >/dev/null; then G="STILL THERE after $((T1 - T0)) s"; else G="gone after $((T1 - T0)) s"; fi; echo "frozen $OLD; $G; new supervisor ${NEW:-none} after $(( $(date +%s) - T0 )) s"
frozen 66280; gone after 96 s; new supervisor 66453 after 96 s
[exit 0]
```
```text
$ sudo -u lab "$PY" -m lab.cli --db "$DB" watchdog --dry-run
watchdog: healthy pid 66453 (heartbeat 2s old)
[exit 0]
```

Record both in `ops/drills/log/` with `ops/drills/TEMPLATE.md` (LAB_TARGET=mac-mini).

**Result: PASS.** killed 53081; new supervisor 66280 after 0 s. frozen 66280; gone after 96 s; new supervisor 66453 after 96 s. watchdog healthy

## Summary

| # | Step | Issues | Result | Note |
|---|---|---|---|---|
| 1 | Timed kill and freeze drills | #78 | PASS | killed 53081; new supervisor 66280 after 0 s. frozen 66280; gone after 96 s; new supervisor 66453 after 96 s. watchdog healthy |
