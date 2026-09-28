# Security policy

## What this project is

An always-on agent platform that runs on a personal machine with access to a
filesystem, a shell and network egress. The threat model is not incidental to
the project; it is most of the project. Read `docs/PLAN.md` for the design
rationale and `ops/mac-mini-setup.md` for the deployment boundary.

## Reporting a vulnerability

Open a private security advisory through GitHub's "Report a vulnerability"
button on the Security tab, or email <roshanaryaal@gmail.com>. Please do not
open a public issue for anything exploitable.

Expect an acknowledgement within 7 days.

## Repository controls

- `main` is protected: pull requests only, linear history, resolved
  conversations, and the `test`, `workflow-audit` and `secret-scan` CI jobs
  must pass.
- CI actions are pinned to commit SHAs and audited by zizmor.
- Dependabot alerts and security updates are on for `uv.lock` and actions.
- The repository is private, so GitHub secret scanning, push protection
  and CodeQL are not available without Advanced Security. gitleaks scans
  the full history in CI instead. It runs after a push, so it detects a
  leaked secret rather than preventing it.

## Scope

In scope, and taken seriously:

- Sandbox or workspace escape from a task workspace.
- Bypassing the approval gate, replaying or forging an approval token.
- Prompt injection that causes an unauthorized tool call, credential access or
  data egress.
- Privilege escalation from the agent account.
- Any path by which a model output becomes an executed instruction without
  passing policy.

The risk-by-risk mapping to the OWASP agentic top 10, with the control, its
test and the open issue for each gap, is in [THREATS.md](THREATS.md).

## What exists, as of 2026-09-25

Implemented and tested:

