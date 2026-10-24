# Fresh-Mac installation validation record (template)

**Template only.** Copy this file to a separately named, dated record **after**
running [ops/install-validation.md](install-validation.md) on real macOS
hardware. Never fill a passing result from memory or from CI alone. The
remaining acceptance gate belongs to [#188](https://github.com/roshanaryal1/home-lab/issues/188).

## Provenance

- Date (local and UTC if different): **NOT RUN**
- Tester (role or pseudonym, not a private account name): **NOT RUN**
- Hardware: Apple silicon model and memory tier only: **NOT RUN**
- macOS version: **NOT RUN**
- Clean Mac or fresh non-admin macOS user: **NOT RUN**
- Existing lab/deployment on this machine: **NOT RUN**
- Repository commit (full SHA): **NOT RUN**
- `uv --version`, Python version and lockfile state: **NOT RUN**
- Model repository and pinned weight revision (no local path): **NOT RUN**
- Backup volume: present / absent / not tested: **NOT RUN**
- Operator key: created and unreadable to `lab` (never paste contents): **NOT RUN**

A second user on a Mac with existing system tools is not a full clean-Mac
test. A second user on the already-running Mac mini is not an isolated test:
system launchd jobs, paths, account and model port collide.

## Acceptance steps

For each step, record **PASS**, **FAIL**, **SKIP** or **NOT RUN**, a brief
expected/observed comparison, and an evidence reference. Never attach
private keys, URLs with credentials, unredacted logs, signed approvals or
the queue database. If a failure requires privileged repair, record it
as a follow-up rather than changing unrelated files.

| Step | What was tested | Result | Expected vs observed (short) | Redacted evidence or follow-up |
|---|---|---|---|---|
| 1 | Clone, read-only prerequisite checker, architecture/RAM tier | NOT RUN |  |  |
| 2 | Locked environment, CLI help, operator key generation | NOT RUN |  |  |
| 3 | `setup-plan` review: no unexpected paths or model | NOT RUN |  |  |
| 4 | Dedicated `lab` account, public key, six isolation checks | NOT RUN |  |  |
| 5 | Pinned deployment and local model ID from server | NOT RUN |  |  |
| 6 | Installed launchd services, health, task and audit checks | NOT RUN |  |  |
| 7 | Loopback-only server, correct model/revision, successful inference | NOT RUN |  |  |
| 8 | Watchdog, interrupted-task and startup-recovery drills | NOT RUN |  |  |
| 9 | Deliberate teardown without destroying unrelated data | NOT RUN |  |  |

## Safety properties worth explicitly demonstrating

| Property | Result | Evidence reference |
|---|---|---|
| `lab` is not in `admin` or `wheel` | NOT RUN |  |
| `lab` cannot use `sudo` or change its code/service files | NOT RUN |  |
| `lab` cannot read the operator's private key or private home paths | NOT RUN |  |
| Model and status endpoints are loopback only | NOT RUN |  |
| Approval without a valid operator signature is refused | NOT RUN |  |
| An interrupted non-idempotent action is not blindly replayed | NOT RUN |  |
| Backup contains required files and restores into service | NOT RUN |  |
| No real credentials, public sends or unapproved external actions used | NOT RUN |  |

Do not run additional destructive/fault-injection drills simply because this
template names them. Use only the established runbook and an isolated test
installation with an operator present.

## Discrepancies and cleanup

- Exact step(s) whose command differed from the guide: **NONE RECORDED**
- Incorrect instruction or missing prerequisite: **NONE RECORDED**
- Scripts, code or docs changed as a result: **NONE**
- Was the test Mac restored to its expected state? **NOT RUN**
- Remaining manual actions, link to issue(s): **NONE RECORDED**
- Tester's sign-off and date: **NOT RUN**

## Verdict

- [ ] An actual fresh-account or fresh-Mac test was run.
- [ ] Every required check above has evidence, with no unexplained skip.
- [ ] Recovery and permissions were verified on target hardware.
- [ ] Failures have fixes and a rerun record.
- [ ] Issue #188 can be proposed for closure.

Leave the verdict unchecked until the actual Mac test and review are done.
