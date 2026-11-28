# Troubleshooting an installation safely

This is a companion to [INSTALL.md](INSTALL.md) and the deployed-Mac
[runbook](../ops/runbook-lab-account-and-daemons.md). It describes diagnosis,
not a successful install test. The remaining fresh-account acceptance check is
[#188](https://github.com/roshanaryal1/home-lab/issues/188).

**Safety rule:** start with read-only commands. Do not make the agent an admin,
disable FileVault, remove operator-key checks, put the model server on the LAN,
turn off the sandbox or bypass a failed audit to make a check green. While
diagnosing, do not run the deployed build in `/opt/homelab` as yourself or as
root (step 1 of "On the deployed Mac" says why). Never run cleanup or restore
commands against data you have not backed up.

## Start here: collect a local diagnosis

Run the diagnosis that matches where you are. Every command in this section
only reads.

### In your checkout, before or during installation

Use the environment that
[INSTALL.md section 3](INSTALL.md#3-create-the-python-environment) built,
through its own interpreter. Do not use `uv run` for this: it locks and syncs
the project before it runs anything, so it can change the environment you are
diagnosing. `REPO` is your clone, as in INSTALL.md section 2.

```sh
PY="$REPO/.venv/bin/python"
if [ -x "$PY" ]; then
  "$PY" -I -m lab.cli --version
  LAB_OPERATOR_PUBKEY="$HOME/.lab-operator/operator.pub" "$PY" -I -m lab.cli doctor
  LAB_OPERATOR_PUBKEY=/etc/homelab/operator.pub "$PY" -I -m lab.cli security-audit
else
  echo "STOP: no environment at $PY; finish INSTALL.md section 3 first"
fi
```

A `STOP` line means a prerequisite is missing. Complete INSTALL.md section 3 as
written rather than letting another command build the environment.

`doctor` here opens its default database, `~/.local/share/home-lab/lab.db`,
not one in the checkout, and nothing in INSTALL.md sections 1 to 5 creates it.
So `database` fails with `no database at`, and `selftest` and `audit_chain`
fail with `cannot read`. That is expected: do not create a database to clear
it. `lab_account` and `permissions` look at that database's folder, so they say
nothing about the lab account here. After `setup-plan --apply`, `services`
lists every `com.homelab` job as not loaded until runbook step 5 starts them.
The lines that tell you something at this stage are `operator_key` (the key
from section 4, or `/etc/homelab/operator.pub` once setup has installed it),
`disk` and `versions`.

`security-audit` checks the deployed paths and the key the services use,
`/etc/homelab/operator.pub`. Which findings fail depends on how far the
installation has got:

| Stage | Your run, from the checkout | The `lab` run of the deployed build (below) |
|---|---|---|
| Before `setup-plan --apply` (INSTALL.md sections 1 to 4) | `deploy`, `services`, `config`, `data` and `public_key` fail: `/opt/homelab`, `/opt/homelab-python`, the `com.homelab` plists, `/etc/homelab` and `/var/homelab` do not exist yet. Once section 4 has made the key, `private_key` fails too: without `/var/homelab` it cannot show that the key belongs to another account than the lab's. | Not possible yet. |
| After `setup-plan --apply` (INSTALL.md section 5, runbook step 2) | Only `deploy` fails, because runbook step 3 creates the two deployed folders. `data` is skipped. Setup installed everything else, so any other `FAIL` is real. | Not possible yet. |
| Deployed (runbook steps 3 and 4) | Every finding is `ok`, and `data` is skipped. | Only `data` fails, for `/var/homelab/lab.db` alone: the supervisor creates it when step 5 starts it. `private_key` is skipped. A build older than #404 also fails `config` (see the permissions row below). |
| Running (runbook step 5 on) | Every finding is `ok`, and `data` is skipped. | Every finding is `ok`, and `private_key` is skipped. The #404 note applies here too. |

Treat a `FAIL` as expected only after checking what it names. Run the audit
again with `--details` (in your own Terminal only: it prints paths) and check
each path under the `FAIL` with `ls -ld PATH`, or, for `/var/homelab/lab.db`,
with `sudo -u lab /bin/ls -ld /var/homelab/lab.db`. The `FAIL` is expected only
if the table names that path for your stage and `ls` says `No such file or
directory`. Two expected failures name no missing path: `services` names none
when there is no `com.homelab` plist at all, and before setup `private_key`
names your key, which exists. A `FAIL` for a path that exists, or one the table
does not name, is an ownership or mode problem: see the permissions row below.

### On the deployed Mac

Diagnose the deployed build as the `lab` account, with the settings the
services actually use. The checkout's code can differ from the deployed commit
(for example, newer migrations make `doctor` report a false schema failure),
and the deployed data folder is closed to every account but `lab`, so your own
account cannot read `/var/homelab/lab.db`. Do the three steps in order, in one
Terminal window.

**1. Check the deployment before running it.** Run the checkout audit above, as
yourself. It reads the owners and modes under `/opt/homelab` and
`/opt/homelab-python` without running any of that code, and the checkout is
yours, so the lab account cannot have changed it. Go on only when `deploy` and
`services` both say `ok`. If either fails, the code or a service definition may
have been writable by an account other than root, so stop. Do not run
`/opt/homelab/.venv/bin/python` as yourself or as root: code that `lab` could
change would then run with access to your files, `operator.key` included.
Python's `-I` does not prevent that. It keeps your environment and the current
folder out of the interpreter; it does not check what is installed. Look at the
reported paths with `--details` and `ls -ld`:

- `deploy` names a path owned by you, with no group or other write bit: that is
  what the ownership block of the
  [update runbook](../ops/runbook-lab-account-and-daemons.md#updating-the-deployed-code)
  leaves when it stops half way. Finish that block.
- `services` names a definition owned by you, with no group or other write bit:
  reinstall it root-owned, redo its settings and reload it, as the update
  runbook says for a changed definition.
- Anything owned by `lab`, or writable by a group or others: the lab account
  may have changed what it runs. Leave it as it is, so the evidence stays, and
  report it as [SECURITY.md](../SECURITY.md) says. Giving it back to root would
  make the audit pass without making the code trustworthy.

This guide never runs the deployed build as you. Running it as `lab`, which
runs it anyway, gives it nothing new.

**2. Read each service's settings, and stop at any that cannot be read.**

```sh
P=/Library/LaunchDaemons
plutil -lint "$P/com.homelab.supervisor.plist" "$P/com.homelab.tick.plist" "$P/com.homelab.backup.plist"
```

Each line must end in `OK`. If one does not, that definition is missing or
cannot be parsed, so what its job runs with is unknown. That is the finding:
reinstall it from `/opt/homelab/ops/launchd` and redo its settings as the
update runbook says for a changed definition, rather than diagnosing around it.

Then run the diagnosis once with the supervisor's settings and once with the
loop's (`tick`). Both send requests to the local model, and each definition is
edited on its own, so one passing says nothing about the other:

```sh
lab_setting() {
  v=$(plutil -extract "EnvironmentVariables.$1" raw -expect string -o - "$P/com.homelab.$2.plist") && [ -n "$v" ] && printf '%s\n' "$v" \
    || { echo "STOP: com.homelab.$2 has no usable $1; see runbook step 4" >&2; return 1; }
}
lab_diagnose() {
  KEY=$(lab_setting LAB_OPERATOR_PUBKEY "$1") && URL=$(lab_setting LAB_MODEL_URL "$1") \
    && NAME=$(lab_setting LAB_MODEL_NAME "$1") && BDIR=$(lab_setting LAB_BACKUP_DIR backup) || return 1
  echo "== with the settings of com.homelab.$1"
  sudo -u lab /usr/bin/env LAB_OPERATOR_PUBKEY="$KEY" LAB_MODEL_URL="$URL" LAB_MODEL_NAME="$NAME" \
    LAB_BACKUP_DIR="$BDIR" /opt/homelab/.venv/bin/python -I -m lab.cli --db /var/homelab/lab.db doctor
  sudo -u lab /usr/bin/env LAB_OPERATOR_PUBKEY="$KEY" LAB_MODEL_URL="$URL" \
    /opt/homelab/.venv/bin/python -I -m lab.cli security-audit
}
lab_diagnose supervisor
lab_diagnose tick
```

`sudo` starts the command in a fresh environment (`env_reset`, `man sudoers`),
so the settings are passed through `/usr/bin/env`. A `STOP` line means that
job's definition lacks a setting runbook step 4 gives it, and the diagnosis
for that job does not run. Do not run it with an empty value instead: `doctor`
would print `ok model: not configured` or `ok backup: not configured`, and
`security-audit` would print `loopback: ok`, for a setting it never saw. Redo
that file's part of
[runbook step 4](../ops/runbook-lab-account-and-daemons.md#step-4-settings-the-service-files-need-sudo),
then reload the job as in step 3 below. With no backup disk yet, the backup
folder is still `PASTE_BACKUP_DIR` (INSTALL.md section 6 lets you skip it), so
`backup` fails with `still the placeholder`: that is a true result.

In each pair of runs, the lines that depend on the job are doctor's `model` and
the audit's `public_key` and `loopback`. The others repeat.

**3. Compare with what launchd has loaded.** The values above come from the
files. launchd keeps the definition a job had when it was bootstrapped: editing
the file does not change a loaded job, and `launchctl kickstart` starts the
loaded definition again, so it does not apply an edit either. Read what the two
jobs actually run with in the `environment = {` block of:

```sh
sudo launchctl print system/com.homelab.supervisor
sudo launchctl print system/com.homelab.tick
```

That output is for reading only: Apple's `man launchctl` says it is not an API
and may change. If a value there differs from the file, or after you correct a
file, reload that job as the runbook does. Check first that the queue is idle,
because stopping the supervisor interrupts its work:
`sudo -u lab /opt/homelab/.venv/bin/python -m lab.cli --db /var/homelab/lab.db status | head -3`.
Then run `sudo launchctl bootout system/com.homelab.NAME` and
`sudo launchctl bootstrap system /Library/LaunchDaemons/com.homelab.NAME.plist`,
read `print` again and repeat step 2.

### Reading the results

`doctor` checks the state visible to **the account and environment running
it**. Use the service's `LAB_*` settings (listed in
[INSTALL.md](INSTALL.md#7-first-run), section 7), or a model or backup check
may be marked `not configured` even when the service is configured differently.

- `ok`: the named check passed *as implemented*. For a model or backup
  reported as `not configured`, this does not prove the service works.
- `FAIL`: stop and investigate that named check. The command returns 1 if
  any check fails. Use the remedies below before attempting a deployment.
- `skip`: the check could not be performed in this environment, or does not
  apply. It does not fail `doctor`, but **it does not demonstrate success**.

`security-audit` is a separate read-only check of file owners and modes, the
operator public key and the model URL, with `ok`, `FAIL` and `skip` findings.
It reads `LAB_OPERATOR_PUBKEY` and `LAB_MODEL_URL` from **the environment of
the command**, not from the launchd plists. Without the service's values,
`public_key` fails even on a correct install, and `loopback` reports `ok`
because there is no URL to check, which proves nothing about the service's
endpoint. That is why the checkout run says nothing about the model URL and
step 2 runs the audit once per job. It can also return different findings when
run as the owner or as `lab`: `data` is skipped for an account that cannot see
into the closed data folder, and `private_key` for one that cannot see your
key. Use `--details` only locally: it prints paths for failed findings. The
command does not repair them.

If doctor says `the audit chain breaks at event N`, the chain was read and
failed verification: treat the database and logs as **evidence**. Stop
automated work through the documented operator controls, preserve the
database, and follow the verified backup/restore runbook. Do not edit the
audit chain, reset the queue or blindly rerun non-idempotent tasks. `cannot
read the audit log` is different: the chain was not checked at all (see the
`audit_chain` row).

## Interpret common checks

A deployed build prints only the checks it has. Doctor's `versions`,
`lab_account`, `permissions` and `services` came after `5105422`, the
deployment [STATUS.md](STATUS.md) records (#416).

| Output or symptom | First read-only check | Next action or reference |
|---|---|---|
| `FAIL database`: missing, unreadable or schema mismatch | Confirm which `--db` path, account and interpreter you used. In a checkout before the first run, `no database at` is expected (see Start here). On the deployed Mac, `cannot read ... as a lab database` from the owner account is expected: run it as `lab`, as in Start here. | A missing DB is not repaired by touching a file. If the deployed build says the schema is behind, do not follow its hint to run `lab tasks`: opening the database migrates it, and a migration can be undone only from a backup. Follow the [update runbook](../ops/runbook-lab-account-and-daemons.md#updating-the-deployed-code), which takes a backup, stops the jobs that write the database, and then runs `sudo -u lab /opt/homelab/.venv/bin/python -m lab.cli --db /var/homelab/lab.db migrate --check`. That tries the new migrations on its own private copy and leaves the database unchanged. A newer schema needs the build that wrote it. |
| `FAIL operator_key` | Check that the public key exists at the configured path and cannot be written by other accounts. | Follow [INSTALL.md](INSTALL.md), sections 4 and 5. Never give `operator.key` to the `lab` account or disable signature enforcement. |
| `FAIL model` | Note which job's settings the failing run used (step 2 runs one per job). Check the server window and its loopback `GET /models` response. `doctor` checks the configured model ID against that list. | Start the locally pinned model in [INSTALL.md](INSTALL.md), section 8, and correct `LAB_MODEL_NAME` in that job's definition (runbook step 4 reads it from the server), then reload the job as in step 3. Do not change the listener to `0.0.0.0`. |
| `FAIL disk` | Check available space on the database volume. Doctor's minimum is 5 GB. | Free space without deleting lab state. The install guide asks for more free space initially because model weights also use disk. |
| `FAIL backup` | Confirm `LAB_BACKUP_DIR` in the backup job's definition is an absolute directory with a recent manifest. Doctor flags backups older than 36 hours. `still the placeholder` means the backup folder block of runbook step 4 was skipped, which INSTALL.md section 6 allows until there is a backup disk. | Check [INSTALL.md](INSTALL.md), section 9, and the backup launcher [runbook](../ops/runbook-lab-account-and-daemons.md#moving-the-backups-full-disk-access-to-its-launcher-287-once). A normal Terminal command may lack the launcher's Full Disk Access. Do not broadly grant Full Disk Access to the shared Python interpreter. |
| `FAIL selftest` | Read what the scheduled run reported, which runs nothing: `sudo tail -n 20 /var/log/homelab/selftest.log /var/log/homelab/selftest.err`. Until the first 03:17 run, doctor says `no selftest has been recorded yet`. | To run it again, start the scheduled job itself, as a separate step from reading its log: `sudo launchctl kickstart system/com.homelab.selftest`. It runs as `com.homelab.selftest.plist` defines it (`plutil -p` shows it): as `lab`, with `/opt/homelab/.venv/bin/python`, `--db /var/homelab/lab.db` and its alert settings. It is not read-only: it adds its result to the database's event log and sends it through the alert channel. A bare `lab selftest` is a different run, because its default database is not `/var/homelab/lab.db`. Read the log again afterwards, and see [INSTALL.md](INSTALL.md), section 7, and the runbook. |
| `FAIL audit_chain` | Read the whole line. `cannot read the audit log` means the chain was not read at all, for example because there is no database at that path: resolve the `database` line first (which `--db`, which account, whether the file exists). Only `the audit chain breaks at event N` is a chain that was read and failed verification. | For a break at event N, preserve the failing database and do not attempt to rewrite history. Refer to [backup restoration](../ops/runbook-lab-account-and-daemons.md#putting-a-backup-back-into-service) and [incident records](INCIDENTS.md) before recovery. Never restore over a database whose chain was only unreadable. |
| doctor `FAIL services` or `skip services` | A skip means `launchctl` could not inspect a job: check it with `sudo launchctl print system/com.homelab.NAME`. For a FAIL, read the labels after `not loaded:`. Before runbook step 5 every job is listed. After it, two can be listed on a correct install, because setup installs every definition: `com.homelab.chat`, which stays unloaded until the pairing step in [ops/mac-mini-setup.md](../ops/mac-mini-setup.md#23-chat-through-the-broker-and-retiring-the-raw-shell-bot-239) section 23, and `com.homelab.weekly-eval`, which is optional and needs its four `PASTE_` values first. | Do not bootstrap `chat` or `weekly-eval` just to clear this FAIL: start each only through its own procedure. Any other label is a required job that is not loaded: follow the [services section](../ops/runbook-lab-account-and-daemons.md#step-5-start-the-services-one-at-a-time-sudo) for it. Do not change launchd files just to remove a skip. A security-audit `services` FAIL is about file owners: see the next row. |
| `FAIL permissions`, `FAIL lab_account`, or a security-audit `deploy`, `services`, `config`, `data`, `public_key` or `private_key` FAIL | Compare with the stage table in Start here first. Then list the paths with `--details` and look at each with `ls -ld`. | A `deploy` or `services` FAIL after deployment: follow step 1 of "On the deployed Mac", and do not run the deployed build as yourself or as root. A `config` FAIL naming only `/etc/homelab/heartbeat-url` or `/etc/homelab/telegram-alert.json`, each a regular file owned by `lab` with mode `-rw-------`, from a deployed build older than #404 (such as `5105422`): a known bug in that build's audit (#403), not a permission fault ([STATUS.md](STATUS.md#waiting-on-the-owner-needs-sudo-hardware-or-a-decision), item 3). Leave the files as they are, since jobs running as `lab` read them, and run the audit again after the next update. Anything else: follow the [lab account isolation steps](../ops/runbook-lab-account-and-daemons.md#step-2-the-lab-account-directories-and-service-files-sudo). Do not use broad `chmod -R` or make `lab` an administrator. |
| security-audit `loopback: FAIL` | Note which job's run failed: each run in step 2 uses one job's `LAB_MODEL_URL`, so check both even when one passes. Its host is not `127.0.0.1`, `localhost` or `::1`. Compare it with what the loaded job uses (step 3). | Set `LAB_MODEL_URL` in that definition to the loopback address of the local model server, `http://127.0.0.1:8080/v1` in [runbook step 4](../ops/runbook-lab-account-and-daemons.md#step-4-settings-the-service-files-need-sudo): `sudo plutil -replace EnvironmentVariables.LAB_MODEL_URL -string http://127.0.0.1:8080/v1 /Library/LaunchDaemons/com.homelab.NAME.plist`, then `sudo plutil -lint` that file. Reload the job with `bootout` and `bootstrap` as in step 3 (a `kickstart` keeps the old value), read `print` and repeat step 2. Never move the model server onto the LAN to make it reachable. |
| `FAIL versions` | Read doctor's own line: it names the Python that ran it. To ask that interpreter directly, use the path of the failing command: `"$REPO/.venv/bin/python" -V` in a checkout, `sudo -u lab /opt/homelab/.venv/bin/python -V` on the deployed Mac. The project requires Python 3.13 or later (`requires-python` in `pyproject.toml`). | In a checkout, build the environment as INSTALL.md section 3 says (`uv sync --locked`), which uses a Python that uv manages. On the deployed Mac the environment comes from runbook step 3 and the update runbook. Do not replace or upgrade the system Python to make the check pass. |
| Supervisor will not start | Inspect its local error log and verify the public-key setting and deployment state. | [INSTALL.md](INSTALL.md), Troubleshooting. Do not use development-only unsigned flags on the deployed Mac. |
| Model runs out of unified memory | Check the server's cache size and other apps' memory use. | The measured 32 GB setup requires `--prompt-cache-size 1`. Follow the model selection limits in [INSTALL.md](INSTALL.md); the 16 GB and 64 GB tiers are not validated. |
| Update or rollback is needed | Review the current deployed commit, available backup, and queued work first. | Use `lab update --plan COMMIT` only with a known full commit hash. It **prints** the operator steps; it does not apply them. [The runbook](../ops/runbook-lab-account-and-daemons.md#updating-the-deployed-code) distinguishes rollback before and after a database migration. |

## When not to continue

Stop before any new privileged operation, external connector or release if an
operator signature is missing, any audit-chain check fails, an unexpected
permission change is observed, or a restore has not been verified. A passing
CLI command is not a substitute for the end-to-end drills on the target Mac.

The owner-only follow-ups and the exact deployed commit known to the project
are tracked in [STATUS.md](STATUS.md). This guide is not proof those steps
have been performed. In particular, #70 (database/account boundary), #77
(power/FileVault) and #91 (recovery drills) remain separate acceptance gates.

## Share a diagnostic report without exposing data

Keep raw command output on your own Mac. Before sharing even a short report:

1. Record the affected **check names and outcomes**, not complete logs.
2. Include the non-sensitive operating-system major version, memory tier,
   expected versus observed behavior, and the Git commit identifier.
3. Replace local usernames, home directories and machine-specific paths with
   placeholders. Do not publish database paths that expose identity.
4. Remove tokens, authorization headers, bot and chat IDs, private/public-key
   contents, signed approvals, model request payloads, raw task text,
   customer content, secrets and callback URLs. Never upload the database,
   `.env`, operator-key files, `launchctl print` output or the complete service
   configuration.
5. If a security boundary might be involved, use the reporting instructions
   in [SECURITY.md](../SECURITY.md) instead of posting exploit detail to a
   public issue.

Do not assume that a command is safe to share merely because it was read-only.
Read-only diagnostics can still disclose sensitive paths and metadata.

## Fresh-install verification

Only an actual test on a separate, suitable Apple silicon Mac or fresh user
can finish [#188](https://github.com/roshanaryal1/home-lab/issues/188).
Follow [ops/install-validation.md](../ops/install-validation.md) in its existing
order and fill out the [validation record
template](../ops/install-validation-template.md). Mark skipped steps as
not demonstrated. No CI result or review of this document replaces that test.
