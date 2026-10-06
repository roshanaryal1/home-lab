# Runbook: lab account, deployment and launchd daemons

For one sitting of about two hours with the operator at the Mac mini. It
covers setup sections 1, 5, 11 and 16, and prepares 10 and 19. Every
`sudo` step is typed by the operator; nothing here is run by an agent. Each
step says what it changes, how to check it, how to undo it, and which
checklist items it closes (by name).

Written 2026-09-29 UTC (2026-09-30 in New Zealand). The deployment approach (Python installed under
`/opt`, root-owned and not in a home folder, so the lab account depends on nothing in it) was
rehearsed in a scratch directory without `sudo` on that date: Python
3.13.15, SQLite 3.53.1, no reference to any home directory inside the
environment, and `lab.cli` ran. The `plutil` edits in step 4 were tested
on copies of the committed plists and pass `plutil -lint`.

> **zsh note.** Some blocks below have `#` comments at the end of some lines. macOS's default zsh does not treat those as comments when you paste, so run `setopt interactivecomments` first (it lasts for that Terminal window), or leave the comments out. The variables block in particular must not be pasted with its comment: it would leave `MODEL_REV` empty.

## Before you start

- Check out `main` in the operator's clone and confirm CI is green.
- Pick the commit to deploy and write it down: `git rev-parse HEAD`.
- The model server (`docs/INSTALL.md` section 8, or `ops/mac-mini-setup.md`
  section 13) runs as the operator's own process; on the project's machine it is a
  LaunchAgent. After a reboot it starts only once the operator logs in (FileVault,
  section 17), so until then `tick` records model errors; that is expected.

Variables used below. Replace the `PASTE_...` values first, then paste
the block once into the terminal:

```sh
REPO="$HOME/home-lab"
COMMIT="PASTE_THE_COMMIT_YOU_WROTE_DOWN"
MODEL_REV="PASTE_THE_SERVING_MODEL_40_HEX_REVISION"
BACKUP_VOLUME="/Volumes/PASTE_BACKUP_VOLUME_NAME"
UV="$HOME/.local/bin/uv"
```

`MODEL_REV` is the revision of the build the model server is serving (ADR 0001).
The model's name is not a variable: step 4 reads it from the server, which reports
the name it accepts (a path, not the Hugging Face repository name, which it
refuses with HTTP 404). Then check that no placeholder is left, because an unset
check cannot see a value that still says `PASTE_`:

```sh
case "$COMMIT$MODEL_REV$BACKUP_VOLUME" in *PASTE_*) echo "STOP: a PASTE_ value is still there" ;; *) echo "variables are filled in" ;; esac
```

## Step 1. Operator key (no sudo)

```sh
cd "$REPO" && uv run python -m lab.cli operator init --dir ~/.lab-operator
```

- Changes: creates the operator's Ed25519 keypair in `~/.lab-operator`.
- Check: `ls -l ~/.lab-operator` shows `operator.key` (private, yours
  only) and `operator.pub`.
- Undo: `rm -r ~/.lab-operator` (only before anything was signed with it).
- Closes: 11 "As the operator: operator init".

## Step 2. The lab account, directories and service files (sudo)

Dry run first and read it:

```sh
cd "$REPO" && uv run python -m lab.cli setup-plan --operator-pubkey ~/.lab-operator/operator.pub
```

Then apply it. It creates the non-admin `lab` account (you choose its
password at the prompt), `/var/homelab` and `/var/log/homelab` owned by
`lab` with mode 700, root-owned `/etc/homelab`, copies only the public key
there, and installs every service definition in `ops/launchd/` root-owned
under `/Library/LaunchDaemons`. It does not load them. Since #67 and #79 that includes the daily backup and
the dead-man switch ping. Since #239 that
includes `com.homelab.chat.plist`, which stays unloaded until the pairing step
in `ops/mac-mini-setup.md` section 23.

```sh
sudo "$REPO/.venv/bin/python" -m lab.cli setup-plan --apply \
  --operator-pubkey "$HOME/.lab-operator/operator.pub"
```

