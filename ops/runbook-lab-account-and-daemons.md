# Runbook: lab account, deployment and launchd daemons

For one sitting of about two hours with the operator at the Mac mini. It
covers setup sections 1, 5, 11 and 16, and prepares 10 and 19. Every
`sudo` step is typed by the operator; nothing here is run by an agent. Each
step says what it changes, how to check it, how to undo it, and which
checklist items it closes (by name).

Written 2026-09-29 UTC (2026-09-30 in New Zealand). The deployment approach (Python installed under
`/opt`, not in a home folder, which the lab account cannot read) was
rehearsed in a scratch directory without `sudo` on that date: Python
3.13.15, SQLite 3.53.1, no reference to any home directory inside the
environment, and `lab.cli` ran. The `plutil` edits in step 4 were tested
on copies of the committed plists and pass `plutil -lint`.

## Before you start

- Check out `main` in the operator's clone and confirm CI is green.
- Pick the commit to deploy and write it down: `git rev-parse HEAD`.
- The model server keeps running as the operator's LaunchAgent. After a
  reboot it starts only once the operator logs in (FileVault, section 17),
  so until then `tick` records model errors; that is expected.

Variables used below. Replace the two `PASTE_...` values first, then paste
the block once into the terminal:

```sh
REPO="$HOME/Research and Development /home-lab"
COMMIT="PASTE_THE_COMMIT_YOU_WROTE_DOWN"
MODEL_REV="PASTE_THE_SERVING_MODEL_40_HEX_REVISION"   # ADR 0001, the build now served
UV=/Users/$USER/.local/bin/uv
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
there, and installs the six service definitions root-owned under
`/Library/LaunchDaemons`. It does not load them.

```sh
sudo "$REPO/.venv/bin/python" -m lab.cli setup-plan --apply \
  --operator-pubkey /Users/$USER/.lab-operator/operator.pub
```

- Check: `--apply` runs the changing steps only; it prints the four checks
  for you to run. Run them with these exact commands (setup-plan prints the
  same four; its key check used a literal `~operator` until 2026-09-30 and
  now prints the absolute path):

  ```sh
  sudo -u lab /usr/bin/sudo -n -l                                  # expect a refusal
  /usr/bin/dscl . -read /Groups/admin GroupMembership              # expect no "lab"
  sudo -u lab /bin/cat /Users/$USER/.lab-operator/operator.key      # expect Permission denied
  sudo -u lab /usr/bin/touch /Library/LaunchDaemons/com.homelab.supervisor.plist  # expect Permission denied
  ```

- Undo, before step 7 only (no lab data exists yet):
  `sudo launchctl bootout system/com.homelab.<name>` for anything loaded,
  `sudo rm /Library/LaunchDaemons/com.homelab.*.plist`,
  `sudo sysadminctl -deleteUser lab`, and `sudo rm -r /etc/homelab`.
  Removing `/var/homelab` or `/var/log/homelab` deletes the lab's database
  and logs; that is a separate teardown, never part of an undo: first back
  up with `sudo -u lab /opt/homelab/.venv/bin/python -m lab.cli --db /var/homelab/lab.db backup --to /Volumes/labbackup/home-lab-backups/before-teardown`,
  confirm the manifest exists, and only then remove them.
- Closes: 1 "dedicated non-admin account" and "lab user cannot sudo";
  11 "create the non-admin lab account", "copy only operator.pub", "as
  lab, try cat operator.key"; 16 "create the lab account and
  /var/log/homelab".

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
  (`for s in supervisor watchdog keepawake statuscheck selftest tick; do sudo launchctl bootout system/com.homelab.$s; done`),
  then `sudo rm -rf /opt/homelab /opt/homelab-python`.
- Rehearsal note: the scratch rehearsal ran `uv sync` as the operator too.

## Step 4. Settings the service files need (sudo)

The committed plists leave two things to the operator.

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
# Section 16: the supervisor needs the same three, so the daemon registers the summarizer.
sudo plutil -insert EnvironmentVariables.LAB_MODEL_URL -string http://127.0.0.1:8080/v1 $P/com.homelab.supervisor.plist
sudo plutil -insert EnvironmentVariables.LAB_MODEL_NAME -string "$MODEL_ID" $P/com.homelab.supervisor.plist
sudo plutil -insert EnvironmentVariables.LAB_MODEL_REVISION -string "$MODEL_REV" $P/com.homelab.supervisor.plist
fi
sudo plutil -lint $P/com.homelab.*.plist
```

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

## Step 5. Start the services, one at a time (sudo)

Order: supervisor, watchdog, keep-awake, status check, self-test, tick.

```sh
for s in supervisor watchdog keepawake statuscheck selftest tick; do
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
  "alert command" (interim channel).

## Step 6. Drills (sudo)

```sh
OLD=$(pgrep -f lab.supervisor); sudo kill -9 "$OLD"   # launchd restarts it within ~30 s
sleep 40; NEW=$(pgrep -f lab.supervisor)
[ -n "$NEW" ] && [ "$NEW" != "$OLD" ] && echo "restarted as $NEW" || echo "NOT restarted"
OLD=$NEW; sudo kill -STOP "$OLD"                     # frozen; the watchdog must kill it
sleep 150; NEW=$(pgrep -f lab.supervisor)
if ps -p "$OLD" >/dev/null; then echo "FAIL: frozen $OLD still exists; resuming it"; sudo kill -CONT "$OLD"; \
elif [ -n "$NEW" ] && [ "$NEW" != "$OLD" ]; then echo "replaced: $OLD -> $NEW"; \
else echo "FAIL: no new supervisor; kicking it"; sudo launchctl kickstart -k system/com.homelab.supervisor; \
  sleep 10; pgrep -f lab.supervisor >/dev/null && echo "supervisor running again" || echo "STOP: supervisor still down"; fi
sudo -u lab /opt/homelab/.venv/bin/python -m lab.cli --db /var/homelab/lab.db watchdog --dry-run
```

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

Scheduling the backup to `/Volumes/labbackup/home-lab-backups` needs the
lab account to write there (the volume is currently the operator's);
decide ownership in the sitting.

- Closes: 10 "first full restore drill" (copy the record into
  `ops/drills/log/` and commit it).

## Updating the deployed code

Do this when a merged change has to reach the Mac mini (#204). It has not been
tried yet: the first run is the owner's, and its result belongs in this section.

The installed service definitions in `/Library/LaunchDaemons` are copies. A code
update does not change them, so first look at what the update touches, then
decide whether they need reinstalling.

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
policy (18 item 1), the Telegram alert channel and dead-man switch (19),
the first real destination (15), secrets for real credentials (6), the
router port-forwarding check (2), and the monthly restore drills.
