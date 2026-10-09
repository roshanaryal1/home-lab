# Releasing a citable version (item 8.2, #83)

`CITATION.cff` at the repo root is validated in CI (`tests/test_citation.py`)
and kept in step with `pyproject.toml`. A DOI needs one thing only the
repository owner can do.

## Once, by the owner

- [ ] Sign in to Zenodo with the GitHub account that owns the repository and
      switch the `roshanaryal1/home-lab` toggle on under GitHub integration.
      (The repository is public, which is what Zenodo needs.)
- [ ] Optionally add an ORCID iD under `authors` in `CITATION.cff`.

## Per paper, or per version worth citing

- [ ] Bump `version` in `pyproject.toml` and in `CITATION.cff` together, and
      set `date-released`. The test fails if they differ.
- [ ] Update CHANGELOG.md: move the Unreleased notes under a
      `## [<version>] - <date>` heading, and leave an empty `## [Unreleased]`
      heading above it for the next notes. `tests/test_changelog.py` fails if
      the heading for the version in `pyproject.toml` is missing, or if
      Unreleased is not the first heading.
- [ ] Run the evaluation on the mini (`lab eval run`, section 14 of
      `ops/mac-mini-setup.md`) and commit the sealed record, so the release
      contains the exact code, task set and result.
- [ ] Publish a GitHub release for that tag. Zenodo archives it and mints a
      DOI; paste the DOI badge into the README and add `doi:` to `CITATION.cff`
      in the next commit.

## Versioning and compatibility (draft)

> **Draft.** The owner has not approved this section. Until the owner does,
> it states the intended rules and binds no release.

Versions use `MAJOR.MINOR.PATCH`, as in
[Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html).
`pyproject.toml` and `CITATION.cff` carry the same version. Each release is
recorded in [CHANGELOG.md](../CHANGELOG.md).

Two rules decide what a bump may change:

- **Before 1.0.** Any minor version may break any surface below. A patch
  version may not.
- **From 1.0.** Only a major version may break a surface below. A minor
  version may only add to them.

A change that breaks a surface goes in CHANGELOG.md under **Changed** or
**Removed**, with what a user must do.

How long a version gets fixes: owner to decide.

### Stable surfaces

| Surface | Where it is defined | Patch | Minor | Major |
|---|---|---|---|---|
| CLI commands and flags | The commands in the README's "Operating the lab" section, run as `python -m lab.cli <command>`, with the exit codes that section names | No command, flag or exit code is removed, renamed or changed in meaning. | May add commands and flags. Before 1.0 may also remove or rename one. | May remove, rename or change one. |
| Environment variables | Names that `lab/` reads from or sets in the environment (listed below) | Same as the CLI row. | May add a variable. Before 1.0 may also rename or remove one. | May rename, remove or change the meaning of one. |
| Database schema and migrations | `lab/migrations/`, numbered `NNNN_name.sql`, with the version in `PRAGMA user_version` | No schema change. Shipped migration files are never edited. | Adds new migrations only. Before 1.0 a migration may also rebuild a table. | May drop or rename a table or column. Each such change ships with a migration that upgrades an existing database. |
| Backup manifest | `manifest_version` in `lab/backup.py` (1 at 0.1.0) | No change to the manifest fields. | May add a field. Before 1.0 may also change or remove one. | May change or remove fields and raise `manifest_version`. From 1.0, restore-check still reads the earlier versions. |
| Approval signature format | The Ed25519 signature stored with each approval (`lab/operator.py`, `lab/policy.py`). It is a stable format. | No change. | No change from 1.0. Before 1.0 may change, with a CHANGELOG entry. | May change. The release notes say what happens to approvals signed in the old format. |
| Launchd service definitions | `ops/launchd/` (nine `com.homelab.*` plists). `lab/service.py` builds the definitions. | No change to a label, file name or the command a definition runs. | May add a definition. Before 1.0 may also change a label or command. | May rename a label or change what a definition runs. The runbook step is in the release notes. |
| Eval run record format | `record_version` in `lab/evals.py` (1 at 0.1.0), sealed by `record_sha256` | No change to the fields or to how a record is sealed. | May add an optional field. Before 1.0 may change the fields. | May change the fields and raise `record_version`. Records of the earlier version still load in `lab eval rerun`, or the release notes say why not. |

**Environment variables that `lab/` reads or sets:**

- `LAB_OPERATOR_KEY`: the operator's private key file, read by the commands
  that sign decisions.
- `LAB_OPERATOR_PUBKEY`: the operator's public key file, read by the
  supervisor, the CLI and the MCP and repository commands.
- `LAB_CHAT_ID`: the paired Telegram chat, when `--chat-id` is not given.
- `LAB_BACKUP_DIR`: the backup folder, when `backup --to` is not given.
- `LAB_LOG_DIR`: the folder for the supervisor's rotating JSON logs, when
  `--log-dir` is not given.
- `LAB_MCP_SERVERS`: the MCP server list. The default is
  `/etc/homelab/mcp.json`.
- `LAB_REPO_SOURCES`: the signed repository source list, used by
  `workspace.acquire` and `repo.read`.
- `LAB_WEB_FETCH_HOSTS`: the hosts the web fetch handler may reach.
- `LAB_CONTAINER_IMAGE`: the container image that `skill.run` uses.
- `LAB_MODEL_URL`, `LAB_MODEL_NAME`, `LAB_MODEL_REVISION`,
  `LAB_MODEL_TOKENIZER_REVISION` and `LAB_MODEL_WEIGHTS_MB`: the served heavy
  model.
- `LAB_TARGET`: set to `mac-mini` on the project's Mac mini. `lab/drills.py`
  checks it.
- `LAB_SECRET_<NAME>`: a secret the vault resolves. `<NAME>` is the secret name
  in upper case, with `-` and `.` replaced by `_`. The vault falls back to the
  macOS Keychain when the variable is not set.
- `LAB_ALERT_KIND`: set by `lab/alert.py` for the operator's alert hook, to a
  fixed lowercase word.

**Not covered.** Internal modules in `lab/` that the table does not name, the
wording of log lines and of CLI help text, the dashboard page, the layout of
`tests/` and `docs/`, and the content of recorded evaluation runs.
