# Installer design: a signed one-line install that stops before sudo

Issue [#385](https://github.com/roshanaryal1/home-lab/issues/385). Written 2026-10-09 (NZDT).

This is a design for the owner to decide. No installer exists yet. The facts come from the
sources in section 10, each with the date it was read. A fact that comes only from a search
result is marked "(search summary)". A fact that I could not confirm is marked "(unverified)".

The plan this builds on is row 15 of [FEATURE-PLAN.md](FEATURE-PLAN.md). The steps it replaces
are in [INSTALL.md](INSTALL.md).

## 1. What this is

`install.sh` is a versioned, signed script for a Mac with Apple silicon. It downloads one tagged
release of home-lab, checks it, sets up the Python environment as the user, and prints the steps
that need `sudo`. The owner reads those steps and runs them. The script never runs `sudo`.

It replaces the prerequisite check and the clone and environment steps in sections 1 and 3 of
[INSTALL.md](INSTALL.md), for a user who wants one entry point. It does not change the runbook in [ops/runbook-lab-account-and-daemons.md](../ops/runbook-lab-account-and-daemons.md).
Every system change stays with the owner.

The target is macOS 14 or later on Apple silicon. MLX needs macOS 14.0 or later, as
[scripts/check-prerequisites.sh](../scripts/check-prerequisites.sh) already states. Intel Macs
are refused.

## 2. What it does, step by step

1. **Checks, before any download.** Apple silicon (`uname -m` prints `arm64`), macOS 14 or later
   (`sw_vers`), at least 30 GB free on the volume that holds `$HOME`, and unified memory
   (`sysctl -n hw.memsize`). The memory check prints the same model tier as
   `scripts/check-prerequisites.sh`. A 16 GB Mac gets a warning that its tier is untested
   (open question 6).

2. **uv at user level, if missing.** If `uv` is not on the PATH, the script runs uv's standalone
   installer at the version the script pins. The current version is 0.12.24, released
   2026-10-08. The script sets `UV_NO_MODIFY_PATH=1`, so the installer does not edit shell
   profiles. uv's installer reference says uv goes to the user executable directory by default.
   The directory is `~/.local/bin` according to a search summary (unverified on the official
   page).

3. **Download one tagged release.** The script downloads the lab archive and a `SHA256SUMS` file
   from the GitHub release for the version the script was built for. It never reads a branch.

4. **Verify.** It checks GitHub artifact attestations on the archive (section 4). Then it checks
   the archive's SHA-256 against `SHA256SUMS`. Any failure stops the install and deletes the
   partial download.

5. **Unpack as the user.** It unpacks the archive into `$HOME/home-lab`. If that folder already
   exists, the script stops and names the folder. Section 6 covers what the owner does then.

6. **Python environment as the user.** It runs `uv sync --locked` in that folder.
   [pyproject.toml](../pyproject.toml) sets `python-preference = "only-managed"`, so uv fetches
   its own Python 3.13 and does not use the Mac's Python.

7. **Plan, without applying.** It runs `uv run python -m lab.cli setup-plan` with no `--apply`.
   `build()` in [lab/accountplan.py](../lab/accountplan.py) executes nothing. `apply()` runs only
   with `--apply`, as root. The default operator key path is `~/.lab-operator/operator.pub`. The
   plan does not read that file, so it prints before the key exists.

8. **Doctor, as a checklist.** It runs `uv run python -m lab.cli doctor`. On a fresh Mac, doctor
   reports FAIL for the missing database and the missing operator key. The checks that read the
   database, the selftest check among them, also fail, because there is no database to read.
   Then doctor exits 1. The default database path is `~/.local/share/home-lab/lab.db` (`lab/cli.py`).
   The installer shows each line and continues. It does not treat doctor's exit code as a
   failure.

9. **Print the sudo steps, then stop.** The script prints the system steps in order. Each step
   has its command and the result to expect. The steps cover the account and folders from
   `setup-plan --apply`, the runbook's deploy, settings, start and backup steps, and the
   operator key command from INSTALL.md section 4. The owner runs that key command, because the
   key belongs to the owner. The script prints the runbook path and stops. It prints the
   commands and does not run them.

The model server in INSTALL.md section 8 is not part of the script. It is a download of about
16 GB that the owner starts in a second window.

## 3. What it never does

- **It never runs `sudo`.** Not for a check, a download or a printed step. The test in section 8
  puts a `sudo` shim on the PATH and fails if anything calls it.
- **It sends no telemetry.** The script has no analytics call and no beacon.
- **It makes few network calls.** The script contacts GitHub for the release and the
  attestation, and it contacts the uv installer's host. It calls no other home-lab host.
  Two calls are not in the rule that this design was given. `uv sync` downloads the managed
  Python and the locked packages from their indexes, and `gh attestation verify` calls GitHub's
  API unless it can check a downloaded bundle offline (search summary, to be tested). Open
  question 3 asks whether to name both calls.
- **It changes nothing outside `$HOME` and the folders the printed steps name.** The printed
  steps name `/Users/lab`, `/var/homelab`, `/var/log/homelab`, `/etc/homelab`, `/opt/homelab`
  and `/Library/LaunchDaemons`. Those paths appear only as printed text.
- **It edits no shell profile.** uv runs with `UV_NO_MODIFY_PATH=1`. The Hermes Agent script
  does append PATH lines to shell profiles (section 7). This design does not.
- **It reads, writes and prints no secret.** It does not create the operator key.
- **It starts no service and loads no launchd job.**
- **It installs nothing system-wide.** It runs no `npm install -g`, no Homebrew install and no
  system package manager. It requires `gh` and `shasum` to be present, and it says so if not.
- **It writes no database.** Doctor opens the database read-only if the file exists, and it
  never migrates it (`lab/doctor.py`).

## 4. Verification

The table compares four ways to check the download. Each row names what the user needs, what the
check proves and how it fails.

| Option | What the user needs installed | What it proves | How it fails |
|---|---|---|---|
| SHA-256 pinned in the script | `shasum`, which ships with macOS (unverified here) | The download matches the hash written in the script. Hermes Agent pins the SHA-256 of its uv download the same way and aborts on a mismatch (its script, read 2026-10-09). | A mismatch aborts the install. The hash sits in the same script as the code, so a changed script can carry a new hash. This check covers the download, not the script. |
| minisign, public key in the docs | `minisign`, listed under Homebrew in its README (read 2026-10-09). The latest tag is 0.12 (page read, year not shown). | The archive was signed with the project's minisign secret key. | Prints "Signature verification failed" (search summary). The exit code is unverified. A lost or leaked secret key needs a new key in the docs. The owner would keep the secret key on one machine. |
| cosign, keyless (Sigstore) | `cosign`. The latest tag is v3.1.3 (page read, year not shown). The README says the project supports the latest release and the last v2 release. Network access to Sigstore services (search summary, unverified). | The archive was signed by a GitHub Actions workflow of this repository. The check pins `--certificate-identity` and `--certificate-oidc-issuer` (README, read 2026-10-09). | An identity mismatch makes `verify-blob` fail. The exact exit code and text are unverified. Cosign before 1.12.0 skipped some identity checks (search summary). The design requires cosign v3. |
| GitHub artifact attestation (`gh attestation verify`) | `gh`, from Homebrew or a release download (unverified which). The check needs gh 2.67.0 or later (search summary, about an exit-code bug). It calls GitHub's API unless it checks a downloaded bundle (search summary, to be tested). | The archive was built by a workflow of roshanaryal1/home-lab. The signer workflow can be pinned with a flag (search summary). uv's 0.12.24 release uses this mechanism (release page, read 2026-10-09). | A missing or mismatched attestation makes the command fail. Older `gh` may exit 0 when nothing matches (search summary). The installer requires gh 2.67.0 or later and tests the failing case on that version. |

**Recommended: GitHub artifact attestations.** The reasons:

- It needs no long-lived key. The owner holds no signing key that must be guarded, rotated or
  recovered. The signer is the release workflow in this repository.
- The release workflow makes the attestation, so no person signs each release by hand.
- uv publishes its releases the same way (release page, read 2026-10-09). The owner can point to
  one known practice.

The costs are real. The owner must add a release workflow that creates the attestation, with its
actions pinned to commit SHAs, as `.github/workflows/check.yml` already requires. The verifier
needs `gh`, which INSTALL.md does not list yet. GitHub becomes the trust root for the download.
minisign is the fallback if the owner wants a key that does not depend on GitHub (open question 1).

The SHA-256 check stays in the script under any option. It is not a signature. It catches a
damaged download.

## 5. Reading before running

The docs show the download, verify, read and run order first. The one-line form comes second,
with a warning.

```sh
VERSION="PASTE_THE_RELEASE_VERSION"
BASE="https://github.com/roshanaryal1/home-lab/releases/download/v$VERSION"
curl -fLO "$BASE/install.sh"
gh attestation verify install.sh --repo roshanaryal1/home-lab
less install.sh
sh install.sh --dry-run
sh install.sh
```

Reading `install.sh` covers the installer. The lab's code arrives in a second archive, which the
script verifies and keeps under `$HOME`. The owner can list that archive's contents with
`tar -tzf` before the run.

The one-line form runs the script without reading it and without any check on the script:

```sh
curl -fsSL "https://github.com/roshanaryal1/home-lab/releases/download/vPASTE_THE_RELEASE_VERSION/install.sh" | sh
```

Use the one-line form only for a version that someone has already checked with the steps above.

## 6. Upgrade and uninstall

**Upgrade.** The installer upgrades nothing. If `$HOME/home-lab` exists, the installer stops,
names the folder and points to `lab update --plan COMMIT` (#370, merged in #372). That command
prints the runbook's "Updating the deployed code" steps for the standard install at
`/opt/homelab`, with the deployed commit, the new commit and the installed jobs filled in. It
runs no `sudo`. The owner runs the printed steps.

**Uninstall.** `sh install.sh --uninstall` prints the steps from INSTALL.md section 10, including
the `sudo` commands. It also lists the user-level paths: the clone, the model cache and, only if
the owner wants to lose the operator key, `~/.lab-operator`. It removes nothing. The owner runs
the printed steps, as at install time.

## 7. How OpenClaw and Hermes Agent do it

**OpenClaw.** The README gives `curl -fsSL https://openclaw.ai/install.sh | bash` for macOS
(read 2026-10-09). The newest release is v2026.9.9. Its asset timestamps read 2026-10-08
(releases page, read 2026-10-09). The script in the repository, `scripts/install.sh`, was read
for its first 1,000 of 3,618 lines. It installs Node when needed, through the Homebrew node
formula on macOS. It installs the package with `npm install -g`. It uses `sudo` for Linux
package managers, and it can start onboarding. Each release asset lists a
sha256 on the releases page, and the tag is signed by the committer. The `openclaw.ai` copy of
the script was not reachable on 2026-10-09, so the served script is not compared with the
repository copy (unverified).

**Hermes Agent.** The README gives `curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash`
for macOS and Linux (read 2026-10-09). The newest release is v0.21.6, dated 2026-10-08
(releases page, read 2026-10-09). Its script, `scripts/install.sh`, was read in part. It installs
a checkout under `$HERMES_HOME/hermes-agent`, where `$HERMES_HOME` defaults to `~/.hermes`. It
checks its uv tarball against a SHA-256 pinned in the script, and it aborts on a mismatch. It
appends a PATH line to the shell profile files, and it writes a local install receipt. Its
comments say the installer itself sends nothing. The part read shows `sudo` only on Linux paths.
The macOS path was not in the part read.

**What home-lab does differently.** It installs one tagged release, with a provenance check
against that release. Both one-line forms above run a script served from a web host, and the
sources read do not show a check the user can run on that script. The design also:

- never runs `sudo`, and prints the system steps for the owner instead,
- uses no global npm package, no Homebrew install and no profile edit,
- sends no telemetry,
- stops before the model download and before the operator key.

## 8. Tests

- **shellcheck in CI.** A new step in `.github/workflows/check.yml` runs
  `shellcheck --shell=sh scripts/install.sh`. The pinned version is shellcheck v0.11.0. On
  2026-10-09 its GitHub release page marked v0.11.0 as the latest release, with a changelog date of
  2025-08-03. The
  binary is fetched from a pinned URL and checked against a pinned SHA-256, the same way the
  CI actions are pinned to commit SHAs. A newer tag would need a new pin. No newer tag appeared
  in the search results (search summary).
- **Dry-run mode.** `--dry-run` prints every step, makes no change and makes no network call.
  A pytest case runs the script under `/bin/sh` with a temporary PATH. The PATH holds shims for
  `sudo`, `curl` and `gh`, and shims that answer `uname`, `sw_vers`, `sysctl` and `df` for a
  simulated Mac. The case asserts three things: the `sudo` shim was never called, no `curl` or
  `gh` call happened, and the printed steps match a fixed list.
- **Verification failure cases.** A flipped byte in the archive must fail the SHA-256 check. A
  wrong attestation must fail the `gh` check on gh 2.67.0 or later. The attestation case needs a
  real attested release, so it runs against a test release, not the owner's.
- **Fresh macOS user before beta.** The checklist in
  [ops/install-validation.md](../ops/install-validation.md) runs from a new macOS user account on
  Apple silicon. The result is recorded there. #188 names the target: install to the first signed
  task in under 30 minutes, measured on three people's Macs ([MARKET-2026.md](MARKET-2026.md)).
- **Doc checks.** The design's code blocks have no comments at the end of a line, as
  `tests/test_doc_shell_blocks.py` requires. Its links resolve, as `tests/test_doc_links.py`
  requires.

## 9. Open questions for the owner

1. Verification: GitHub artifact attestations (recommended in section 4), or a minisign key the
   owner holds?
2. Trust root: is it acceptable that GitHub, and for cosign Sigstore, is the trust root for the
   download?
3. Network: the rule for this design allows the release download and uv's installer only. Should
   the design name `gh attestation verify` (GitHub's API, unless an offline bundle works) and
   `uv sync` (PyPI and the Python index) as allowed calls?
4. uv: run uv's own installer at a pinned version, as section 2 says, or have home-lab check uv's
   archive itself against the SHA-256 and attestation that uv publishes? uv's installation page
   does not describe a checksum step (unverified).
5. Clone location: a fixed `$HOME/home-lab`, or a folder the owner chooses?
6. Memory: refuse a 16 GB Mac, warn, or let the owner choose? INSTALL.md marks 16 GB untested.
7. Disk: keep 30 GB, as INSTALL.md and `scripts/check-prerequisites.sh` say? `lab/doctor.py` uses a
   5 GB floor (`MIN_FREE_BYTES`) for the database volume. The two numbers serve two purposes.
   Which one does the installer use?
8. Doctor: confirm that the installer shows doctor's lines and does not stop on them.
9. Operator key: confirm that the script never creates the key.
10. Release host: GitHub release assets only, or a domain for the one-line form? OpenClaw and
    Hermes Agent both use their own domains (section 7).
11. Release workflow: approve a new workflow that creates the attestation, with its actions pinned
    to commit SHAs and checked by zizmor, as `check.yml` does now.
12. Scope: Apple silicon only, with Intel refused? Confirm.
13. `gh`: is installing `gh` acceptable for the owner's users? If so, INSTALL.md section 1 needs
    a line for it.

## 10. Sources

Repository files were read at `origin/main`, commit `1689d58`, on 2026-10-09:
`docs/INSTALL.md`, `docs/FEATURE-PLAN.md`, `docs/MARKET-2026.md`, `docs/COMPARISON.md`,
`lab/accountplan.py`, `lab/doctor.py`, `lab/cli.py`, `scripts/check-prerequisites.sh`,
`ops/runbook-lab-account-and-daemons.md`, `ops/release.md`, `ops/install-validation.md`,
`pyproject.toml`, `.github/workflows/check.yml`, `SECURITY.md`. Section 6 was updated from
`lab/update_plan.py` at commit `72b3bc3`, after #372 merged.

Web sources:

1. OpenClaw README, https://github.com/openclaw/openclaw, read 2026-10-09.
2. OpenClaw install script, `scripts/install.sh` (first 1,000 of 3,618 lines),
   https://github.com/openclaw/openclaw/blob/main/scripts/install.sh, read 2026-10-09.
3. OpenClaw releases (v2026.9.9, asset dates 2026-10-08),
   https://github.com/openclaw/openclaw/releases, read 2026-10-09.
4. Hermes Agent README, https://github.com/NousResearch/hermes-agent, read 2026-10-09.
5. Hermes Agent install script, `scripts/install.sh` (part read),
   https://github.com/NousResearch/hermes-agent/blob/main/scripts/install.sh, read 2026-10-09.
6. Hermes Agent releases (v0.21.6, dated 2026-10-08),
   https://github.com/NousResearch/hermes-agent/releases, read 2026-10-09.
7. uv installation guide, GitHub copy (the docs.astral.sh host did not resolve on 2026-10-09),
   https://github.com/astral-sh/uv/blob/main/docs/getting-started/installation.md, read 2026-10-09.
8. uv installer reference, https://github.com/astral-sh/uv/blob/main/docs/reference/installer.md,
   read 2026-10-09.
9. uv 0.12.24 release (released 2026-10-08, attestations and `.sha256` files),
   https://github.com/astral-sh/uv/releases/tag/0.12.24, read 2026-10-09.
10. minisign README, https://github.com/jedisct1/minisign, read 2026-10-09.
11. minisign 0.12 release (year not shown on the page),
    https://github.com/jedisct1/minisign/releases/tag/0.12, read 2026-10-09.
12. minisign failure output and exit code, search summary, 2026-10-09. Result listed at
    https://docs.rs/minisign-verify. The exit code is unverified.
13. cosign README, https://github.com/sigstore/cosign/blob/main/README.md, read 2026-10-09.
14. cosign v3.1.3 release (year not shown on the page),
    https://github.com/sigstore/cosign/releases/tag/v3.1.3, read 2026-10-09.
15. cosign verification docs, search summary, 2026-10-09. Page listed at
    https://docs.sigstore.dev/cosign/verify, not reachable on 2026-10-09.
16. cosign advisory GHSA-fx35-mq7g-6g98 on legacy bundles, search summary, 2026-10-09,
    https://scout.docker.com/vulnerabilities/id/GHSA-fx35-mq7g-6g98.
17. `gh attestation verify` manual, search summary, 2026-10-09. Page listed at
    https://cli.github.com/manual/gh_attestation_verify, not reachable on 2026-10-09.
18. shellcheck latest release (v0.11.0, dated 2025-08-03 on the page),
    https://github.com/koalaman/shellcheck/releases/latest, read 2026-10-09.
19. shellcheck 0.11.0 in a 2026 distribution package, search summary, 2026-10-09,
    https://archlinux.org/packages/extra-staging/x86_64/shellcheck/. No newer tag appeared.

Not read on 2026-10-09: `https://openclaw.ai/install.sh` and `https://hermes-agent.nousresearch.com/install.sh`
(not reachable from the research environment). The GitHub API for these repositories was not
enabled for this session, so the releases were read from their web pages.
