# Mac mini session report

- started: 20261006T125340Z
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
> 2. This is **pass 2 of 2** of the timed drills for #78 and #271 on deployed commit `4bfa910` (pass 1: `docs/reviews/2026-10-07-mac-session-drills-pass-1.md`). The kill drill killed pid 66453; launchd started 75030; the script waited until the watchdog reported `healthy pid 75030`, checked it again, and froze exactly that pid (`VERIFIED` held 75030, as in the result line `frozen 75030`). The watchdog replaced it with 75960 after **97 s**, under the 120 s target.
> 3. As in pass 1, the restart after `kill -9` shows 0 s because the first poll ran before the 2 s interval; it is an upper bound under the poll interval.
> 4. Nothing was run besides the drills (`--only drills`).

## 1. Timed kill and freeze drills (#78)

Step `drills`. The two timed drills of runbook step 6, with its commands. A kill -9 must bring a new supervisor within about 30 seconds; a frozen supervisor must be gone and replaced within 120 seconds, and the watchdog must then say healthy.

- asked: These drills kill the supervisor twice. Work running now is interrupted. Check that the queue is idle. Run them? [y/N]; answer: y
```text
$ sudo -v
[exit 0]
```
```text
$ pgrep -f lab.supervisor
66453
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
killed 66453; new supervisor 75030 after 0 s
[exit 0]
```
```text
$ for i in $(seq 1 30); do sudo -u lab "$PY" -m lab.cli --db "$DB" watchdog --dry-run 2>&1 | grep -q "healthy pid $NEW " && break; sleep 2; done
[exit 0]
```
```text
$ sudo -u lab "$PY" -m lab.cli --db "$DB" watchdog --dry-run
watchdog: healthy pid 75030 (heartbeat 2s old)
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
watchdog: healthy pid 75030 (heartbeat 2s old)
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
frozen 75030; gone after 97 s; new supervisor 75960 after 97 s
[exit 0]
```
```text
$ sudo -u lab "$PY" -m lab.cli --db "$DB" watchdog --dry-run
watchdog: healthy pid 75960 (heartbeat 2s old)
[exit 0]
```

Record both in `ops/drills/log/` with `ops/drills/TEMPLATE.md` (LAB_TARGET=mac-mini).

**Result: PASS.** killed 66453; new supervisor 75030 after 0 s. frozen 75030; gone after 97 s; new supervisor 75960 after 97 s. watchdog healthy

## Summary

| # | Step | Issues | Result | Note |
|---|---|---|---|---|
| 1 | Timed kill and freeze drills | #78 | PASS | killed 66453; new supervisor 75030 after 0 s. frozen 75030; gone after 97 s; new supervisor 75960 after 97 s. watchdog healthy |
