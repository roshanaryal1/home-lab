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
| 16 GB | Qwen3 4B Instruct 4-bit | **Untested.** The small model was only run as a baseline on a 32 GB machine. |
| 32 GB | Qwen3-Coder-30B-A3B-Instruct-4bit-DWQ | **Measured** on the project's Apple M6 Mac mini. Keep one heavy inference slot. |
| 64 GB | The 32 GB model, or a separately benchmarked larger one | **Untested.** Do not raise concurrency or pick a larger model merely because memory is free; measure first. |

Only the 32 GB row has been measured: the model's weights take about 17.2 GB
and the project uses a 20.5 GB admission budget, and the context also uses
memory. That budget is a constant in `lab/model.py` and cannot be set from
configuration yet, so on a 16 GB Mac it is larger than the machine: run
only the small model there.

### Software

- macOS on Apple silicon.
- Git.
- Xcode Command Line Tools.
- Python 3.13 or newer, managed by `uv`.
- An account that can use `sudo` during installation. The running lab
  itself is deliberately non-admin.
- Enough free internal storage for Python, the repository and the selected
  model.
- Optional external storage for model archives, backups and research data.

Do not put real credentials into the installation. The repository is
designed to be exercised with dummy data until the remaining security
checks are complete.

## 1. Check prerequisites

From the repository root, run the read-only prerequisite check:

```sh
./scripts/check-prerequisites.sh
```

It changes nothing and never calls `sudo`. A successful check prints the
detected macOS version, architecture, unified memory, and whether Git, the
Xcode Command Line Tools and `uv` are present, and marks the model tier for
your memory size as measured or untested. It does not look at Python: `uv`
installs the Python 3.13 the project needs.

If it reports a missing prerequisite, install that prerequisite and run the
check again.

If `uv` is not installed yet, install it using the official installer,
then reopen the terminal. The check will tell you whether it is visible on
your PATH.

## 2. Choose your local variables

The runbook uses variables instead of assuming the original owner's paths
or disk names.

For a normal user clone:

```sh
export REPO="$HOME/home-lab"
export BACKUP_VOLUME="/Volumes/labbackup"
export MODEL_REV="cfcade7221ccd128681961446e5f7906c08cae55"
```

Change `REPO` and `BACKUP_VOLUME` to match your machine. The backup volume is
optional until you perform backup and restore work. `MODEL_REV` is the revision
of the 32 GB model above; for any other model use that model's own revision.

There is deliberately no model name to type. The runbook reads it from the
model server, which reports the name it will accept (on the project's machine a
path to the downloaded snapshot). The Hugging Face repository name is refused by
that server with HTTP 404, so do not put it in the configuration.

Keep one Terminal window open for the whole install: these are shell variables.

The production runbook also uses a fixed root-owned deployment location
(`/opt/homelab`) so the non-admin lab account cannot modify the code it
executes. That path is intentional and is not an owner-specific home
directory.

## 3. Clone and create the Python environment

```sh
git clone https://github.com/roshanaryal1/home-lab.git "$REPO"
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
```

The first, third, fourth and fifth commands must be refused. The admin-group
listing must not contain `lab`. If the fifth command lists your files, run
`chmod 700 "$HOME"` and check again: a new account is in the `staff` group, and a
group-readable home folder would let it read your files.

## 6. Deploy and start

For the full launchd deployment, follow:

```
ops/runbook-lab-account-and-daemons.md
```

Before running it:

1. Set `REPO`, `COMMIT`, `MODEL_REV` and `BACKUP_VOLUME`.
2. Read the complete runbook.
3. Skip its steps 1 and 2: they are sections 4 and 5 above and are done.
4. Keep the model server on loopback.
5. Use dummy data only.

The runbook intentionally separates operator actions requiring `sudo`
from code executed by the non-admin `lab` account.

## 7. First run

With the services running, check health:

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
then `tick` only records model errors). This is the setup measured on the project's
32 GB Mac; it is run as **your normal user, not the lab account**. The steps are the
ones verified in `ops/mac-mini-setup.md` section 13:

```sh
uv tool install mlx-lm==0.31.3
HF_HUB_DISABLE_XET=1 uv tool run --from mlx-lm python -c "from huggingface_hub import snapshot_download; snapshot_download('mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit-DWQ', revision='cfcade7221ccd128681961446e5f7906c08cae55')"
HF_HUB_OFFLINE=1 mlx_lm.server --host 127.0.0.1 --port 8080 --prompt-cache-size 1 --model "$HOME/.cache/huggingface/hub/models--mlx-community--Qwen3-Coder-30B-A3B-Instruct-4bit-DWQ/snapshots/$MODEL_REV"
```

- The download is about 16 GB.
- `--prompt-cache-size 1` is required: without it the server keeps every request's
  cache and runs out of Metal memory.
- `HF_HUB_DISABLE_XET=1` because the default download protocol stalled twice on the
  project's machine.
- Close large apps first; a single browser tab held 16 GB there.
- The last command runs in the foreground: leave that Terminal window open. Starting
  the server automatically at login and restarting it needs a LaunchAgent; the
  project's own is on its Mac mini and is not in this repository yet, so this guide
  cannot give it to you (#188).
- The runbook's step 4 reads the model's name from the running server, so start it
  before that step.

For another model, use its own repository, revision and path; nothing above has been
tested for it.

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
for s in supervisor watchdog keepawake statuscheck selftest tick backup heartbeat; do
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

## Troubleshooting

### The prerequisite check fails

Run it again and fix only the item it reports. It is intentionally
read-only, so it is safe to run repeatedly.

### `uv sync --locked` fails

Confirm that Python 3.13+ is available to `uv` and that the repository is
at the commit recorded in `COMMIT`. Do not bypass the lockfile for a
reproducible installation.

### The supervisor refuses to start

Check the launchd log and confirm that `LAB_OPERATOR_PUBKEY` points to the
installed public key. A missing operator public key is a fail-closed
condition for task execution.

### The model does not fit

Lower the context length or use the smaller model tier for your RAM size.
Do not increase the heavy-model concurrency. Unified memory is shared by
the model, context, macOS and other applications.

## What is deliberately not promised yet

This guide does **not** claim that home-lab is a drop-in replacement for
other personal agents. It has no chat interface, no real tools and no memory
you can talk to yet. Where the project could go is a proposal, not a plan: see
[docs/ROADMAP.md](ROADMAP.md) and issue #189.

Installing it today gives you the controlled runtime foundation: a queue, a
supervisor, signed approvals, an audit log and a local model. It is not an
invitation to give an agent unrestricted access to your Mac.

## For maintainers

The checklist for testing this guide on a fresh Mac or macOS user, which is
the open acceptance item of issue #188, is in
[ops/install-validation.md](../ops/install-validation.md).
