# Install home-lab on your Mac

home-lab is a local-first personal-agent runtime. This guide walks you
through installing it on a Mac you administer, and keeps the runtime under a
dedicated non-admin `lab` account.

> **Current scope:** this guide installs the repository and prepares the
> local runtime. It does not enable unrestricted autonomous work, add real
> credentials, or expose the model/dashboard to the network.

## Requirements

### Hardware

| Unified memory | Starting model | Status |
|---|---|---|
| 16 GB | Qwen3-4B-Instruct-2507-4bit | **Untested.** The small model was only run as a baseline on a 32 GB machine. |
| 32 GB | Qwen3-Coder-30B-A3B-Instruct-4bit-DWQ | **Measured** on the project's Apple M6 Mac mini. Keep one heavy inference slot. |
| 64 GB | The 32 GB model, or a separately benchmarked larger one | **Untested.** Do not raise concurrency or pick a larger model merely because memory is free; measure first. |

Only the 32 GB row has been measured: the model's weights take about 17.2 GB
and the project uses a 20.5 GB admission budget, and the context also uses
memory. That budget is a constant in `lab/model.py` and cannot be set from
configuration yet, so on a 16 GB Mac it is larger than the machine: run
only the small model there.

### Software

- macOS 14 or later on Apple silicon. MLX, which serves the model, asks for
  14.0 or later ([its install page](https://ml-explore.github.io/mlx/build/html/install.html));
  the project's own Mac runs macOS 27.0.
- Git.
- Xcode Command Line Tools.
- Python 3.13 or newer, managed by `uv`.
- An account that can use `sudo` during installation. The running lab
  itself is deliberately non-admin.
- At least 30 GB free on the disk that holds your home folder (the check in
  section 1 asks for it). The 32 GB model alone takes about 16 GB.
- Optional external storage for model archives, backups and research data.

Do not put real credentials into the installation. The repository is
designed to be exercised with dummy data until the remaining security
checks are complete.

## 1. Get the code and check prerequisites

The check is a script in the repository, so clone it first. Git comes with the
Xcode Command Line Tools: if `git --version` does not print a version, run
`xcode-select --install`, let it finish, and open a new Terminal window.

Clone into the folder you want the repository in. This guide uses
`$HOME/home-lab`; any folder works, and section 2 records which one. Then run
the read-only prerequisite check from the repository root:

```sh
git clone https://github.com/roshanaryal1/home-lab.git "$HOME/home-lab"
cd "$HOME/home-lab"
./scripts/check-prerequisites.sh
```

The check changes nothing and never calls `sudo`. A successful check prints the
detected macOS version, architecture, unified memory, and whether Git, the
Xcode Command Line Tools and `uv` are present, and marks the model tier for
your memory size as measured or untested. It does not look at Python: `uv`
installs the Python 3.13 the project needs.

If it reports a missing prerequisite, install that prerequisite and run the
check again.

If `uv` is not installed yet, install it with the official installer from
[uv's installation page](https://docs.astral.sh/uv/getting-started/installation/),
then open a new Terminal window and run the check again. It will tell you
whether `uv` is visible on your PATH.

## 2. Choose your local variables

The runbook uses variables instead of assuming the original owner's paths
or disk names.

For a normal user clone:

```sh
export REPO="$HOME/home-lab"
export BACKUP_VOLUME="/Volumes/PASTE_BACKUP_VOLUME_NAME"
export MODEL_REV="cfcade7221ccd128681961446e5f7906c08cae55"
```

Set `REPO` to the folder you cloned into in section 1. Replace
`PASTE_BACKUP_VOLUME_NAME` with the name of the external disk you will back up
to, as `ls /Volumes` lists it. The backup volume is optional until you perform
backup and restore work: with no disk yet, leave the placeholder and see
section 6. `MODEL_REV` is the revision of the 32 GB model above; on 16 GB use
the small model's, `50d427756c6b1b2fe0c0a10f67fbda1fc8e82c1b` (section 8), and
for any other model that model's own revision.

There is deliberately no model name to type. The runbook reads it from the
model server, which reports the name it will accept (on the project's machine a
path to the downloaded snapshot). The Hugging Face repository name is refused by
that server with HTTP 404, so do not put it in the configuration.

Keep one Terminal window open for the whole install: these are shell variables,
and a new window starts without them. The model server in section 8 runs in a
second window and does not need them.

The production runbook also uses a fixed root-owned deployment location
(`/opt/homelab`) so the non-admin lab account cannot modify the code it
executes. That path is intentional and is not an owner-specific home
directory.

## 3. Create the Python environment

```sh
cd "$REPO"

uv sync --locked

uv run python -m lab.cli --help
```

Record the commit you are installing:

```sh
export COMMIT="$(git rev-parse HEAD)"
printf 'Installing commit: %s\n' "$COMMIT"
```

For a reproducible deployment, pin `COMMIT` in the runbook rather than
installing an unrecorded moving branch.

## 4. Install the operator key

The operator key belongs to the human operator, not the lab account:

```sh
uv run python -m lab.cli operator init --dir "$HOME/.lab-operator"
```

Check that the directory contains:

- `operator.key`: private signing key; never give this to the agent.
- `operator.pub`: public verification key.

The lab account must not be able to read `operator.key`.

## 5. Review the setup plan

Before making system changes, inspect the generated plan:

```sh
uv run python -m lab.cli setup-plan \
  --operator-pubkey "$HOME/.lab-operator/operator.pub"
```

The plan describes the dedicated non-admin `lab` account, its data
directories and the root-owned launchd service definitions.

The apply step requires `sudo` because it creates the service account and
system-level launchd configuration:

```sh
sudo "$REPO/.venv/bin/python" -m lab.cli setup-plan --apply \
  --operator-pubkey "$HOME/.lab-operator/operator.pub"
```

This is step 2 of the runbook in section 6: do it once, here or there, not
both (a second run stops at "create the account", which already exists).

After applying, verify that:

```sh
sudo -u lab /usr/bin/sudo -n -l
/usr/bin/dscl . -read /Groups/admin GroupMembership
sudo -u lab /bin/cat "$HOME/.lab-operator/operator.key"
sudo -u lab /usr/bin/touch /Library/LaunchDaemons/com.homelab.supervisor.plist
sudo -u lab /bin/ls "$HOME"
sudo -u lab /bin/ls "$HOME/Public"
```

The first, third, fourth, fifth and sixth commands must be refused (the last
four with `Permission denied`). The admin-group listing must not contain `lab`.
If the fifth or sixth command lists anything, run `chmod 700 "$HOME"` and check
both again: a new account is in the `staff` group, and a group-readable home
folder would let it read your files. The sixth checks the way in, not only the
listing (runbook step 2, #225).

## 6. Deploy and start

For the full launchd deployment, follow:

```
ops/runbook-lab-account-and-daemons.md
```

Before running it:

1. Start the model server (section 8) in a second Terminal window and leave it
   running: runbook step 4 reads the model's name from it.
2. `REPO`, `COMMIT`, `MODEL_REV` and `BACKUP_VOLUME` are already set in this
   window (sections 2 and 3). Do not paste the runbook's variables block over
   them: run only its `UV=` line, then its placeholder check.
3. Read the complete runbook.
4. Skip its steps 1 and 2: they are sections 4 and 5 above and are done.
5. Keep the model server on loopback.
6. Use dummy data only.
7. No backup disk yet? The placeholder check then says STOP because of
   `BACKUP_VOLUME`. Check that the other two are filled in
   (`echo "$COMMIT $MODEL_REV"`), then go on, but skip every block that uses
   `BACKUP_VOLUME`: the backup folder block in runbook step 4 and the second
   block of step 7. Until a folder is set, the nightly backup job refuses to run
   and sends a `backup` alert, which with the runbook's interim channel goes to
   the system log.
8. The runbook's "Closes:" lines and its steps that commit drill records are
   the project's bookkeeping for its own Mac mini; skip them.

The runbook intentionally separates operator actions requiring `sudo`
from code executed by the non-admin `lab` account.

## 7. First run

With the services running, check health. Start with the doctor: it runs seven
read-only checks (database, operator key, model server, disk, backup age, last
selftest and audit chain), prints one `ok` or `FAIL` line for each, and exits 1
if any of them fails.

```sh
uv run python -m lab.cli doctor
```

On the deployed machine, add `--db /var/homelab/lab.db` before `doctor`, as the
commands below do. Doctor reads the `LAB_` settings from its own environment, so
give it the same ones the services have (`LAB_MODEL_URL`, `LAB_MODEL_NAME`,
`LAB_BACKUP_DIR` and `LAB_OPERATOR_PUBKEY`), or the model and backup checks
report `not configured`.

Then run the existing checks as the lab account:

```sh
sudo -u lab /opt/homelab/.venv/bin/python -m lab.cli \
  --db /var/homelab/lab.db status

sudo -u lab /opt/homelab/.venv/bin/python -m lab.cli --db /var/homelab/lab.db tasks
sudo -u lab /opt/homelab/.venv/bin/python -m lab.cli --db /var/homelab/lab.db approvals
sudo -u lab /opt/homelab/.venv/bin/python -m lab.cli --db /var/homelab/lab.db audit verify
```

For the current system, an empty queue is a valid first-run state.

Do not connect real credentials or a public destination. Publishing is
tested against dummy providers until the remaining credential and recovery
checks are complete.

## 8. Model server

The lab needs a model server on loopback before its loop can do anything (until
then `tick` only records model errors). Start it before runbook step 4 (section
6), which reads the model's name from it, and in a second Terminal window: the
last command keeps running there. This is the setup measured on the project's
32 GB Mac; it is run as **your normal user, not the lab account**. The steps are the
ones verified in `ops/mac-mini-setup.md` section 13:

```sh
uv tool install mlx-lm==0.31.3
HF_HUB_DISABLE_XET=1 uv tool run --from mlx-lm python -c "from huggingface_hub import snapshot_download; snapshot_download('mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit-DWQ', revision='cfcade7221ccd128681961446e5f7906c08cae55')"
HF_HUB_OFFLINE=1 mlx_lm.server --host 127.0.0.1 --port 8080 --prompt-cache-size 1 --model "$HOME/.cache/huggingface/hub/models--mlx-community--Qwen3-Coder-30B-A3B-Instruct-4bit-DWQ/snapshots/cfcade7221ccd128681961446e5f7906c08cae55"
```

- The download is about 16 GB.
- If the shell says `mlx_lm.server` is not found, uv's folder for tool commands
  is not on your PATH: run `uv tool update-shell`, open a new window and run the
  last line again.
- `--prompt-cache-size 1` is required: without it the server keeps every request's
  cache and runs out of Metal memory.
- `HF_HUB_DISABLE_XET=1` because the default download protocol stalled twice on the
  project's machine.
- Close large apps first; a single browser tab held 16 GB there.
- The last command runs in the foreground: leave that Terminal window open. Starting
  the server automatically at login and restarting it needs a LaunchAgent; the
  project's own is on its Mac mini and is not in this repository yet, so this guide
  cannot give it to you (#188).

On 16 GB, use the small model instead. In the last two lines put
`mlx-community/Qwen3-4B-Instruct-2507-4bit` for the repository,
`50d427756c6b1b2fe0c0a10f67fbda1fc8e82c1b` for the revision (both places), and
`models--mlx-community--Qwen3-4B-Instruct-2507-4bit` for the folder in the path.
That build ran on the project's 32 GB Mac as the baseline (`ops/mac-mini-setup.md`
section 14, about 2.3 GB); it has not been run on a 16 GB Mac.

For another model, use its own repository, revision and path; nothing above has been
tested for it. For any model but the 32 GB one, the lab still counts that model's
measured 17,180 MB unless `LAB_MODEL_WEIGHTS_MB` is set in the tick and supervisor
service definitions (`lab/loop.py`).

Never expose the model endpoint directly to the LAN or internet.

## 9. Backup and external storage

An external SSD is storage, not by itself a complete backup strategy.

When backup work is enabled, use the value of `BACKUP_VOLUME` rather than
hard-coding a volume name:

```sh
sudo -u lab /opt/homelab/.venv/bin/python -m lab.cli \
  --db /var/homelab/lab.db backup \
  --to "$BACKUP_VOLUME/home-lab-backups"
```

The daily backup job (`com.homelab.backup`) runs the same command with
`--keep 14`: it restore-checks each new backup and keeps the newest 14. Its
folder is set in the installed copy, as described in
`ops/runbook-lab-account-and-daemons.md` step 4. On macOS it reaches a removable
volume only with Full Disk Access, so it runs through a small launcher that holds
that grant instead of the interpreter every lab service uses (#287); step 4 of the
runbook builds it and says what the grant covers.

Once only the launcher holds that grant, the command above, run from Terminal,
cannot reach a removable volume. Start a manual backup through the job instead,
so it runs with the launcher's grant:

```sh
sudo launchctl kickstart system/com.homelab.backup
```

Keep at least one additional recovery destination for anything that matters.

## 10. Uninstall

Stop and unload the services first:

```sh
for s in supervisor watchdog keepawake statuscheck selftest tick backup heartbeat chat weekly-eval; do
  sudo launchctl bootout "system/com.homelab.$s" 2>/dev/null || true
done
```

If you have no data to preserve and are intentionally removing the
installation, remove the system deployment:

```sh
sudo rm -rf /opt/homelab /opt/homelab-python /opt/homelab-backup
sudo rm -f /Library/LaunchDaemons/com.homelab.*.plist
sudo rm -rf /etc/homelab
sudo sysadminctl -deleteUser lab
```

Then remove the backup launcher's entry from System Settings, Privacy & Security,
Full Disk Access.

**Data warning:** removing `/var/homelab` or `/var/log/homelab` deletes
the local database and logs. Back them up first if they matter.

To remove only the operator key:

```sh
rm -rf "$HOME/.lab-operator"
```

To remove the model server and its download (about 16 GB for the 32 GB model),
stop the server with Ctrl+C in its window, then:

```sh
uv tool uninstall mlx-lm
rm -rf "$HOME/.cache/huggingface/hub/models--mlx-community--Qwen3-Coder-30B-A3B-Instruct-4bit-DWQ"
```

For the small model the folder is `models--mlx-community--Qwen3-4B-Instruct-2507-4bit`.
Last, delete your clone, the folder `REPO` names.

## Troubleshooting

### The prerequisite check fails

Run it again and fix only the item it reports. It is intentionally
read-only, so it is safe to run repeatedly.

### `uv sync --locked` fails

Confirm that Python 3.13+ is available to `uv` and that the repository is
at the commit recorded in `COMMIT`. Do not bypass the lockfile for a
reproducible installation.

### The supervisor refuses to start

Read `/var/log/homelab/supervisor.err` (with `sudo`) and confirm that
`LAB_OPERATOR_PUBKEY` points to the installed public key. A missing operator
public key is a fail-closed condition for task execution.

### The model does not fit

The lab caps its own requests at 8,192 tokens of context and 512 of output
(`lab/loop.py`); that is not configurable yet. If the model server still runs
out of memory, check that it was started with `--prompt-cache-size 1` (section
8), close other large apps, or use the smaller model for your RAM size.
Do not increase the heavy-model concurrency. Unified memory is shared by
the model, context, macOS and other applications.

## What is deliberately not promised yet

This guide does **not** claim that home-lab is a drop-in replacement for
other personal agents. Its Telegram chat (#239) and its three reviewed tools
(#240) are new: both are built and tested, the chat was installed on the
project's own Mac on 2026-10-07, the tools have not run there yet, and this
guide does not set either up (the chat's steps are in `ops/mac-mini-setup.md`
section 23). There is no memory you can talk to yet. Where the project is going
is in [docs/ROADMAP.md](ROADMAP.md), decided in issue #189.

Installing it today gives you the controlled runtime foundation: a queue, a
supervisor, signed approvals, an audit log and a local model. It is not an
invitation to give an agent unrestricted access to your Mac.

## For maintainers

The checklist for testing this guide on a fresh Mac or macOS user, which is
the open acceptance item of issue #188, is in
[ops/install-validation.md](../ops/install-validation.md).