- **Policy enforcement** (`lab/policy.py`, issue #9). Four capability
  tiers enforced **per task**, between leasing it and executing it.
  Approvals are bound to a hash of the task's normalised parameters, are
  single use, and expire. **Per tool call** as well (item 1.1, #43): the
  broker takes each tool's tier from a trusted registry and asks policy
  before every call; an approve-tier call needs an approval for that
  exact tool and arguments, and without a policy engine or an audit
  record nothing runs (`tests/test_broker.py`, item 1.1 section).
  Each approval binds an immutable intent (task, tool, arguments, the
  workspace state it acts on, policy version), stored and shown in
  `lab.cli show`; it is consumed by one UPDATE that re-checks grant,
  expiry and hash, so a changed file, a new policy version or an expiry
  at the moment of use voids it (item 1.4, R08).
- **Execution broker** (`lab/broker.py`, issue #10). Workers submit typed
  tool requests instead of touching the filesystem directly. Private
  (0700) per-task workspaces; file tools walk paths by descriptor with
  O_NOFOLLOW in every component, so a symlink, or a directory swapped for
  one, is refused at the moment of use (item 1.5, R09); git hooks and
  shell start-up files cannot be written or deleted; the Seatbelt
  profile is passed inline and never written into the workspace (R10);
  default-deny tool allowlists per task, byte and file-count ceilings,
  and an artifact manifest per execution.
- **Durable task state** with bounded dispatch, lease renewal, atomic
  state transitions (#44), lease tokens checked in the same transaction
  as each write with a generation per claim and a host singleton lock
  (#47, #55, `tests/test_queue.py` item 1.3 section); lease loss or an
  emergency stop cancels the running handler and kills its worker
  process group, the stop revoking broker authority first, and a
  supervisor-side error releases the task instead of stranding it
  (item 1.8, `tests/test_stopping.py`); non-idempotent tool calls are
  journaled before they run, so a retry replays a confirmed outcome and
  holds an unknown one for a person to reconcile (`lab.cli ops`,
  `resolve`) instead of repeating it, and retry budgets count executions,
  not approval waits (item 1.7, `tests/test_journal.py`); and durable
  commits on a SQLite without the WAL-reset bug (item 1.9,
  `tests/test_durability.py`).
- **Audit log** (`lab/audit.py`, item 3.2, #65). `events` refuses UPDATE
  and DELETE by trigger, no longer cascades from task deletion (a task
  with leases or approvals cannot be deleted at all), and every row
  carries a SHA-256 chain over its content and the previous row. Every
  broker call writes one `broker_call` event: tool, parameter hash (never
  the parameters), lease generation, decision and result. `lab audit
  checkpoint` signs the chain head with HMAC-SHA256 under a key file and
  writes it to a directory; `lab audit check` then proves the live log
  still contains the checkpointed row, which catches a rewrite of the
  whole chain that a bare chain cannot (`tests/test_audit.py`). Limits,
  stated plainly: triggers and chain stop accidents and edits, not
  someone who can drop the trigger and also reach the key and the
  checkpoint directory. Until the separate operator account exists
  (#70) the key and checkpoints must be kept off the lab account by
  hand. Events from before migration 3 are kept but not chained.
- **Content-addressed artifacts** (`lab/artifacts.py`, item 3.3, #66).
  Before a task can be recorded as succeeded, every regular file in its
  workspace is copied into an immutable store keyed by SHA-256 (temp file,
  fsync, rename to a read-only blob, deduplicated) and described in
  `artifacts` (path, hash, size, type, task, attempt, producing build,
  lineage). Symlinks, devices, sockets and FIFOs are found by `lstat`,
  opened only with `O_NOFOLLOW` and re-checked with `fstat`, so a file
  swapped for a link between listing and reading reads nothing; they are
  refused, audited, and do not fail the task. A file over 64 MiB, more
  than 2000 files, or any I/O error fails the task instead of dropping
  output silently. `lab artifacts verify` re-hashes every blob and is what
  a restore drill runs (`tests/test_artifacts.py`). Artifacts are not yet
  encrypted or size-quota'd across tasks.
- **Rule of Two** (`lab/authority.py`, item 4.1, #68, ADR 0006). A task
  that would hold untrusted input, a secret and an external action at
  once is cancelled before its handler runs, with no approval offered.
  The secret and external legs come from trusted registration and a
  fixed tool table (an unclassified tool counts as external); the
  untrusted leg is the task's derived `tainted` flag (item 4.2). No
  external tool or secret exists yet, so the rule is proven by a
  deliberate test, not yet by production traffic.
- **Origin and authority ceilings** (`lab/origin.py`, `lab/untrusted.py`,
  item 4.2, #69). Every task records source type, id, content hash,
  acquisition time, sensitivity and delegation. `tainted` and
  `sensitivity` are derived, not supplied: only an operator-sourced task
  is untainted, a child of a tainted task is tainted whatever it claims,
  sensitivity only rises through lineage, and a task with no recorded
  origin is tainted. A tainted task's payload cannot carry a grant,
  destination, policy or tier key. A result carries a `_provenance`
  stamp the handler cannot forge. Untrusted text reaches later stages
  only as fixed-schema `Evidence` (bounded excerpt, control and bidi
  characters stripped, never parsed). `lab show` prints the origin, so an
  approval cannot hide it. Limit: whoever can call `add_task` can still
  assert an operator origin; separating that caller from the agent is
  #70. Memory and summary lineage arrive with #85.
- **Egress gateway** (`lab/egress.py`, item 4.3, #14). `net.fetch` is
  default-deny: a task's allowed hosts are set at handler registration,
  never by the task, and none means no network. https and port 443 only,
  DNS names only (no spelling of an IP literal can match), no
  credentials in URLs. The name is resolved once per hop; every returned
  address must be globally routable (loopback, private, link-local
  including 169.254.169.254, CGNAT, multicast, reserved and IPv4-mapped
  forms are refused, and one bad address refuses the whole answer); the
  connection then goes to that validated address with the name used only
  for TLS and Host, so a rebinding server gets no second lookup.
  Redirects are never followed by the transport: each hop is fully
  re-validated, at most three. Responses are size-capped, sent without
  content encoding, and returned as fixed-schema `Evidence`. Every
  attempt is audited by host and URL hash (never the query string), and
  an audit failure stops the request. `net.fetch` counts as an external
  action for the Rule of Two (`tests/test_egress.py`). Not covered: the
  server's TLS certificate is verified against the system store only,
  nothing limits total bytes per task across calls, and the shell tool's
  network access is governed by the sandbox profile, not this gateway.
- **Secret broker and connectors** (`lab/vault.py`, `lab/connectors.py`,
  item 4.4, #15). A connector is one destination: one host, one secret
  name, one header, the methods and path prefix allowed. Trusted code
  defines and grants them; a task cannot invent a destination, widen a
  path or choose another secret. `connector.call` is approve tier, so a
  person sees the exact connector, path and body (never the secret);
  malformed, out-of-scope or ungranted calls are refused before anyone
  is asked. The broker resolves the secret from `LAB_SECRET_<NAME>` or
  the Keychain at the moment of the call, puts it in the request header,
  sends through the egress gateway with no redirects (the credential
  goes to one host), and scrubs the value, and its URL-encoded and
  base64 forms, from the result and any error. There is no list
  operation, so a worker cannot enumerate secrets. A test has the server
  echo the header back in three encodings and then searches every table,
  the returned result and the log for the value. The tool holds a secret
  and an outside effect, so the Rule of Two refuses it for any task with
  untrusted input (`tests/test_connectors.py`). Limits: a secret shorter
  than 8 characters is refused, not handled; the value exists in the
  supervisor's memory during the call; and a destination that stores what
  it is sent can still be told to repeat the value later, which redaction
  cannot see.
- **Evidence ledger** (`lab/ledger.py`, migration 7, #90). A research
  task records its question, protocol version, data and code identifiers,
  outputs and validation checks. Each conclusion is a claim whose status
  (unverified, supported, contradicted, verified) is separate from the
  task's: finishing a task changes no claim. A claim links to quotes in
  snapshots of its sources, stored as immutable content-addressed blobs
  and re-hashed on every open; a quote must actually appear in the
  snapshot it cites. Contradicting evidence anywhere blocks supported and
  verified; verification needs two independent sources (snapshots of the
  same source count once), a contradiction pass newer than the last
  evidence change, and a named signer, is never automatic, and is lost
  when new evidence arrives. `run_review_pass` re-opens every cited
  snapshot, drops links whose source no longer verifies, and records the
  list of claims with missing evidence and with contradictions; a draft
  is reviewable only while that pass is current. Every state change is an
  audit event. Limits: "independent" means a different source id, not
  proof the sources did not copy each other, and deciding whether a quote
  really supports a claim is still a person's judgement.
- **Versioned skills with rollback** (`lab/skillstore.py`, migration 11,
  item 8.7, #87). A skill is never edited in place. Each submission is
  a new immutable version whose files go into the content-addressed
  store, recorded with its content hash, the version it derives from,
  where it came from and who submitted it, and it starts as a candidate
  that runs nothing. Promotion makes it active and is an operator
  action: signed with the operator's Ed25519 key when the store is given
  the public key, and never by whoever submitted it. A skill cannot lower
  its own permission tier: the tier is the stricter of the submitter's
  and anything the skill's own SKILL.md declares; a promotion may not be
  less restrictive than the version it replaces unless the operator says
  so in the signed request; a skill with executable files is never below
  `approve`. A version can be marked known good with evidence, and a
  rollback is one step to the newest earlier known-good version, with the
  replaced one kept as rolled back. Everything is re-verified against the
  recorded hashes before promotion, rollback and install, and install is
  one rename. Every step is an audit event. Limits: nothing yet loads
  skills from the install directory into an agent, so the tier is a
  recorded constraint that the loader will have to enforce; and without a
  configured operator public key promotion is unsigned (dev mode).
- **Reviewed publishing with receipts** (`lab/publish.py`,
  `publications`, migration 10, item 8.6, #86). A credentialed send is
  bound, in the approval the operator reads, to the destination host and
  the SHA-256 of the exact body (`connector.call` preconditions), so a
  one-character edit is a different intent and needs a new approval. The
  attempt is written down as `reserved` before anything is sent, with an
  idempotency key derived from the task, connector, method, path and body
  hash, which is also sent to the provider in the connector's
  idempotency header. On a response the row becomes `confirmed` with the
  provider's own id and a hash of the response. A lost response leaves
  the row `reserved` and the operation `uncertain`; the retry is held,
  never resent. `lab publish reconcile` asks the provider by key (the
  connector's `lookup_path`): if found, the receipt is stored and the
  operation resolved as happened; if not, nothing is decided for the
  person, and a resend carries the same key so a provider that honours it
  still cannot post twice. Tested end to end against a dummy provider that
  can act and then lose its reply. Limits: a provider that ignores
  idempotency keys and has no lookup can only be reconciled by hand, and
  a "not found" can be eventual consistency, which is why it stays a
  person's call. Connector definitions live in a strict JSON file
  (`connectors_file`) that holds secret names, never values.
- **Inspectable memory** (`lab/memory.py`, migration 9, item 8.4, #85).
  ADR 0003's rule is enforced: an outsider's content never becomes
  curated memory by itself. Curated memory needs a named promoter and is
  refused when it came from a tainted task; evidence memory always
  records its source and hash, is always untrusted, and always expires
  (30 days by default). Every entry carries source, hash, time, creator,
  trust, expiry and an embedding version (NULL: the FTS5 baseline, no
  embedding). Retrieval rebuilds the query token by token so a search
  string cannot use FTS syntax, returns fixed-schema data with
  provenance, and only from active, unexpired rows. Revoking removes a
  memory from the index in the same transaction, names the tasks that
  read it, and, given the ledger, withdraws evidence that cited it so the
  claims and routes built on it fall back; deleting blanks the text and
  keeps a tombstone with its hash; correcting replaces it and links back.
  Limits: a memory a task already copied elsewhere (a file, a prompt)
  is not recalled by revoking it, and there is no embedding retrieval yet.
- **Router rubric** (`lab/rubric.py`, item 7.2, closes #33). Routes a
  research task by evidence weight alone, with no model call: a post
  needs one usable claim; a blog needs at least three distinct incident
  sources and a mechanism claim; a paper needs a *verified* measurement
  claim whose evidence includes a measurement, a baseline and a control;
  a ledger with no current review pass, or with claims but none usable,
  is "insufficient evidence"; an empty one is "no artifact". A caller may
  ask for a route, and thin evidence is refused upward with the reason
  recorded; a model's opinion cannot raise a route. Contradicted claims
  are listed as conflicts and excluded, never averaged. Every decision
  carries its evidence chain and asks for human review. `stop_decision`
  gives research a bounded finish (every claim supported, none
  contradicted) or an honest "insufficient evidence" when the search or
  time budget runs out, instead of trusting the model to say it is done.
  Limits: the thresholds are a first guess to be corrected by the human
  reviews, and the source types (incident, measurement, baseline,
  control) are labels the person or trusted code attaches, not verified.
- **Evaluation records** (`lab/evals.py`, `evals/tasks.jsonl`, item 7.3,
  #81). A fixed 24-task set runs against any OpenAI-compatible loopback
  endpoint and is graded by deterministic checks only. Each run writes a
  record sealed with its own hash: lab commit and dirty flag, model name
  and pinned weight and tokenizer revisions, Python, SQLite and library
  versions, macOS build and power settings on a Mac, sampling settings,
  the task file's hash, and every answer with tokens and latency.
  `lab eval rerun` rebuilds a run from the record alone and refuses if the
  record was edited, the task file changed, or the checkout is at a
  different commit (unless told otherwise), then reports which answers
  moved. Tested against a stub endpoint; no real model has been run, and
  the records are not signed, only self-hashed, so they detect accident
  and casual edits, not an adversary who recomputes the hash.
- **Event-log emitter** (`lab/emitter.py`, `lab emit`, `lab chain`, #32).
  Proposals come from patterns in the append-only log (today: three or
  more tasks of one kind failing for the same normalized reason). A
  proposal is an ordinary `notify`-tier task with an `event` origin, so it
  is tainted, cannot carry a grant, destination or policy, and passes the
  same gate as any task. The producing event ids are stored in the payload
  and in a hash-chained `proposal_emitted` event; failure text is
  untrusted and is shown escaped. Proposals never count as input to the
  rule, so the emitter cannot feed on itself. Nothing schedules `lab emit`
  yet; it is run by a person or a timer.
- **Status and counters** (`lab/metrics.py`, `lab status`, #89). Queue
  depth per state, the age of the oldest queued, running and
  approval-waiting task, live leases, last success, and counters for
  policy denials, rejected approvals, egress denials, lease losses,
  forced terminations (emergency stop, lease-loss stop, wall-clock kill),
  retries, recoveries, worker errors and tainted tasks, all read from the
  append-only event log, so a number cannot disagree with the audit trail.
  `lab status` exits 2 when unhealthy (a running task on an expired
  lease, or work waiting with no live worker and a silent log), which is
  what the watchdog (#78) and the dead-man switch (#79) will act on.
  Nothing acts on it yet.
- **Injection harness and taint on read** (`lab/attacks.py`, item 4.7,
  #72). Nine scenarios pair a useful task (fetch a page, write a summary)
  with a prompt injection in the page, run against a stub model that is
  the worst case: it obeys every directive it reads. Grading is on state
  (workspace files, database rows, what the simulated network saw), not
  on the transcript, and reports utility and attack success separately.
  Result today: 0 of 9 attacks succeed (delete, shell, exfiltrate to
  another host, metadata address, path escape, git hook, credentialed
  connector, ungranted tool, authority smuggled into a child task);
  utility 7 of 9, the two losses being the refusals of a credentialed
  connector for a task that fetches. Four control tests remove one
  defence each and require the matching scenario to then succeed, so the
  graders are known to be able to fail. The harness found two things,
  both fixed: `net.fetch` now counts as untrusted input for the Rule of
  Two whatever the task's origin, and a task that has read fetched or
  connector content is marked tainted from then on (`task_tainted`
  event), so children it creates inherit that. `python -m lab.attacks`
  prints the table. **Not done:** a run against a real model, which
  needs the adapter and the Mac mini, and the AgentDojo suite itself.
- **Operator-signed approvals** (`lab/operator.py`, item 4.5, #70).
  `lab operator init` creates an Ed25519 keypair; `lab approve --key`
  signs the grant over the approval id, action hash, expiry and decider.
  A supervisor given the public key (`operator_public_key` or
  `LAB_OPERATOR_PUBKEY`) consumes only approvals whose signature
  verifies, so a row the agent wrote, an unsigned grant, a grant signed
  by another key, or a signed one edited afterwards (longer window,
  different decider) is ignored and audited as `approval_rejected`. A
  forged row cannot shadow a genuine one. `--by` is an audit label, not
  identity: the key is the identity. The CLI refuses ambiguous or
  non-hex id prefixes, refuses to approve if `--expect-hash` differs
  from what was reviewed, escapes control and bidi characters in
  everything it prints, and warns when a grant is unsigned. **This is a
  boundary only once the private key is unreadable to the agent's OS
  account and the supervisor is configured with the public key.** Neither
  is true yet: without a configured key approvals are not checked (the
  supervisor logs a warning at start), and the lab account is parked for
  the Mac mini (`ops/mac-mini-setup.md` section 11). Adds `cryptography`
  as the first runtime dependency.
- **Backup and restore** (`lab/backup.py`, item 3.4, #67). `lab backup`
  snapshots the live database with SQLite's online backup API (no torn
  copy, supervisor keeps running) and copies only artifact blobs the
  destination lacks. `lab restore-check` restores into a fresh, empty
  directory and fails on any of: file hash or size differing from the
  manifest, SQLite integrity check, schema version, foreign keys, the
  audit chain or its head, or any artifact blob missing or altered
  (`tests/test_backup.py`). Backups are not encrypted and are only as
  private as the directory they are written to. Recovery drills
  (`lab drill`, `ops/drills/`) record every run and count as
  demonstrated only on the Mac mini; the monthly drill there is parked.

## Known gaps in what exists

Reproduced by an independent review on 2026-09-28 and tracked, not fixed
yet. Until these close, run the lab only with dummy data and review
every draft by hand.

- Handlers registered with `register_reviewed` run in their own worker
  process with a minimal environment, no database path and no lease
  token, and act only through the broker (item 1.2). They still run as
  the same OS user, so a hostile handler that found the database file
  could open it; the separate lab account closes that (#70). Only code
  under `lab.handlers` can be loaded into a worker.

## What does NOT exist yet

Stated plainly, because a security policy implying protections that are
absent is worse than no policy:

- **Network egress exists only through one gateway, and no real host
  has been exercised yet.** `net.fetch` (`lab/egress.py`, item 4.3, #14)
  is the only outbound path: see "What exists". Shell commands still run
  with the sandbox's network rules, and nothing else in the lab opens
  sockets.
- **No memory ceiling per task.** Commands get only PATH, HOME, TMPDIR
  and LANG; parameters are schema-checked; timeouts are clamped to 300 s;
  output is capped at 256 KiB per stream; the command's whole process
  group is killed at the deadline and on exit; tool calls, child tasks
  and each run's wall-clock time are capped (item 1.10,
  `tests/test_limits.py`). Memory is not, and arrives with the model
  adapter (#16, #74). A fork bomb inside the deadline is contained by the
  group kill, not prevented.
- **The secret broker and publishing have only been run against a dummy provider.**
  Built (`lab/vault.py`, `lab/connectors.py`, item 4.4, #15) and tested
  with dummy credentials and a fake transport. No real credential has
  been resolved from the Keychain, and no real destination has been
  called.

**Do not connect real credentials until the separate operator account
exists (#70), the memory ceiling exists (#16), and the Keychain path has
been exercised on the mini (`ops/mac-mini-setup.md` section 6).** The
egress gateway and the secret broker are the other two of the original
three absences and are now built.

## Process isolation

Implemented (issue #17). Anything that executes code runs under a macOS
Seatbelt profile via `sandbox-exec`, applied by `lab/sandbox.py`. The
kernel intercepts file opens, network connects and forks at the syscall
boundary, and **child processes inherit the restrictions**, so a
subprocess cannot escape by spawning another.

If OS-level isolation is unavailable, execution is **refused** rather
than downgraded. A caller that asked for confinement and silently did not
get it would be trusted with work it cannot safely run.

### What the sandbox does NOT stop

**Local account enumeration.** Measured on macOS 27 with `/etc` and
`/private/etc` fully denied:

```
/usr/bin/id -un                    -> the operator's username
/usr/bin/dscl . -list /Users       -> all 133 accounts
/usr/bin/dscl . -read /Users/<u>   -> home directory and shell
```

These reach `opendirectoryd` over a mach port, not through
`/etc/passwd`, so denying `/etc` does not affect them. Narrowing
`mach-lookup` would very likely break dyld, Python and the model
runtime, so it stays open deliberately, a decision recorded in
[ADR 0007](docs/decisions/0007-isolation-for-untrusted-code.md) (#27).
Untrusted code is not executed under Seatbelt at all; when it must be, it
goes in a disposable Apple container (Linux guest, VM boundary), which is
not built yet.

Filesystem confinement itself is intact: `ls /Users` is refused. A
sandboxed agent cannot walk home directories, but it **can** enumerate
every account on the machine and learn the operator's home path and
shell.

**Do not describe this sandbox as preventing username or home-directory
disclosure.** It does not.

Known limitation: `sandbox-exec` is deprecated by Apple. It remains
functional, macOS's own daemons use Seatbelt internally, and Apple has
published no replacement covering headless process sandboxing, since App
Sandbox requires code signing and an Xcode project. Tracked as a risk
with Apple's container framework as the fallback if it is ever removed.

## Fixed

1. Unbounded task leasing. Measured at 19 leased against 1 running slot;
   now bounded by a worker pool. Issue #7.
2. No lease renewal, permitting duplicate execution of live work.
   Issue #8.
3. Approvals defined but not enforced. Issue #9.
4. Isolation documented but not implemented. Issue #10, filesystem
   portion; issue #17, process isolation.

5. `recover()` acted on a scan taken before its transaction began, so it
   could raise on a task that had just finished, or release a lease
   another supervisor had taken in the meantime. It now re-checks each
   task inside the transaction that changes it. Found by the race tests
   in `tests/test_races.py` (item 2.4, #61).
6. `resume_after_approval` changed the state and wrote its audit event as
   two separate commits, so a crash between them left a transition with
   no event. It now runs in one transaction.

Regression checks: `tests/manual_confirm_defects.py`.
