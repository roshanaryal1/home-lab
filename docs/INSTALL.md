# Install home-lab on your Mac

home-lab is a local-first personal-agent runtime. The installer below is
designed for a fresh macOS user account and keeps the runtime under a
dedicated non-admin `lab` account.

> **Current scope:** this guide installs the repository and prepares the
> local runtime. It does not enable unrestricted autonomous work, add real
> credentials, or expose the model/dashboard to the network.

## Requirements

### Hardware

| Unified memory | Recommended starting model | Notes |
|---|---|---|
| 16 GB | Qwen3 4B Instruct 4-bit | Use the small model for development and light tasks. |
| 32 GB | Qwen3-Coder-30B-A3B-Instruct-4bit-DWQ | This is the currently measured heavy-model target. Keep one heavy inference slot. |
| 64 GB | Qwen3-Coder-30B-A3B-Instruct-4bit-DWQ or a separately benchmarked larger model | Do not increase concurrency or choose a larger model merely because memory is available; measure first. |

The 32 GB heavy model has been measured on the project's target Apple
silicon machine. Its measured weight footprint is about 17.2 GB and the
project uses a 20.5 GB admission budget. Context length also consumes
unified memory.

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
detected macOS version, architecture, unified-memory estimate, Git, Python
and `uv` availability, and the recommended model tier.

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
export MODEL_ID="mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit-DWQ"
export MODEL_REV="cfcade7221ccd128681961446e5f7906c08cae55"
```

Change `REPO`, `BACKUP_VOLUME` and the model variables to match your
machine. The backup volume is optional until you perform backup/restore
work.

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

- `operator.key` — private signing key; never give this to the agent.
- `operator.pub` — public verification key.

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

After applying, verify that:

```sh
sudo -u lab /usr/bin/sudo -n -l
/usr/bin/dscl . -read /Groups/admin GroupMembership
sudo -u lab /bin/cat "$HOME/.lab-operator/operator.key"
sudo -u lab /usr/bin/touch /Library/LaunchDaemons/com.homelab.supervisor.plist
```

The first, third and fourth commands should be refused. The admin-group
listing should not contain `lab`.

## 6. Deploy and start

For the full launchd deployment, follow:

```
ops/runbook-lab-account-and-daemons.md
```

Before running it:

1. Set `REPO`, `COMMIT`, `MODEL_ID`, `MODEL_REV` and
   `BACKUP_VOLUME`.
2. Read the complete runbook.
3. Start with its dry-run/setup-plan steps.
4. Keep the model server on loopback.
5. Use dummy data only.

The runbook intentionally separates operator actions requiring `sudo`
from code executed by the non-admin `lab` account.

## 7. First run

With the services running, check health:

```sh
sudo -u lab "$REPO/.venv/bin/python" -m lab.cli \
  --db /var/homelab/lab.db status

sudo -u lab "$REPO/.venv/bin/python" -m lab.cli tasks
sudo -u lab "$REPO/.venv/bin/python" -m lab.cli approvals
sudo -u lab "$REPO/.venv/bin/python" -m lab.cli audit verify
```

For the current system, an empty queue is a valid first-run state.

Do not connect real credentials or a public destination. Publishing is
tested against dummy providers until the remaining credential and recovery
checks are complete.

## 8. Model server

The current measured heavy-model setup is:

- loopback address: `127.0.0.1:8080`
- runtime: `mlx-lm`
- model: `Qwen3-Coder-30B-A3B-Instruct-4bit-DWQ`
- pinned revision: `cfcade7221ccd128681961446e5f7906c08cae55`

The exact server invocation and cache paths are intentionally kept in the
Mac runbook because model-server storage is machine-specific.

Never expose the model endpoint directly to the LAN or internet.

## 9. Backup and external storage

An external SSD is storage, not by itself a complete backup strategy.

When backup work is enabled, use the value of `BACKUP_VOLUME` rather than
hard-coding a volume name:

```sh
sudo -u lab "$REPO/.venv/bin/python" -m lab.cli \
  --db /var/homelab/lab.db backup \
  --to "$BACKUP_VOLUME/home-lab-backups"
```

Keep at least one additional recovery destination for anything that matters.

## 10. Uninstall

Stop and unload the services first:

```sh
for s in supervisor watchdog keepawake statuscheck selftest tick; do
  sudo launchctl bootout "system/com.homelab.$s" 2>/dev/null || true
done
```

If you have no data to preserve and are intentionally removing the
installation, remove the system deployment:

```sh
sudo rm -rf /opt/homelab /opt/homelab-python
sudo rm -f /Library/LaunchDaemons/com.homelab.*.plist
sudo rm -rf /etc/homelab
sudo sysadminctl -deleteUser lab
```

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
other personal agents. The current product roadmap in #189 adds chat,
brokered tools, memory, skills, integrations and bounded autonomy
incrementally.

Until the Phase 1/2 execution path is complete, installation should be
treated as installation of the controlled runtime foundation rather than
an invitation to give an agent unrestricted access to the Mac.