- Check: `--apply` runs the changing steps only; it prints the four checks
  for you to run. Run them with these exact commands (setup-plan prints the
  same four; its key check used a literal `~operator` until 2026-09-30 and
  now prints the absolute path):

  ```sh
  sudo -u lab /usr/bin/sudo -n -l
  /usr/bin/dscl . -read /Groups/admin GroupMembership
  sudo -u lab /bin/cat "$HOME/.lab-operator/operator.key"
  sudo -u lab /usr/bin/touch /Library/LaunchDaemons/com.homelab.supervisor.plist
  ```

  Expected, in order: a refusal ("a password is required" or "not allowed to run
  sudo"); a group list without `lab`; `Permission denied`; `Permission denied`.

- Undo, before step 7 only (no lab data exists yet):
  `sudo launchctl bootout system/com.homelab.<name>` for anything loaded,
  `sudo rm /Library/LaunchDaemons/com.homelab.*.plist`,
  `sudo sysadminctl -deleteUser lab`, and `sudo rm -r /etc/homelab`.
  Removing `/var/homelab` or `/var/log/homelab` deletes the lab's database
  and logs; that is a separate teardown, never part of an undo: first back
  up with `sudo -u lab /opt/homelab/.venv/bin/python -m lab.cli --db /var/homelab/lab.db backup --to "$BACKUP_VOLUME/home-lab-backups/before-teardown"`,
  confirm the manifest exists, and only then remove them.
- Closes: 1 "dedicated non-admin account" and "lab user cannot sudo";
  11 "create the non-admin lab account", "copy only operator.pub", "as
  lab, try cat operator.key"; 16 "create the lab account and
  /var/log/homelab".

**A fifth check: what else `lab` can read (#225).** The four checks above test one
file, `operator.key`, which is mode 600. But a new macOS account is put in the
`staff` group, and the operator's home folder is usually `drwxr-x---` with group
`staff`, so `lab` can traverse it and read whatever below it is group-readable
(on the project's machine 38,315 of the 38,495 files under `~/.claude`, counting
only files whose every parent folder the group can also search). Check the folder
and one folder below it, since blocking the listing alone is not the same as
blocking the way in. `~/Public` exists on every macOS account:

```sh
sudo -u lab /bin/ls "$HOME"
sudo -u lab /bin/ls "$HOME/Public"
```

Both must say `Permission denied`. If either lists anything, run `chmod 700 "$HOME"`
(your own folder, no `sudo`) and check both again. Do this before the first task
runs.

## Step 3. Deploy the code to /opt/homelab (sudo)

The services run `/opt/homelab/.venv/bin/python`. Code and Python are
root-owned so the lab account cannot change what it runs.

The clone and `uv sync` run as the operator, never as root: `uv sync`
builds the project with its build backend, and that code must not run with
root privileges. Root only creates the empty directories and takes
ownership afterwards.

```sh
sudo install -d -o "$USER" -g staff -m 755 /opt/homelab /opt/homelab-python
git clone https://github.com/roshanaryal1/home-lab.git /opt/homelab
git -C /opt/homelab -c advice.detachedHead=false checkout "$COMMIT"
cd /opt/homelab && UV_PYTHON_INSTALL_DIR=/opt/homelab-python \
  UV_PYTHON_PREFERENCE=only-managed "$UV" sync --locked
sudo chown -R root:wheel /opt/homelab /opt/homelab-python
sudo chmod -R go-w /opt/homelab /opt/homelab-python
```

- Check, as the lab account:
  `sudo -u lab /opt/homelab/.venv/bin/python -c "import sqlite3, lab; print(sqlite3.sqlite_version)"`
  prints 3.53.1 or later; and `sudo -u lab /usr/bin/touch /opt/homelab/x`
  is refused.
- Undo: first unload anything step 5 started
  (`for s in supervisor watchdog keepawake statuscheck selftest tick backup heartbeat; do sudo launchctl bootout system/com.homelab.$s; done`),
  then `sudo rm -rf /opt/homelab /opt/homelab-python`.
- Rehearsal note: the scratch rehearsal ran `uv sync` as the operator too.

## Step 4. Settings the service files need (sudo)

The committed plists leave four things to the operator: the operator key and
the model for the supervisor and the loop, the backup folder for the backup
job, and the ping URL file for the dead-man switch.

```sh
P=/Library/LaunchDaemons
sudo plutil -insert EnvironmentVariables.LAB_OPERATOR_PUBKEY -string /etc/homelab/operator.pub $P/com.homelab.supervisor.plist
MODEL_ID=$(curl -sf http://127.0.0.1:8080/v1/models | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"][0]["id"])')
if [ -z "$MODEL_ID" ]; then echo "STOP: the model server did not answer; start it and redo this block"; else
sudo plutil -insert EnvironmentVariables -dictionary $P/com.homelab.tick.plist
sudo plutil -insert EnvironmentVariables.LAB_MODEL_URL -string http://127.0.0.1:8080/v1 $P/com.homelab.tick.plist
sudo plutil -insert EnvironmentVariables.LAB_MODEL_NAME -string "$MODEL_ID" $P/com.homelab.tick.plist
sudo plutil -insert EnvironmentVariables.LAB_MODEL_REVISION -string "$MODEL_REV" $P/com.homelab.tick.plist
sudo plutil -insert EnvironmentVariables.LAB_OPERATOR_PUBKEY -string /etc/homelab/operator.pub $P/com.homelab.tick.plist
sudo plutil -insert EnvironmentVariables.LAB_MODEL_URL -string http://127.0.0.1:8080/v1 $P/com.homelab.supervisor.plist
sudo plutil -insert EnvironmentVariables.LAB_MODEL_NAME -string "$MODEL_ID" $P/com.homelab.supervisor.plist
sudo plutil -insert EnvironmentVariables.LAB_MODEL_REVISION -string "$MODEL_REV" $P/com.homelab.supervisor.plist
fi
sudo plutil -lint $P/com.homelab.*.plist
```

The last three insert lines give the supervisor the same model settings as the
loop, so the daemon registers the summarizer (section 16).

Interim alert channel, until the Telegram bot exists: alerts go to the
system log. Owned by `lab`, mode 600, as section 19 asks.

```sh
printf '{"command": ["/usr/bin/logger", "-t", "homelab-alert"], "timeout_seconds": 20, "min_interval_seconds": 3600}\n' \
  | sudo tee /etc/homelab/alert.json >/dev/null
sudo chown lab /etc/homelab/alert.json && sudo chmod 600 /etc/homelab/alert.json
```

- Check: `plutil -p $P/com.homelab.supervisor.plist` and
  `plutil -p $P/com.homelab.tick.plist` both show `LAB_OPERATOR_PUBKEY`.
  The loop needs it as well as the supervisor because it runs the queue
  itself whenever the daemon is down; since #190 both refuse to start
  without it, instead of running with approvals unchecked; `sudo -u lab cat /etc/homelab/alert.json` works.
- Undo: `sudo plutil -remove EnvironmentVariables.<KEY> <file>`;
  `sudo rm /etc/homelab/alert.json`.
- Closes: nothing on its own; 19 "alert command" becomes closable once
  step 5 loads the status check (the channel item stays open until the bot).

**The backup folder (#67).** The committed `com.homelab.backup.plist` says
`LAB_BACKUP_DIR` is `PASTE_BACKUP_DIR`. The job refuses to run while it says
that, so the real folder goes only into the installed, root-owned copy, the
same way the model settings above do. The job runs as `lab`, so `lab` must own
the folder. An external volume ignores file owners by default; turn owners on
first:

```sh
diskutil info "$BACKUP_VOLUME" | grep -i owners
sudo diskutil enableOwnership "$BACKUP_VOLUME"
sudo install -d -o lab -g staff -m 700 "$BACKUP_VOLUME/home-lab-backups"
sudo plutil -replace EnvironmentVariables.LAB_BACKUP_DIR -string "$BACKUP_VOLUME/home-lab-backups" $P/com.homelab.backup.plist
plutil -p $P/com.homelab.backup.plist | grep LAB_BACKUP_DIR
sudo -u lab /usr/bin/touch "$BACKUP_VOLUME/home-lab-backups/probe" && sudo -u lab /bin/rm "$BACKUP_VOLUME/home-lab-backups/probe" && echo "lab can write the backup folder"
```

The `grep` must show the real path, not `PASTE_`, and the last line must print
`lab can write the backup folder`. If the volume is encrypted, unmounting it to
apply the setting means unlocking it again (`diskutil apfs unlockVolume <diskNsM>`,
which asks for its passphrase). If `chown` or `install` still says `Operation not
permitted` as root, macOS is blocking the terminal app from changing files on a
removable disk: turn the terminal on under System Settings, Privacy & Security,
Full Disk Access, quit it completely and reopen it (met on 2026-10-07, #67). That
grants every command run from that terminal access to protected files, so it is a
temporary measure for this one change: turn it off again, and restart the terminal,
as soon as the `chown` has worked.

The scheduled job needs a second, permanent grant. Under launchd it has no terminal
to borrow permission from, and macOS refuses its interpreter access to a removable
volume (`tccd` logs `Refusing TCCAccessRequest for service
kTCCServiceSystemPolicyRemovableVolumes ... in background session`), so the job fails
with `backup: unable to open database file` while the same command run from a
permitted terminal works. Add the real interpreter, not the venv symlink, under
System Settings, Privacy & Security, Full Disk Access, with **+** and Cmd+Shift+G:
`/opt/homelab-python/cpython-3.13.15-macos-aarch64-none/bin/python3.13` (the folder
that `readlink -f /opt/homelab/.venv/bin/python` prints). Found and fixed on
2026-10-07 (#67). The grant belongs to the
binary, not to the `lab` account: every process that runs that interpreter gets it,
whichever account runs it, and the keep-awake and watchdog daemons run as root with
the same interpreter. For `lab` services, file permissions still bound what they can
read; a root-run process is not bounded that way. So a compromised lab service could
read or change the backups on the T7. Narrowing the grant to a backup-only
executable is open as #287. A redeploy that changes the Python version changes this
path and needs the grant again; `readlink -f` prints the new one (it works on current
macOS).

The job runs at 02:47, writes one backup,
restores it into a temporary folder and checks every hash, then deletes all but
the newest 14 backups in that folder. It deletes only its own manifests, their
databases and blobs only they used, and follows no symlink. Any failure, the
placeholder included, sends a `backup` alert through `/etc/homelab/alert.json`.
Give the folder itself, not a symlink to it: the job refuses a symlink.

- Undo: `sudo plutil -replace EnvironmentVariables.LAB_BACKUP_DIR -string PASTE_BACKUP_DIR $P/com.homelab.backup.plist`.
  The backups stay where they are.

**The ping URL file (#79).** Create it as in `ops/mac-mini-setup.md` section
19 before step 5 loads `com.homelab.heartbeat`. Until it exists the job only
logs, every five minutes, that it cannot read the file.

## Step 5. Start the services, one at a time (sudo)

Order: supervisor, watchdog, keep-awake, status check, self-test, tick,
backup, heartbeat.

```sh
for s in supervisor watchdog keepawake statuscheck selftest tick backup heartbeat; do
  sudo launchctl bootstrap system /Library/LaunchDaemons/com.homelab.$s.plist \
    || { echo "STOP: $s did not load; fix it before starting the rest"; break; }
  sleep 5; sudo launchctl print system/com.homelab.$s | grep -E "state|last exit"
done
```

- Check: the supervisor runs as `lab`
  (`ps -o user= -p $(pgrep -f lab.supervisor)`), its log has no
  "approvals are NOT signature-checked" line
  (`sudo tail /var/log/homelab/supervisor.log`), and
  `sudo -u lab /opt/homelab/.venv/bin/python -m lab.cli --db /var/homelab/lab.db status`
  reports healthy.
- Undo one service: `sudo launchctl bootout system/com.homelab.<name>`.
- Closes: 5 "launchd job ... for the supervisor", "watchdog or heartbeat",
  "structured rotating logs", "queue-aware sleep prevention" (after the
  keep-awake check in that item); 11 "confirm the supervisor log does NOT
  show ..." and "put the queue database ... under a lab-only path"; 16
  "install supervisor", "install watchdog", and "the loop" (after one
  `lab tick` by hand and `lab audit verify`); 18 "nightly job" and 19
  "alert command" (interim channel). After the first night, 18 "nightly job"
  also needs the "selftest ok" message to have arrived (#80).

## Step 6. Drills (sudo)

First, a hard kill. launchd should restart the supervisor within its 30 second
throttle:

```sh
sudo -v
OLD=$(pgrep -f lab.supervisor)
T0=$(date +%s)
sudo kill -9 "$OLD"
for i in $(seq 1 45); do NEW=$(pgrep -f lab.supervisor) && [ "$NEW" != "$OLD" ] && break; sleep 2; done
echo "killed $OLD; new supervisor ${NEW:-none} after $(( $(date +%s) - T0 )) s"
```

Then a frozen one. The watchdog must kill it and launchd must start another; the
loop times both, so this drill shows whether it happens within the two minutes
that issue #78 asks for, not only that it happens. It first waits until the
supervisor that the kill drill started has written a heartbeat, because freezing it
before that is not a valid test (it was done on 2026-10-06, #271). It freezes that
same process, and does nothing at all if no heartbeat appears within 60 seconds:

```sh
sudo -v
OLD=$(pgrep -f lab.supervisor)
OK=0
for i in $(seq 1 30); do sudo -u lab /opt/homelab/.venv/bin/python -m lab.cli --db /var/homelab/lab.db watchdog --dry-run | grep -q "^watchdog: healthy pid $OLD " && { OK=1; break; }; sleep 2; done
if [ "$OK" != 1 ]; then
  echo "STOP: no healthy heartbeat from supervisor $OLD after 60 s; nothing was frozen. See #271."
else
  T0=$(date +%s)
  sudo kill -STOP "$OLD"
  for i in $(seq 1 90); do ps -p "$OLD" >/dev/null || break; sleep 2; done
  T1=$(date +%s)
  ps -p "$OLD" >/dev/null && { echo "FAIL: $OLD is still there after $((T1 - T0)) s; resuming it"; sudo kill -CONT "$OLD"; }
  for i in $(seq 1 30); do NEW=$(pgrep -f lab.supervisor) && [ "$NEW" != "$OLD" ] && break; sleep 2; done
  if ps -p "$OLD" >/dev/null; then G="STILL THERE after $((T1 - T0)) s"; else G="gone after $((T1 - T0)) s"; fi
  echo "frozen $OLD; $G; new supervisor ${NEW:-none} after $(( $(date +%s) - T0 )) s"
  sudo -u lab /opt/homelab/.venv/bin/python -m lab.cli --db /var/homelab/lab.db watchdog --dry-run
fi
```

`sudo -v` comes first so a password prompt cannot be counted in the timing.
Read the two timings. For the first drill the new pid is expected within about 30
seconds; for the second, `gone after` and the new supervisor should both come in
under 120 seconds. The last line must say `healthy`. Put the numbers in the drill
record.

Log both as drills with `LAB_TARGET=mac-mini` (see `ops/drills/README.md`).

- Closes: 16 "kill -9 the supervisor", "freeze it instead", "watchdog
  --dry-run prints healthy". Not section 5's "confirm startup recovery":
  that needs a task in flight when the supervisor dies (queue a dummy
  idempotent and a non-idempotent task first, then check requeue and
  review); do it as a separate drill.

## Step 7. Backups, now that a live database exists

Section 10's first restore drill needs the database at
`/var/homelab/lab.db`:

```sh
sudo -u lab env LAB_TARGET=mac-mini /opt/homelab/.venv/bin/python -m lab.cli \
  --db /var/homelab/lab.db drill restore --log /var/log/homelab/drills
```

The scheduled backup (`com.homelab.backup`, set up in step 4) writes to
`$BACKUP_VOLUME/home-lab-backups` every night. Run it once now instead of
waiting for 02:47, then read what it printed:

```sh
sudo launchctl kickstart system/com.homelab.backup
sleep 30
sudo tail -n 5 /var/log/homelab/backup.log /var/log/homelab/backup.err
ls -l "$BACKUP_VOLUME/home-lab-backups"
```

`backup.log` must end with `restore check ok` and a `kept ... removed ...`
line, `backup.err` must be empty, and the folder must hold a new
`lab-*.manifest.json` with its `lab-*.db`.

- Closes: 10 "first full restore drill" (copy the record into
  `ops/drills/log/` and commit it).

## Updating the deployed code

Do this when a merged change has to reach the Mac mini (#204). It has not been
tried yet: the first run is the owner's, and its result belongs in this section.

The installed service definitions in `/Library/LaunchDaemons` are copies. A code
update does not change them, so first look at what the update touches, then
decide whether they need reinstalling.

Do the whole section in one Terminal window. The blocks below use `COMMIT`, `UV`
and `OLD` from the first block, and a new window starts without them. Write down
the `deployed now` hash the first block prints: it is the commit to roll back
to. If you have to open a new window before the checkout, run the first block
again. After the checkout, do not run it again, because `OLD` would then be the
new commit. Set the three by hand instead, with `OLD` set to the hash you wrote down.

```sh
COMMIT="PASTE_THE_NEW_COMMIT"
UV="$HOME/.local/bin/uv"
OLD=$(sudo git -C /opt/homelab rev-parse HEAD)
echo "deployed now: $OLD, updating to: $COMMIT"
```

Read both lines before going on: `deployed now` must be a 40 character hash and
`updating to` must not still say `PASTE_`.

The deployment is owned by root so the lab account cannot change what it runs.
As in step 3, ownership passes to you for the update and returns to root after,
and `git` and `uv sync` run as you, never as root:

```sh
sudo chown -R "$USER" /opt/homelab /opt/homelab-python
git -C /opt/homelab fetch origin
git -C /opt/homelab -c advice.detachedHead=false checkout "$COMMIT"
git -C /opt/homelab diff --stat "$OLD" "$COMMIT" -- ops/launchd lab/service.py
cd /opt/homelab && UV_PYTHON_INSTALL_DIR=/opt/homelab-python UV_PYTHON_PREFERENCE=only-managed "$UV" sync --locked
sudo chown -R root:wheel /opt/homelab /opt/homelab-python
sudo chmod -R go-w /opt/homelab /opt/homelab-python
```

- **The `diff --stat` line.** Empty output means the service definitions did not
  change. Any file listed means the installed copy of that definition is stale:
  reinstall it (`sudo install -o root -g wheel -m 644 /opt/homelab/ops/launchd/<file>
  /Library/LaunchDaemons/<file>`), redo that file's settings from step 4, then
  `sudo launchctl bootout system/com.homelab.<name>` and `bootstrap` it again.
  A file that is new (listed but not yet in `/Library/LaunchDaemons`) is
  installed the same way, given its settings from step 4, and bootstrapped.
  Do not re-run `setup-plan --apply` for this: it tries to create the `lab`
  account again.
- **After ownership is back with root,** run git as root
  (`sudo git -C /opt/homelab ...`): as you it stops with "dubious ownership",
  which is correct and is not to be silenced with `safe.directory`.

Then restart the two services that stay running (the others start fresh on
their timers) and check. Look at `status` first: `kickstart -k` kills the
supervisor, and work running at that moment is interrupted (startup recovery
requeues idempotent tasks and holds the rest for review), so do it when the
queue is idle.

```sh
for s in supervisor keepawake; do sudo launchctl kickstart -k system/com.homelab.$s; done
sleep 10
sudo git -C /opt/homelab rev-parse HEAD
ps -o user=,pid= -p $(pgrep -f lab.supervisor)
sudo -u lab /opt/homelab/.venv/bin/python -m lab.cli --db /var/homelab/lab.db status | head -3
sudo -u lab /usr/bin/touch /opt/homelab/x
```

- `rev-parse` prints the commit you asked for.
- `ps` shows `lab` as the user, and a new pid.
- `status` shows `health` as `IDLE` or `OK` (`ATTENTION` or `UNHEALTHY` needs
  a look at the reasons it lists) and `mode` as `running`.
- `touch` is refused with `Permission denied`.

**Roll back** by running the same block with `COMMIT` set to the `OLD` hash
printed at the start (write it down). If the supervisor does not start, its
reason is in `/var/log/homelab/supervisor.err`.

## What stays open after this sitting

FileVault's restart trade-off and the power-pull test (17), macOS update
policy (18 item 1), the Telegram alert channel and the dead-man switch's
outside check and unplug test (19),
the first real destination (15), secrets for real credentials (6), the
router port-forwarding check (2), and the monthly restore drills.
