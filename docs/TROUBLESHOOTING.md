# Troubleshooting an installation safely

This is a companion to [INSTALL.md](INSTALL.md) and the deployed-Mac
[runbook](../ops/runbook-lab-account-and-daemons.md). It describes diagnosis,
not a successful install test. The remaining fresh-account acceptance check is
[#188](https://github.com/roshanaryal1/home-lab/issues/188).

**Safety rule:** start with read-only commands. Do not make the agent an admin,
disable FileVault, remove operator-key checks, put the model server on the LAN,
turn off the sandbox or bypass a failed audit to make a check green. Never run
cleanup or restore commands against data you have not backed up.

## Start here: collect a local diagnosis

From your repository checkout, with the Python environment installed:

```sh
uv run python -m lab.cli --version
uv run python -m lab.cli --db /var/homelab/lab.db doctor
uv run python -m lab.cli security-audit
```

The database path above is for the **deployed** installation only. Before
deployment, follow [INSTALL.md](INSTALL.md), sections 1 to 5, and do not
create `/var/homelab` just to satisfy a check. The default database for a
development checkout is different. If you run a command without its
intended `--db` value, it can inspect the wrong database.

`doctor` checks the state visible to **the account and environment running
it**. Use the service's `LAB_*` settings (listed in
[INSTALL.md](INSTALL.md), section 7), or a model/backup check may be marked
`not configured` even when the service is configured differently.

- `ok`: the named check passed *as implemented*. For a model or backup
  reported as `not configured`, this does not prove the service works.
- `FAIL`: stop and investigate that named check. The command returns 1 if
  any check fails. Use the remedies below before attempting a deployment.
- `skip`: the check could not be performed in this environment, or does not
  apply. It does not fail `doctor`, but **it does not demonstrate success**.

`security-audit` is a separate read-only permission and ownership check,
with `ok`, `FAIL` and `skip` findings. It can return different findings
when run as the owner or as `lab`. Use `--details` only locally: it prints
paths for failed findings. The command does not repair them.

If the audit chain fails, treat the database and logs as **evidence**. Stop
automated work through the documented operator controls, preserve the
database, and follow the verified backup/restore runbook. Do not edit the
audit chain, reset the queue or blindly rerun non-idempotent tasks.

## Interpret common checks

| Output or symptom | First read-only check | Next action or reference |
|---|---|---|
| `FAIL database`: missing, unreadable or schema mismatch | Confirm which `--db` path and deployed commit you used. | A missing DB is not repaired by touching a file. For an older schema, follow the backed-up [update runbook](../ops/runbook-lab-account-and-daemons.md#updating-the-deployed-code); use `lab migrate --check` on a copy before a real update. A newer schema needs the build that wrote it. |
| `FAIL operator_key` | Check that the public key exists at the configured path and cannot be written by other accounts. | Follow [INSTALL.md](INSTALL.md), sections 4 and 5. Never give `operator.key` to the `lab` account or disable signature enforcement. |
| `FAIL model` | Check the server window and its loopback `GET /models` response. `doctor` checks the configured model ID against that list. | Start the locally pinned model in [INSTALL.md](INSTALL.md), section 8, and correct the configured model name. Do not change the listener to `0.0.0.0`. |
| `FAIL disk` | Check available space on the database volume. Doctor's minimum is 5 GB. | Free space without deleting lab state. The install guide asks for more free space initially because model weights also use disk. |
| `FAIL backup` | Confirm `LAB_BACKUP_DIR` is set to an absolute directory and contains a recent manifest. Doctor flags backups older than 36 hours. | Check [INSTALL.md](INSTALL.md), section 9, and the backup launcher [runbook](../ops/runbook-lab-account-and-daemons.md#moving-the-backups-full-disk-access-to-its-launcher-287-once). A normal Terminal command may lack the launcher's Full Disk Access. Do not broadly grant Full Disk Access to the shared Python interpreter. |
| `FAIL selftest` | Find the most recent `selftest` event and the existing scheduled job report. | Use [INSTALL.md](INSTALL.md), section 7 and the runbook. Run `lab selftest` only in the intended environment after reading its documented scope: it exercises checks and may write its result to the queue. |
| `FAIL audit_chain` | Preserve the failing database and identify the failing event number from doctor. | Do not attempt to rewrite history. Refer to [backup restoration](../ops/runbook-lab-account-and-daemons.md#putting-a-backup-back-into-service) and [incident records](INCIDENTS.md) before recovery. |
| `FAIL services` or `skip services` | Determine whether service inspection was permitted. A skip can simply mean `launchctl` could not inspect a job. | Follow the [services section](../ops/runbook-lab-account-and-daemons.md#step-5-start-the-services-one-at-a-time-sudo) and check the specific service label on the Mac. Do not change launchd files just to remove a skip. |
| `FAIL permissions`, `FAIL lab_account`, or security-audit `FAIL` | Identify the reported concern and inspect it as the owner. | Follow the [lab account isolation steps](../ops/runbook-lab-account-and-daemons.md#step-2-the-lab-account-directories-and-service-files-sudo). Do not use broad `chmod -R` or make `lab` an administrator. |
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
   `.env`, operator-key files or the complete service configuration.
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
