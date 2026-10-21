#!/bin/bash
# Each command is kept in single quotes on purpose: `run` prints it as written,
# then evaluates it, so the report shows "$HOME" and not one machine's path.
# Variables set or read only inside those commands look unused to shellcheck.
# shellcheck disable=SC2016,SC2034
#
# One sitting at the Mac mini (#241): run every open machine check in order and
# write one Markdown report. Read ops/mac-session.md before the first run.
#
# Nothing on the machine is changed unless a step says so and the operator
# answers y at its prompt. Every prompt defaults to no. One failed check does
# not stop the others, so there is deliberately no `set -e`.

set -u

usage() {
  cat <<'EOF'
usage: ops/mac-session.sh [--dry-run] [--only STEP[,STEP...]] [--report PATH]

  --dry-run      print every command and prompt, run nothing
  --only STEP    run only these steps (comma separated)
  --report PATH  where to write the report
                 (default ./mac-session-<UTC timestamp>.md)

Steps, in order:
  context      deployed commit, macOS version and lab status (#241)
  home         lab cannot read the operator's home folder (#225)
  caffeinate   lab can hold the Mac awake with caffeinate (#235)
  signature    an approval made without the operator key is refused (#70)
  backup       backup to the backup volume and a restore check (#67)
  alert        a test alert reaches the phone (#79, #80)
  selftest     the nightly self-test ran and passed (#80)
  skillrun     a skill script runs only in a real container (#255)
  mcp          signed MCP servers start under Seatbelt and match their snapshot (#256)
  concurrency  two model requests at once: timing, memory, swap (#211)
  drills       timed kill and freeze drills of the supervisor (#78)
  network      network-unplug test of the dead-man switch, manual (#79)
  power        power-pull drill, manual (#77, #91)

Environment, with defaults:
  REPO=$HOME/home-lab
  DB=/var/homelab/lab.db
  PY=/opt/homelab/.venv/bin/python
  MODEL_URL=http://127.0.0.1:8080/v1
  BACKUP_VOLUME=/Volumes/labbackup
  LAB_CONTAINER_IMAGE=(not set, the skillrun step needs an image pinned by digest)
  MCP_CONFIG=/etc/homelab/mcp.json
EOF
}

REPO="${REPO:-$HOME/home-lab}"
DB="${DB:-/var/homelab/lab.db}"
PY="${PY:-/opt/homelab/.venv/bin/python}"
MODEL_URL="${MODEL_URL:-http://127.0.0.1:8080/v1}"
BACKUP_VOLUME="${BACKUP_VOLUME:-/Volumes/labbackup}"
CONTAINER_IMAGE="${LAB_CONTAINER_IMAGE:-}"
MCP_CONFIG="${MCP_CONFIG:-/etc/homelab/mcp.json}"
PUBKEY=/etc/homelab/operator.pub
ALERT_CONFIG=/etc/homelab/alert.json
LOG_DIR=/var/log/homelab

ALL_STEPS="context home caffeinate signature backup alert selftest skillrun mcp concurrency drills network power"

DRY_RUN=0
ONLY=""
REPORT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --only)
      [ $# -ge 2 ] || { usage >&2; exit 2; }
      ONLY="$2"; shift ;;
    --report)
      [ $# -ge 2 ] || { usage >&2; exit 2; }
      REPORT="$2"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'unknown argument: %s\n\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

for wanted in $(printf '%s' "$ONLY" | tr ',' ' '); do
  case " $ALL_STEPS " in
    *" $wanted "*) ;;
    *) printf 'unknown step: %s (one of: %s)\n' "$wanted" "$ALL_STEPS" >&2; exit 2 ;;
  esac
done

STARTED="$(date -u +%Y%m%dT%H%M%SZ)"
[ -n "$REPORT" ] || REPORT="./mac-session-$STARTED.md"
case "$REPORT" in
  /*) ;;
  *) REPORT="$PWD/$REPORT" ;;
esac
if ! : >"$REPORT"; then
  printf 'cannot write the report at %s\n' "$REPORT" >&2
  exit 2
fi

# The lab account cannot enter the operator's home folder once it is 700, and
# commands run as lab inherit the current directory.
cd / || exit 2

WORK=""
SAMPLER=""
if [ "$DRY_RUN" = 0 ]; then
  WORK="$(mktemp -d "${TMPDIR:-/tmp}/mac-session.XXXXXX")" || exit 2
fi
OUT_FILE="${WORK:-/dev/null}/out"

cleanup() {
  [ -n "$WORK" ] && rm -f "$WORK/sampling"
  [ -n "$SAMPLER" ] && wait "$SAMPLER" 2>/dev/null
  [ -n "$WORK" ] && rm -rf "$WORK"
}
trap cleanup EXIT

# ------------------------------------------------------------------ helpers

dry() { [ "$DRY_RUN" = 1 ]; }
say() { printf '%s\n' "$*"; }
rep() { printf '%s\n' "$*" >>"$REPORT"; }
both() { say "$*"; rep "$*"; }

# The Unix time with milliseconds. macOS `date` has no sub-second format.
now() { python3 -c 'import time; print(f"{time.time():.3f}")'; }

LAST_OUT=""
LAST_RC=0

# Run one command line, show it and its output, and copy both to the report.
# It runs in this shell, so a variable it sets is visible to the next command.
# Standard input is closed so a command can never eat the answer to a prompt.
run() {
  local cmd="$1"
  say "\$ $cmd"
  rep '```text'
  rep "\$ $cmd"
  if dry; then
    rep '(dry run: not run)'
    rep '```'
    LAST_OUT=""
    LAST_RC=0
    return 0
  fi
  eval "$cmd" </dev/null >"$OUT_FILE" 2>&1
  LAST_RC=$?
  LAST_OUT="$(cat "$OUT_FILE")"
  [ -n "$LAST_OUT" ] && say "$LAST_OUT"
  [ -n "$LAST_OUT" ] && rep "$LAST_OUT"
  rep "[exit $LAST_RC]"
  rep '```'
  return "$LAST_RC"
}

# A y/N question. Anything but y or yes, including no answer at all, is no.
ask() {
  local question="$1" answer=""
  if dry; then
    say "(dry run) would ask: $question [y/N]"
    rep "- would ask: $question [y/N]"
    return 0
  fi
  printf '%s [y/N] ' "$question"
  read -r answer || answer=""
  rep "- asked: $question [y/N]; answer: ${answer:-none, so no}"
  case "$answer" in
    y|Y|yes|Yes|YES) return 0 ;;
    *) return 1 ;;
  esac
}

STEP_N=0
CUR_TITLE=""
CUR_ISSUES=""
SUMMARY=""
FAILED=0

begin() {
  STEP_N=$((STEP_N + 1))
  CUR_TITLE="$2"
  CUR_ISSUES="$3"
  say ""
  say "=== $STEP_N. $CUR_TITLE ($CUR_ISSUES) ==="
  rep ""
  rep "## $STEP_N. $CUR_TITLE ($CUR_ISSUES)"
  rep ""
  rep "Step \`$1\`. $4"
  rep ""
}

finish() {
  local status="$1" note="$2"
  if dry && [ "$status" != MANUAL ]; then
    status=SKIPPED
    note="dry run, nothing was run"
  fi
  [ "$status" = FAIL ] && FAILED=$((FAILED + 1))
  rep ""
  rep "**Result: $status.** $note"
  say "--> $status: $note"
  note="$(printf '%s' "$note" | tr '|\n' '/ ')"
  SUMMARY="$SUMMARY| $STEP_N | $CUR_TITLE | $CUR_ISSUES | $status | $note |
"
}

has() { printf '%s\n' "$LAST_OUT" | grep -q -- "$1"; }

# Largest "used" value, in MB, from `top` PhysMem lines and `sysctl vm.swapusage`.
peak_physmem_mb() {
  awk '/PhysMem:/ { v = $2; u = substr(v, length(v)); n = substr(v, 1, length(v) - 1) + 0;
         if (u == "G") n *= 1024; else if (u == "T") n *= 1048576; else if (u == "K") n /= 1024;
         if (n > m) m = n }
       END { if (m == "") print "unknown"; else printf "%.0f MB\n", m }' "$@"
}
peak_swap_mb() {
  awk '/vm.swapusage/ { for (i = 1; i <= NF; i++) if ($i == "used") {
         v = $(i + 2); u = substr(v, length(v)); n = substr(v, 1, length(v) - 1) + 0;
         if (u == "G") n *= 1024; if (n > m) m = n } }
       END { if (m == "") print "unknown"; else printf "%.1f MB\n", m }' "$@"
}

# Read a here-document into the variable named $1, without its last newline.
# A here-document inside "$(...)" trips the parser of the bash 3.2 that macOS
# ships when the text holds quotes or parentheses; this form does not.
heredoc() {
  local text=""
  IFS= read -r -d '' text
  printf -v "$1" '%s' "${text%$'\n'}"
}

# The fabricated-signature probe for #70. It runs as lab, against a scratch
# database in a temporary folder, with the real operator public key. It never
# opens the live database.
heredoc SIG_PROBE <<'PY'
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
PY

ALERT_PY='import sys; from lab import alert; c = alert.load(sys.argv[1]); sys.exit(0 if alert.send(c, kind="test", message="home-lab test alert from the Mac mini session (#79)") else 1)'

# -------------------------------------------------------------------- steps

step_context() {
  begin context "What is deployed and how the lab is" "#241" \
    "Records the macOS version, the deployed commit and the lab's own status, so every \
result below is tied to a known build."
  run 'sw_vers'
  run 'sudo git -C /opt/homelab rev-parse HEAD'
  run 'sudo -u lab "$PY" -m lab.cli --db "$DB" status'
  if [ "$LAST_RC" = 0 ]; then
    finish PASS "lab status exits 0"
  else
    finish FAIL "lab status exited $LAST_RC; read its reasons above"
  fi
}

home_denied() {
  local denied=0
  run 'sudo -u lab /bin/ls "$HOME"'
  [ "$LAST_RC" != 0 ] && has 'Permission denied' && denied=$((denied + 1))
  run 'sudo -u lab /bin/ls "$HOME/Public"'
  [ "$LAST_RC" != 0 ] && has 'Permission denied' && denied=$((denied + 1))
  [ "$denied" = 2 ]
}

step_home() {
  begin home "lab cannot read the operator's home folder" "#225" \
    "Lists the home folder and one folder below it as lab. Both must say Permission denied."
  if home_denied && ! dry; then
    finish PASS "lab cannot read the home folder: both listings say Permission denied"
    return
  fi
  rep ""
  rep "lab can read the home folder, or one listing did not say Permission denied."
  if ask "lab can read $HOME. Run chmod 700 \"\$HOME\" now (your own folder, no sudo)?"; then
    run 'chmod 700 "$HOME"'
    if home_denied; then
      finish PASS "lab could read the home folder; after chmod 700 both listings say Permission denied"
    else
      finish FAIL "lab can still read the home folder after chmod 700"
    fi
  else
    finish FAIL "lab can read the home folder; chmod 700 was not run"
  fi
}

# Only an assertion held by a caffeinate that runs as lab counts: the root
# keep-awake daemon starts its own caffeinate while work is pending, and other
# sessions may run theirs, so any caffeinate line would pass without lab.
step_caffeinate() {
  begin caffeinate "lab can hold the Mac awake" "#235" \
    "Starts caffeinate as lab for 15 seconds and looks for a PreventUserIdleSystemSleep \
assertion held by a caffeinate process that runs as lab. Assertions of other accounts' \
caffeinate processes do not count."
  run 'sudo -u lab /usr/bin/caffeinate -i -t 15 >/dev/null 2>&1 &'
  run 'sleep 2'
  run 'LAB_PIDS=$(pgrep -d " " -u lab -x caffeinate); echo "caffeinate running as lab: ${LAB_PIDS:-none}"'
  run 'pmset -g assertions | grep -i caffeinate'
  local pid held=""
  for pid in ${LAB_PIDS:-}; do
    printf '%s\n' "$LAST_OUT" | grep -q "pid $pid(caffeinate):.*PreventUserIdleSystemSleep" \
      && held="$pid"
  done
  if [ -n "$held" ]; then
    finish PASS "caffeinate pid $held runs as lab and holds PreventUserIdleSystemSleep"
  elif has PreventUserIdleSystemSleep; then
    finish FAIL "PreventUserIdleSystemSleep is held, but not by a caffeinate that runs as lab"
  else
    finish FAIL "no caffeinate line with PreventUserIdleSystemSleep in pmset -g assertions"
  fi
}

step_signature() {
  begin signature "An approval without the operator key is refused" "#70" \
    "As lab, against a scratch database and the real public key, approves a request three \
ways without the operator key: unsigned through \`lab.cli approve\`, signed with a key lab \
made itself, and by writing the row directly. Each must be refused at the gate with an \
\`approval_rejected\` event. The live database is not opened."
  rep "The probe, fed to Python on standard input:"
  rep ""
  rep '```python'
  rep "$SIG_PROBE"
  rep '```'
  rep ""
  run 'printf "%s\n" "$SIG_PROBE" | sudo -u lab "$PY" - "$PUBKEY"'
  if [ "$LAST_RC" = 0 ] && has '^PASS'; then
    finish PASS "all three approvals made without the operator key were refused"
  else
    finish FAIL "an approval made without the operator key was not refused, or the probe failed"
  fi
}

step_backup() {
  begin backup "Backup to the backup volume and a restore check" "#67" \
    "Writes a backup of the live database to the backup volume as lab, restores it into a \
temporary folder and checks its integrity, then removes the temporary folder."
  if ! ask "Write a new backup of $DB to $BACKUP_VOLUME/home-lab-backups?"; then
    finish SKIPPED "the operator chose not to write a backup"
    return
  fi
  MANIFEST=""
  RESTORE_DIR=""
  run 'sudo -u lab env LAB_TARGET=mac-mini "$PY" -m lab.cli --db "$DB" backup --to "$BACKUP_VOLUME/home-lab-backups"'
  MANIFEST="$(printf '%s\n' "$LAST_OUT" | sed -n 's/^wrote //p' | tail -n 1)"
  if [ -z "$MANIFEST" ] && ! dry; then
    finish FAIL "the backup did not write a manifest (can lab write to $BACKUP_VOLUME/home-lab-backups?)"
    return
  fi
  run 'RESTORE_DIR=$(sudo -u lab /usr/bin/mktemp -d /tmp/homelab-restore.XXXXXX)'
  run 'sudo -u lab "$PY" -m lab.cli --db "$DB" restore-check "$MANIFEST" --into "$RESTORE_DIR/restore"'
  local restore_rc="$LAST_RC" restore_out="$LAST_OUT"
  case "$RESTORE_DIR" in
    /tmp/homelab-restore.*) run 'sudo -u lab /bin/rm -rf "$RESTORE_DIR"' ;;
    *) dry && run 'sudo -u lab /bin/rm -rf "$RESTORE_DIR"' ;;
  esac
  if [ "$restore_rc" = 0 ] && printf '%s\n' "$restore_out" | grep -q '^ok:'; then
    finish PASS "backup $MANIFEST restored and verified: $(printf '%s\n' "$restore_out" | grep '^ok:')"
  else
    finish FAIL "the restore check did not pass for $MANIFEST"
  fi
}

step_alert() {
  begin alert "A test alert reaches the phone" "#79, #80" \
    "Sends one alert through the lab's alert hook, as lab and with the installed \
configuration, the same path \`lab.cli status\` and \`lab.cli selftest\` use. Then asks \
whether it arrived."
  run 'sudo -u lab /bin/cat "$ALERT_CONFIG"'
  if ! ask "Send one test alert through the lab's alert hook now?"; then
    finish SKIPPED "the operator chose not to send a test alert"
    return
  fi
  rep "The alert command: \`$ALERT_PY\`"
  run 'sudo -u lab "$PY" -c "$ALERT_PY" "$ALERT_CONFIG"'
  if [ "$LAST_RC" != 0 ]; then
    finish FAIL "the alert hook failed or is not configured"
    return
  fi
  if ask "Did the test alert arrive on your phone?"; then
    finish PASS "the hook ran and the operator saw the alert on the phone"
  else
    finish FAIL "the hook ran but the operator did not confirm the alert arrived"
  fi
}

step_selftest() {
  begin selftest "The nightly self-test ran and passed" "#80" \
    "Reads the nightly self-test log. It must have been written in the last 26 hours and \
its recent lines must hold no FAIL."
  run 'sudo -u lab /usr/bin/find "$LOG_DIR/selftest.log" -mmin -1560'
  local recent=0
  has selftest.log && recent=1
  run 'sudo -u lab /usr/bin/tail -n 30 "$LOG_DIR/selftest.log"'
  local tail_rc="$LAST_RC" failures=0
  printf '%s\n' "$LAST_OUT" | grep -q '^FAIL' && failures=1
  run 'sudo -u lab /usr/bin/tail -n 10 "$LOG_DIR/selftest.err"'
  rep ""
  rep "Still manual for #80: a result, including ok, should reach the phone every morning. \
Check the phone for this morning's message."
  if [ "$tail_rc" != 0 ]; then
    finish FAIL "cannot read $LOG_DIR/selftest.log"
  elif [ "$recent" = 0 ]; then
    finish FAIL "the self-test log was not written in the last 26 hours"
  elif [ "$failures" = 1 ]; then
    finish FAIL "the self-test log has a FAIL line; read the tail above"
  else
    finish PASS "the self-test ran in the last 26 hours and its recent lines have no FAIL"
  fi
}

step_skillrun() {
  begin skillrun "A skill script runs only in a real container" "#255" \
    "Runs the gated real-container test of the broker tool \`skill.run\` from the checkout, \
as the operator. An active skill's script runs in an Apple container with no network and \
the task workspace as the only mount, its output is marked untrusted, and the container is \
gone afterwards. The test must pass, not skip. It needs LAB_CONTAINER_IMAGE set to an image \
pinned by digest."
  if [ -z "$CONTAINER_IMAGE" ] && ! dry; then
    finish SKIPPED "LAB_CONTAINER_IMAGE is not set to an image pinned by digest"
    return
  fi
  run 'container --version'
  run '(cd "$REPO" && LAB_CONTAINER_IMAGE="$CONTAINER_IMAGE" uv run --locked --extra dev python -m pytest tests/test_skillrun.py -k real_container -rs -q -p no:cacheprovider)'
  if [ "$LAST_RC" = 0 ] && has ' passed' && ! has 'skipped'; then
    finish PASS "the real-container test of skill.run passed"
  elif has 'skipped'; then
    finish FAIL "the real-container test was skipped, read the reason above"
  else
    finish FAIL "the real-container test of skill.run failed, read its output above"
  fi
}

step_mcp() {
  begin mcp "Signed MCP servers run under Seatbelt and match their snapshot" "#256" \
    "As lab, starts every signed MCP server in \`$MCP_CONFIG\` under the Seatbelt profile and \
compares its tool list with the snapshot the operator signed. Skipped when no server is \
configured."
  if ! dry && [ ! -f "$MCP_CONFIG" ]; then
    rep "No MCP server is configured: \`$MCP_CONFIG\` does not exist."
    finish SKIPPED "no MCP server is configured"
    return
  fi
  run 'sudo -u lab "$PY" -m lab.cli mcp --servers "$MCP_CONFIG" --operator-pubkey "$PUBKEY" list --check'
  if [ "$LAST_RC" = 0 ] && has 'no MCP servers are configured'; then
    finish SKIPPED "the MCP config lists no server"
  elif [ "$LAST_RC" = 0 ] && has '^ok '; then
    finish PASS "every signed server started under Seatbelt and matches its signed snapshot"
  else
    finish FAIL "a server is unsigned, changed since signing or did not start. Read the output above"
  fi
}

step_concurrency() {
  begin concurrency "Two model requests at once" "#211" \
    "Sends two chat completions to the loopback model server at the same moment and \
records each one's wall time, and memory and swap before, during and after. \`now\` \
prints the Unix time with milliseconds."
  MODEL_ID=""
  BODY=""
  heredoc CMD <<'EOF'
MODEL_ID=$(curl -sf --max-time 10 "$MODEL_URL/models" | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"][0]["id"])')
EOF
  run "$CMD"
  if [ -z "$MODEL_ID" ] && ! dry; then
    finish FAIL "the model server did not answer at $MODEL_URL/models"
    return
  fi
  rep "Model: \`$MODEL_ID\`"
  heredoc CMD <<'EOF'
BODY=$(python3 -c 'import json,sys;print(json.dumps({"model": sys.argv[1], "messages": [{"role": "user", "content": "Count from 1 to 150, separated by spaces."}], "max_tokens": 400, "temperature": 0}))' "$MODEL_ID")
EOF
  run "$CMD"
  run 'sysctl vm.swapusage; top -l 1 -n 0 | grep PhysMem; vm_stat'
  local before="$LAST_OUT"
  heredoc CMD <<'EOF'
touch "$WORK/sampling"; ( n=0; while [ -e "$WORK/sampling" ] && [ "$n" -lt 900 ]; do n=$((n + 1)); echo "sample at $(now)"; sysctl vm.swapusage; top -l 1 -n 0 | grep PhysMem; sleep 1; done ) >"$WORK/samples.txt" 2>&1 & SAMPLER=$!
EOF
  run "$CMD"
  heredoc CMD <<'EOF'
( s=$(now); code=$(curl -s --max-time 600 -o "$WORK/reply1.json" -w '%{http_code}' -H 'Content-Type: application/json' -d "$BODY" "$MODEL_URL/chat/completions"); echo "request 1 start $s end $(now) http $code" ) & P1=$!
( s=$(now); code=$(curl -s --max-time 600 -o "$WORK/reply2.json" -w '%{http_code}' -H 'Content-Type: application/json' -d "$BODY" "$MODEL_URL/chat/completions"); echo "request 2 start $s end $(now) http $code" ) & P2=$!
wait "$P1" "$P2"
EOF
  run "$CMD"
  local requests="$LAST_OUT"
  run 'rm -f "$WORK/sampling"; wait "$SAMPLER"; SAMPLER=""; grep -c "^sample at" "$WORK/samples.txt"'
  run 'sysctl vm.swapusage; top -l 1 -n 0 | grep PhysMem; vm_stat'
  local after="$LAST_OUT"
  if dry; then
    finish SKIPPED ""
    return
  fi
  local timing peak_mem swap_before swap_peak swap_after
  timing="$(printf '%s\n' "$requests" | awk '
    $1 == "request" && $2 == 1 { s1 = $4; e1 = $6; c1 = $8 }
    $1 == "request" && $2 == 2 { s2 = $4; e2 = $6; c2 = $8 }
    END {
      if (s1 == "" || s2 == "") { print "missing"; exit }
      w1 = e1 - s1; w2 = e2 - s2
      ls = (s1 > s2) ? s1 : s2; fe = (e1 < e2) ? e1 : e2
      lo = (w1 < w2) ? w1 : w2; hi = (w1 < w2) ? w2 : w1
      printf "http %s and %s; wall %.2f s and %.2f s; overlapped %s; longer/shorter %.2f\n",
        c1, c2, w1, w2, (ls < fe) ? "yes" : "no", (lo > 0) ? hi / lo : 0
    }')"
  peak_mem="$(peak_physmem_mb "$WORK/samples.txt")"
  swap_before="$(printf '%s\n' "$before" | peak_swap_mb)"
  swap_peak="$(peak_swap_mb "$WORK/samples.txt")"
  swap_after="$(printf '%s\n' "$after" | peak_swap_mb)"
  rep ""
  rep "- requests: $timing"
  rep "- peak PhysMem used during: $peak_mem"
  rep "- swap used: before $swap_before, peak during $swap_peak, after $swap_after"
  case "$timing" in
    "http 200 and 200;"*)
      finish PASS "$timing; peak memory $peak_mem; swap $swap_before before, $swap_peak peak, $swap_after after" ;;
    *)
      finish FAIL "a request did not return HTTP 200: $timing" ;;
  esac
}

step_drills() {
  begin drills "Timed kill and freeze drills" "#78" \
    "The two timed drills of runbook step 6, with its commands. A kill -9 must bring a new \
supervisor within about 30 seconds; a frozen supervisor must be gone and replaced within \
120 seconds, and the watchdog must then say healthy."
  if ! ask "These drills kill the supervisor twice. Work running now is interrupted. Check that the queue is idle. Run them?"; then
    finish SKIPPED "the operator chose not to run the drills"
    return
  fi
  OLD=""
  NEW=""
  run 'sudo -v'
  run 'pgrep -f lab.supervisor'
  if ! dry; then
    case "$LAST_OUT" in
      ''|*[!0-9]*)
        finish FAIL "expected exactly one running supervisor before the drills, found: ${LAST_OUT:-none}"
        return ;;
    esac
  fi
  run 'OLD=$(pgrep -f lab.supervisor)'
  run 'T0=$(date +%s)'
  run 'sudo kill -9 "$OLD"'
  run 'for i in $(seq 1 45); do NEW=$(pgrep -f lab.supervisor) && [ "$NEW" != "$OLD" ] && break; sleep 2; done'
  run 'echo "killed $OLD; new supervisor ${NEW:-none} after $(( $(date +%s) - T0 )) s"'
  local kill_line="$LAST_OUT" kill_ok=0
  [ -n "$NEW" ] && [ "$NEW" != "$OLD" ] && kill_ok=1

  # The freeze must not start before the new supervisor has written its first heartbeat: on
  # 2026-10-06 it did, the heartbeat file still named the killed pid, and the watchdog never
  # saw the frozen process (#271).
  if [ "$kill_ok" = 1 ] || dry; then
    run 'for i in $(seq 1 30); do sudo -u lab "$PY" -m lab.cli --db "$DB" watchdog --dry-run 2>&1 | grep -q "healthy pid $NEW " && break; sleep 2; done'
    run 'sudo -u lab "$PY" -m lab.cli --db "$DB" watchdog --dry-run'
    if ! dry && ! printf '%s' "$LAST_OUT" | grep -q "healthy pid $NEW "; then
      finish FAIL "$kill_line. The new supervisor $NEW had written no heartbeat 60 s after it started, so the freeze drill was not run"
      return
    fi
  fi
  VERIFIED="$NEW"

  NEW=""
  run 'sudo -v'
  # The pid whose heartbeat was just seen, not a fresh pgrep: launchd could have replaced it,
  # and the replacement would be frozen before its first beat. Checked once more right before.
  run 'OLD=$VERIFIED'
  run 'sudo -u lab "$PY" -m lab.cli --db "$DB" watchdog --dry-run'
  if ! dry && ! printf '%s' "$LAST_OUT" | grep -q "healthy pid $OLD "; then
    finish FAIL "$kill_line. The supervisor changed between the heartbeat check and the freeze (the heartbeat now says: ${LAST_OUT:-nothing}); rerun the drills"
    return
  fi
  run 'T0=$(date +%s)'
  # Set before the stop so a Ctrl-C during the wait still resumes it.
  dry || FROZEN="$OLD"
  run 'sudo kill -STOP "$OLD"'
  run 'for i in $(seq 1 90); do ps -p "$OLD" >/dev/null || break; sleep 2; done'
  run 'T1=$(date +%s)'
  run 'ps -p "$OLD" >/dev/null && { echo "FAIL: $OLD is still there after $((T1 - T0)) s; resuming it"; sudo kill -CONT "$OLD"; }'
  FROZEN=""
  run 'for i in $(seq 1 30); do NEW=$(pgrep -f lab.supervisor) && [ "$NEW" != "$OLD" ] && break; sleep 2; done'
  run 'if ps -p "$OLD" >/dev/null; then G="STILL THERE after $((T1 - T0)) s"; else G="gone after $((T1 - T0)) s"; fi; echo "frozen $OLD; $G; new supervisor ${NEW:-none} after $(( $(date +%s) - T0 )) s"'
  local freeze_line="$LAST_OUT" gone new_after freeze_ok=0
  gone="$(printf '%s\n' "$freeze_line" | sed -n 's/.*gone after \([0-9]*\) s.*/\1/p')"
  new_after="$(printf '%s\n' "$freeze_line" | sed -n 's/.* after \([0-9]*\) s$/\1/p')"
  run 'sudo -u lab "$PY" -m lab.cli --db "$DB" watchdog --dry-run'
  local healthy=0
  has healthy && healthy=1
  [ -n "$NEW" ] && [ "$NEW" != "$OLD" ] && [ "${gone:-999}" -le 120 ] \
    && [ "${new_after:-999}" -le 120 ] && [ "$healthy" = 1 ] && freeze_ok=1
  rep ""
  rep "Record both in \`ops/drills/log/\` with \`ops/drills/TEMPLATE.md\` (LAB_TARGET=mac-mini)."
  if [ "$kill_ok" = 1 ] && [ "$freeze_ok" = 1 ]; then
    finish PASS "$kill_line. $freeze_line. watchdog healthy"
  else
    finish FAIL "$kill_line. $freeze_line. watchdog healthy: $([ "$healthy" = 1 ] && echo yes || echo no)"
  fi
}

step_network() {
  begin network "Network unplug and the dead-man switch" "#79" \
    "Manual. Shows whether the operator hears about a lab that has gone silent."
  rep "Do this by hand, with your phone in reach:"
  rep ""
  rep "1. Write down the time, then unplug the Mac mini's network cable (and turn Wi-Fi off)."
  rep "2. Wait up to ten minutes. The dead-man switch alert must arrive on the phone."
  rep "3. Write down when it arrived, then plug the network back in."
  rep "4. Confirm the lab is reachable again over Tailscale and \`lab.cli status\` is healthy."
  rep ""
  rep "Paste the two times into #79. If no dead-man switch is set up yet, say so there."
  say "Manual: unplug the network, wait up to ten minutes for the dead-man alert, plug back in."
  finish MANUAL "unplug the network; the dead-man alert must reach the phone within ten minutes"
}

step_power() {
  begin power "Power-pull drill" "#77, #91" \
    "Manual. Shows that the lab comes back on its own after power loss, and what happens to a \
task that was running."
  rep "Do this by hand, when nothing real is running. Steps 1 and 5 run as you, from the"
  rep "checkout, without sudo, on a scratch database (never the lab's own)."
  rep ""
  rep "1. Leave two dummy tasks running, one idempotent and one not:"
  rep "   \`cd \"$REPO\" && LAB_TARGET=mac-mini uv run python -m lab.cli drill interrupted --phase arm\`"
  rep "2. Within 30 minutes, write down the time and pull the Mac mini's power cable."
  rep "3. Wait 30 seconds, plug it back in, and note the time. FileVault is on, so log in."
  rep "4. Time how long until \`sudo -u lab \"\$PY\" -m lab.cli --db \"\$DB\" status\` is healthy."
  rep "5. Check what recovery did with the two tasks; this writes the dated record:"
  rep "   \`cd \"$REPO\" && LAB_TARGET=mac-mini uv run python -m lab.cli drill interrupted --phase check\`"
  rep "   It must say PASS and restarted=yes, and the record must say no SIGTERM reached the holder."
  rep ""
  rep "Add your times and step 4's timing to the record in \`$REPO/ops/drills/log/\`,"
  rep "commit it, and paste the result into #77 and #91."
  say "Manual: power-pull drill; drill interrupted --phase arm, pull the plug, then --phase check"
  finish MANUAL "power-pull drill by hand: drill interrupted --phase arm, pull the plug, boot, --phase check"
}

# --------------------------------------------------------------------- main

selected() {
  [ -z "$ONLY" ] && return 0
  case ",$ONLY," in
    *",$1,"*) return 0 ;;
    *) return 1 ;;
  esac
}

{
  printf '# Mac mini session report\n\n'
  printf -- '- started: %s\n' "$STARTED"
  printf -- '- host: %s\n' "${HOSTNAME:-unknown}"
  if dry; then
    printf -- '- mode: dry run, nothing was run\n'
  else
    printf -- '- mode: live\n'
  fi
  printf -- '- REPO=%s\n- DB=%s\n- PY=%s\n- MODEL_URL=%s\n- BACKUP_VOLUME=%s\n' \
    "$REPO" "$DB" "$PY" "$MODEL_URL" "$BACKUP_VOLUME"
  printf -- '- PUBKEY=%s\n- ALERT_CONFIG=%s\n- LOG_DIR=%s\n' "$PUBKEY" "$ALERT_CONFIG" "$LOG_DIR"
  printf -- '- LAB_CONTAINER_IMAGE=%s\n' "${CONTAINER_IMAGE:-not set}"
  printf -- '- MCP_CONFIG=%s\n' "$MCP_CONFIG"
  printf -- '- steps: %s\n' "${ONLY:-all}"
  printf '\nWhat each step proves, and which issue to paste it into: ops/mac-session.md.\n'
} >>"$REPORT"

say "Report: $REPORT"
if ! dry; then
  say "sudo is asked for once, now."
  if ! sudo -v; then
    rep ""
    rep "sudo -v failed, so nothing was run."
    say "sudo -v failed; stopping before any check."
    exit 1
  fi
fi

interrupted() {
  # A frozen supervisor left stopped would freeze the lab if the watchdog
  # is what failed, so resume it before anything else.
  if [ -n "${FROZEN:-}" ]; then
    sudo kill -CONT "$FROZEN" 2>/dev/null
    rep "- resumed the frozen supervisor $FROZEN after the interrupt"
    FROZEN=""
  fi
  rep ""
  rep "**The session was interrupted during step $STEP_N.**"
  say ""
  say "Interrupted. Partial report: $REPORT"
  {
    printf '\n## Summary\n\n| # | Step | Issues | Result | Note |\n|---|---|---|---|---|\n'
    printf '%s' "$SUMMARY"
  } >>"$REPORT"
  exit 130
}
trap interrupted INT

for step in $ALL_STEPS; do
  selected "$step" && "step_$step"
done

{
  printf '\n## Summary\n\n| # | Step | Issues | Result | Note |\n|---|---|---|---|---|\n'
  printf '%s' "$SUMMARY"
} >>"$REPORT"

say ""
say "Report written to $REPORT ($FAILED failed)"
[ "$FAILED" = 0 ]
