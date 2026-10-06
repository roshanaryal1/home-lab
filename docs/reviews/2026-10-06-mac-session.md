# Mac mini session report

- started: 20261006T063400Z
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
- steps: context,home,caffeinate,signature,selftest,concurrency,drills

What each step proves, and which issue to paste it into: ops/mac-session.md.

> **Reading notes, added after the run (the transcript below is otherwise unedited).**
> 1. The hostname and the operator's home path were replaced with `<mac-mini>` and `/Users/<operator>`; nothing else was changed.
> 2. **Step 7, the freeze drill, did not recover.** The line `frozen 47635; gone after 182 s; new supervisor 47635 after 243 s` is the script's fixed wording, and it is misleading here: the process was never gone and never replaced. After 182 s the same pid 47635 was still there, the script resumed it with `kill -CONT`, and the later 'new supervisor' pid is that same process. The result line is FAIL, which is right. Tracked in #271.
> 3. **Step 5's heading** is the name of the check ('the nightly self-test ran and passed'). The result is FAIL: `safety_tests` fails every night because pytest is not installed in the deployed environment (#270).
> 4. Steps run: context, home, caffeinate, signature, selftest, concurrency, drills. Not run: backup, alert, skillrun, mcp and the two manual steps (reasons in PR #272).

## 1. What is deployed and how the lab is (#241)

Step `context`. Records the macOS version, the deployed commit and the lab's own status, so every result below is tied to a known build.

```text
$ sw_vers
ProductName:		macOS
ProductVersion:		27.0
BuildVersion:		26A428
[exit 0]
```
```text
$ sudo git -C /opt/homelab rev-parse HEAD
7bf235c4af82dbcb7fe2bca1713b4a9f374c0967
[exit 0]
```
```text
$ sudo -u lab "$PY" -m lab.cli --db "$DB" status
health   IDLE
mode     running
as of    2026-10-06T06:34:00+00:00   counters: all time

queue
  oldest queued           -
  oldest running          -
  oldest approval         -

worker
  live leases             0
  last success ago        -
  log last moved        16h ago

counters
  policy_denials             0
  approvals_rejected         0
  egress_denials             0
  lease_losses               0
  forced_terminations        0
  worker_errors              0
  tasks_tainted              0
  retries                    0
  recovered tasks            0

needs a person: 0 approval(s), 0 unresolved operation(s)
model: load time, peak memory and swap arrive with the adapter (5.1)
[exit 0]
```

**Result: PASS.** lab status exits 0

## 2. lab cannot read the operator's home folder (#225)

Step `home`. Lists the home folder and one folder below it as lab. Both must say Permission denied.

```text
$ sudo -u lab /bin/ls "$HOME"
Applications
Desktop
Documents
Downloads
Library
Movies
Music
Pictures
Public
Research and Development 
[exit 0]
```
```text
$ sudo -u lab /bin/ls "$HOME/Public"
Drop Box
[exit 0]
```

lab can read the home folder, or one listing did not say Permission denied.
- asked: lab can read /Users/<operator>. Run chmod 700 "$HOME" now (your own folder, no sudo)? [y/N]; answer: y
```text
$ chmod 700 "$HOME"
[exit 0]
```
```text
$ sudo -u lab /bin/ls "$HOME"
ls: /Users/<operator>: Permission denied
[exit 1]
```
```text
$ sudo -u lab /bin/ls "$HOME/Public"
ls: /Users/<operator>/Public: Permission denied
[exit 1]
```

**Result: PASS.** lab could read the home folder; after chmod 700 both listings say Permission denied

## 3. lab can hold the Mac awake (#235)

Step `caffeinate`. Starts caffeinate as lab for 15 seconds and looks for its PreventUserIdleSystemSleep assertion.

```text
$ sudo -u lab /usr/bin/caffeinate -i -t 15 >/dev/null 2>&1 &
[exit 0]
```
```text
$ sleep 2
[exit 0]
```
```text
$ pmset -g assertions | grep -i caffeinate
   pid 47244(caffeinate): [0x000ef9320001a06e] 00:00:02 PreventUserIdleSystemSleep named: "caffeinate command-line tool"  
	Details: caffeinate asserting for 15 secs
	Localized=THE CAFFEINATE TOOL IS PREVENTING SLEEP.
[exit 0]
```

**Result: PASS.** a caffeinate assertion with PreventUserIdleSystemSleep is held

## 4. An approval without the operator key is refused (#70)

Step `signature`. As lab, against a scratch database and the real public key, approves a request three ways without the operator key: unsigned through `lab.cli approve`, signed with a key lab made itself, and by writing the row directly. Each must be refused at the gate with an `approval_rejected` event. The live database is not opened.

The probe, fed to Python on standard input:

```python
import sys
import tempfile
from pathlib import Path

from lab import operator as op
from lab.cli import main
from lab.policy import Decision, PolicyEngine, Tier
from lab.queue import TaskQueue

public_key = op.load_public(Path(sys.argv[1]))
refused = 0
with tempfile.TemporaryDirectory(prefix="homelab-signature-") as tmp:
    db = Path(tmp) / "scratch.db"
    fake_key, _ = op.generate(Path(tmp) / "not-the-operator")

    def gate(task: str):
        with TaskQueue(db) as queue:
            queue._conn.execute("INSERT OR IGNORE INTO tasks (id, title) VALUES (?, ?)",
                                (task, task))
            policy = PolicyEngine(queue._conn, public_key)
            return policy.authorize_tool(task, "fs.delete", {"path": "probe"}, Tier.APPROVE)

    def rejections(task: str) -> int:
        with TaskQueue(db) as queue:
            return int(queue._conn.execute(
                "SELECT count(*) FROM events WHERE task_id = ? AND kind = 'approval_rejected'",
                (task,)).fetchone()[0])

    for case in ("unsigned", "fabricated-key", "direct-write"):
        approval = gate(case).approval_id
        if case == "unsigned":
            main(["--db", str(db), "approve", approval, "--by", "lab"])
        elif case == "fabricated-key":
            main(["--db", str(db), "approve", approval, "--by", "lab", "--key", str(fake_key)])
        else:
            with TaskQueue(db) as queue:
                queue._conn.execute(
                    "UPDATE approvals SET state = 'granted', decided_by = 'lab', "
                    "expires_at = strftime('%Y-%m-%d %H:%M:%f', 'now', '+1 hour') "
                    "WHERE id = ?", (approval,))
        decision = gate(case).decision
        count = rejections(case)
        ok = decision is Decision.NEEDS_APPROVAL and count > 0
        refused += ok
        print(f"{'REFUSED' if ok else 'ACCEPTED'} {case}: gate said {decision.value}, "
              f"{count} approval_rejected event(s)")
print("PASS" if refused == 3 else "FAIL", f"{refused} of 3 attempts refused")
sys.exit(0 if refused == 3 else 1)
```

```text
$ printf "%s\n" "$SIG_PROBE" | sudo -u lab "$PY" - "$PUBKEY"
warning: no operator key; this approval is UNSIGNED and a supervisor that enforces operator signatures will ignore it
Granting b5455e7b7849: unsigned [hash acf58b866476dec1]
Granted b5455e7b7849 for 15 minutes, label 'lab', UNSIGNED
No parked task released; the approval is stored and will be consumed when the task reaches the gate.
REFUSED unsigned: gate said needs_approval, 1 approval_rejected event(s)
Granting 0d9e2601df0c: fabricated-key [hash 6645e6d2fd6b755c]
Granted 0d9e2601df0c for 15 minutes, label 'lab', signed
No parked task released; the approval is stored and will be consumed when the task reaches the gate.
REFUSED fabricated-key: gate said needs_approval, 1 approval_rejected event(s)
REFUSED direct-write: gate said needs_approval, 1 approval_rejected event(s)
PASS 3 of 3 attempts refused
[exit 0]
```

**Result: PASS.** all three approvals made without the operator key were refused

## 5. The nightly self-test ran and passed (#80) [check name; this run FAILED, see the result below]

Step `selftest`. Reads the nightly self-test log. It must have been written in the last 26 hours and its recent lines must hold no FAIL.

```text
$ sudo -u lab /usr/bin/find "$LOG_DIR/selftest.log" -mmin -1560
/var/log/homelab/selftest.log
[exit 0]
```
```text
$ sudo -u lab /usr/bin/tail -n 30 "$LOG_DIR/selftest.log"
ok   audit_chain     0 events verify
ok   backup_restore  0 events, 0 artifacts restored
ok   health          idle
FAIL safety_tests    
ok   audit_chain     1 events verify
ok   backup_restore  1 events, 0 artifacts restored
ok   health          idle
FAIL safety_tests    
ok   audit_chain     2 events verify
ok   backup_restore  2 events, 0 artifacts restored
ok   health          idle
FAIL safety_tests    
ok   audit_chain     3 events verify
ok   backup_restore  3 events, 0 artifacts restored
ok   health          idle
FAIL safety_tests    
ok   audit_chain     4 events verify
ok   backup_restore  4 events, 0 artifacts restored
ok   health          idle
FAIL safety_tests    
ok   audit_chain     5 events verify
ok   backup_restore  5 events, 0 artifacts restored
ok   health          idle
FAIL safety_tests    
[exit 0]
```
```text
$ sudo -u lab /usr/bin/tail -n 10 "$LOG_DIR/selftest.err"
alert: sent
alert: sent
alert: sent
alert: sent
alert: sent
alert: sent
[exit 0]
```

Still manual for #80: a result, including ok, should reach the phone every morning. Check the phone for this morning's message.

**Result: FAIL.** the self-test log has a FAIL line; read the tail above

## 6. Two model requests at once (#211)

Step `concurrency`. Sends two chat completions to the loopback model server at the same moment and records each one's wall time, and memory and swap before, during and after. `now` prints the Unix time with milliseconds.

```text
$ MODEL_ID=$(curl -sf --max-time 10 "$MODEL_URL/models" | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"][0]["id"])')
[exit 0]
```
Model: `/Users/<operator>/.cache/huggingface/hub/models--mlx-community--Qwen3-Coder-30B-A3B-Instruct-4bit-DWQ/snapshots/cfcade7221ccd128681961446e5f7906c08cae55`
```text
$ BODY=$(python3 -c 'import json,sys;print(json.dumps({"model": sys.argv[1], "messages": [{"role": "user", "content": "Count from 1 to 150, separated by spaces."}], "max_tokens": 400, "temperature": 0}))' "$MODEL_ID")
[exit 0]
```
```text
$ sysctl vm.swapusage; top -l 1 -n 0 | grep PhysMem; vm_stat
vm.swapusage: total = 10240.00M  used = 9689.94M  free = 550.06M  (encrypted)
PhysMem: 31G used (2680M wired, 15G compressor), 245M unused.
Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                    10069.
Pages active:                                 432859.
Pages inactive:                               426177.
Pages speculative:                              5899.
Pages throttled:                                   0.
Pages wired down:                             171481.
Pages purgeable:                               11278.
"Translation faults":                     1935466161.
Pages copy-on-write:                        84466299.
Pages zero filled:                        1166655512.
Pages reactivated:                          14752750.
Pages purged:                               21102159.
File-backed pages:                            398262.
Anonymous pages:                              466673.
Pages stored in compressor:                  1108263.
Pages occupied by compressor:                1013366.
Decompressions:                             18805352.
Compressions:                               34308412.
Pageins:                                    77347900.
Pageouts:                                     115304.
Swapins:                                     1024615.
Swapouts:                                    3028624.
Pages tagged:                                 143783.
Pages tagged resident:                        127607.
Pages tagged compressed:                       16176.
Pages tag-storage:                             65536.
Pages tag-storage holding tags:                 6928.
Pages tag-storage free:                           72.
Pages tag-storage non-tag pageable:            58402.
Pages tag-storage non-tag wired:                   0.
Bytes of compressed tags:                    2175040.
Tagged compressions:                         1068868.
Tagged decompressions:                        888026.
[exit 0]
```
```text
$ touch "$WORK/sampling"; ( n=0; while [ -e "$WORK/sampling" ] && [ "$n" -lt 900 ]; do n=$((n + 1)); echo "sample at $(now)"; sysctl vm.swapusage; top -l 1 -n 0 | grep PhysMem; sleep 1; done ) >"$WORK/samples.txt" 2>&1 & SAMPLER=$!
[exit 0]
```
```text
$ ( s=$(now); code=$(curl -s --max-time 600 -o "$WORK/reply1.json" -w '%{http_code}' -H 'Content-Type: application/json' -d "$BODY" "$MODEL_URL/chat/completions"); echo "request 1 start $s end $(now) http $code" ) & P1=$!
( s=$(now); code=$(curl -s --max-time 600 -o "$WORK/reply2.json" -w '%{http_code}' -H 'Content-Type: application/json' -d "$BODY" "$MODEL_URL/chat/completions"); echo "request 2 start $s end $(now) http $code" ) & P2=$!
wait "$P1" "$P2"
request 1 start 1791268446.845 end 1791268483.438 http 200
request 2 start 1791268446.846 end 1791268483.438 http 200
[exit 0]
```
```text
$ rm -f "$WORK/sampling"; wait "$SAMPLER"; SAMPLER=""; grep -c "^sample at" "$WORK/samples.txt"
31
[exit 0]
```
```text
$ sysctl vm.swapusage; top -l 1 -n 0 | grep PhysMem; vm_stat
vm.swapusage: total = 2048.00M  used = 554.62M  free = 1493.38M  (encrypted)
PhysMem: 25G used (20G wired, 2686M compressor), 6390M unused.
Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                   407390.
Pages active:                                  88362.
Pages inactive:                                85629.
Pages speculative:                              1830.
Pages throttled:                                   0.
Pages wired down:                            1304728.
Pages purgeable:                                1863.
"Translation faults":                     1935710579.
Pages copy-on-write:                        84493098.
Pages zero filled:                        1166852386.
Pages reactivated:                          14922144.
Pages purged:                               21125142.
File-backed pages:                             92351.
Anonymous pages:                               83470.
Pages stored in compressor:                   405057.
Pages occupied by compressor:                 171912.
Decompressions:                             20362865.
Compressions:                               34611211.
Pageins:                                    77352689.
Pageouts:                                     115388.
Swapins:                                     1725059.
Swapouts:                                    3171828.
Pages tagged:                                 130099.
Pages tagged resident:                         79496.
Pages tagged compressed:                       50603.
Pages tag-storage:                             65536.
Pages tag-storage holding tags:                 5436.
Pages tag-storage free:                        17219.
Pages tag-storage non-tag pageable:            41499.
Pages tag-storage non-tag wired:                   3.
Bytes of compressed tags:                    8439936.
Tagged compressions:                         1107598.
Tagged decompressions:                        892319.
[exit 0]
```

- requests: http 200 and 200; wall 36.59 s and 36.59 s; overlapped yes; longer/shorter 1.00
- peak PhysMem used during: 31744 MB
- swap used: before 9689.9 MB, peak during 9689.9 MB, after 554.6 MB

**Result: PASS.** http 200 and 200; wall 36.59 s and 36.59 s; overlapped yes; longer/shorter 1.00; peak memory 31744 MB; swap 9689.9 MB before, 9689.9 MB peak, 554.6 MB after

## 7. Timed kill and freeze drills (#78)

Step `drills`. The two timed drills of runbook step 6, with its commands. A kill -9 must bring a new supervisor within about 30 seconds; a frozen supervisor must be gone and replaced within 120 seconds, and the watchdog must then say healthy.

- asked: These drills kill the supervisor twice. Work running now is interrupted. Check that the queue is idle. Run them? [y/N]; answer: y
```text
$ sudo -v
[exit 0]
```
```text
$ pgrep -f lab.supervisor
46080
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
killed 46080; new supervisor 47635 after 0 s
[exit 0]
```
```text
$ sudo -v
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
FAIL: 47635 is still there after 182 s; resuming it
[exit 0]
```
```text
$ for i in $(seq 1 30); do NEW=$(pgrep -f lab.supervisor) && [ "$NEW" != "$OLD" ] && break; sleep 2; done
[exit 0]
```
```text
$ echo "frozen $OLD; gone after $((T1 - T0)) s; new supervisor ${NEW:-none} after $(( $(date +%s) - T0 )) s"
frozen 47635; gone after 182 s; new supervisor 47635 after 243 s
[exit 0]
```
```text
$ sudo -u lab "$PY" -m lab.cli --db "$DB" watchdog --dry-run
watchdog: healthy pid 47635 (heartbeat 1s old)
[exit 0]
```

Record both in `ops/drills/log/` with `ops/drills/TEMPLATE.md` (LAB_TARGET=mac-mini).

**Result: FAIL.** killed 46080; new supervisor 47635 after 0 s. frozen 47635; gone after 182 s; new supervisor 47635 after 243 s. watchdog healthy: yes

## Summary

| # | Step | Issues | Result | Note |
|---|---|---|---|---|
| 1 | What is deployed and how the lab is | #241 | PASS | lab status exits 0 |
| 2 | lab cannot read the operator's home folder | #225 | PASS | lab could read the home folder; after chmod 700 both listings say Permission denied |
| 3 | lab can hold the Mac awake | #235 | PASS | a caffeinate assertion with PreventUserIdleSystemSleep is held |
| 4 | An approval without the operator key is refused | #70 | PASS | all three approvals made without the operator key were refused |
| 5 | The nightly self-test ran and passed | #80 | FAIL | the self-test log has a FAIL line; read the tail above |
| 6 | Two model requests at once | #211 | PASS | http 200 and 200; wall 36.59 s and 36.59 s; overlapped yes; longer/shorter 1.00; peak memory 31744 MB; swap 9689.9 MB before, 9689.9 MB peak, 554.6 MB after |
| 7 | Timed kill and freeze drills | #78 | FAIL | killed 46080; new supervisor 47635 after 0 s. frozen 47635; gone after 182 s; new supervisor 47635 after 243 s. watchdog healthy: yes |
