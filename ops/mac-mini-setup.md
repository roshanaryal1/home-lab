# Mac mini M6 setup checklist

Do these in order on the mini itself. Everything here comes from the
reference architecture produced by the companion study
(`analysis/consensus/reference-architecture.md` in
[roshanaryal1/llm-architects](https://github.com/roshanaryal1/llm-architects)),
sections 10 to 13. Where the study's systems disagreed, the
`[adjudicated]` choice is the one written down here.

Target hardware: Apple M6 (base) Mac mini, 32 GB unified memory,
512 GB internal SSD, 1 TB external SSD, always on, on AC power.

Verified M6 figures from the study's register: announced 2026-08-25,
170 GB/s memory bandwidth at 24 to 32 GB (153 GB/s at 16 GB), 32 GB
maximum. One of the surveyed systems claimed "300+ GB/s", which is
wrong; do not plan capacity against that number.

## 1. Accounts

- [ ] Complete macOS setup with your normal admin account.
- [ ] Create a **dedicated non-admin account** for the lab, for example
      `lab`. Everything the agents do runs as this user. This is the
      single most important control in the whole design: it is what
      makes "delete everything" a scoped failure rather than a total
      one.
- [ ] Confirm the `lab` user cannot `sudo`: run `sudo -l` while logged
      in as it and expect a refusal.
- [ ] Turn on FileVault.

## 2. Network

- [ ] Install Tailscale and sign in.
- [ ] Note the tailnet hostname; you will bind the dashboard to it later.
- [ ] Confirm **no public port forwarding** exists for this machine on
      your router.
- [ ] Do not expose MLX, Ollama or llama.cpp endpoints directly. They
      bind to loopback or the tailnet interface only.
- [ ] Enable Remote Login (SSH) for the admin account so this repo can
      be deployed from the laptop.

## 3. Storage

Internal 512 GB, latency-sensitive state only:

- [ ] macOS, applications, Python runtime.
- [ ] Supervisor code and SQLite databases including WAL files.
- [ ] Check the Python's linked SQLite before first start:
      `python -c 'import sqlite3; print(sqlite3.sqlite_version)'` must be
      3.51.3 or later, or 3.50.7+ / 3.44.6+. The supervisor refuses to
      start otherwise (WAL-reset bug, https://sqlite.org/wal.html section 11).
- [ ] Active repositories and worktrees.
- [ ] Hot model weights, the ones in daily use.
- [ ] Keep a substantial free-space reserve. Do not fill the internal
      disk just because there is capacity; leave room for swap, WAL
      growth and model swapping.

External 1 TB, mounted at a stable path:

- [ ] Full model library, cold weights.
- [ ] Research corpus, PDFs, datasets.
- [ ] Archived repositories, experiment artifacts, long-term logs.
- [ ] Backup sets.

The external SSD is storage, **not a backup strategy**. Anything that
matters needs a third destination as well.

## 4. Runtime

- [ ] Install the Xcode command line tools.
- [ ] Install Python 3.13 or newer.
- [ ] Install the MLX family for inference. The study was unanimous on
      MLX over the alternatives for this hardware.
- [ ] Decide the heavy model. The study did **not** reach consensus
      here, so this is an open choice, not a settled one: the candidates
      were Qwen3-Coder-30B-A3B, Qwen3.6-35B-A3B, and a dense 2024-era
      32B. Pick one, write down why, and benchmark before committing.
- [ ] Plan for exactly **one heavy inference slot**. Two concurrent
      heavy models do not fit in 32 GB. The supervisor already enforces
      this with a semaphore; do not raise it without measuring first.

## 5. Always-on

- [ ] `launchd` job with `RunAtLoad` and `KeepAlive` for the supervisor.
- [ ] A separate watchdog or heartbeat process. `KeepAlive` restarts a
      dead process; it does not notice a wedged one.
- [ ] Structured rotating logs (built: `lab/logsetup.py`): set `LAB_LOG_DIR`
      in the supervisor's plist, on the external SSD for the long-term set.
- [ ] Queue-aware sleep prevention (built: `lab keepawake`): install
      `com.homelab.keepawake.plist`, queue a task, and confirm with
      `pmset -g assertions` that `caffeinate` holds the machine while work is
      pending and releases it after the grace period.
- [ ] Confirm startup recovery works: kill the supervisor mid-task,
      restart it, and check that an idempotent task requeues while a
      non-idempotent one is held for review. The test suite covers this
      logic, but verify it once on the real machine.

## 6. Secrets

- [ ] Secrets live in Keychain or injected environment, never in the
      agent workspace and never in a file the agent can read.
- [ ] The `lab` user gets only the credentials it actually needs.

## 7. What not to do yet

From section 15 of the reference architecture. These are deferrals, not
rejections, revisit after benchmarking:

- No Redis, no Celery. SQLite WAL is the day-one queue.
- No agent framework as the backbone. The supervisor is your own code
  so the architecture survives a framework changing or disappearing.
- No Kubernetes, no heavy virtualization for ordinary tasks. On 32 GB
  the overhead is a real cost.
- No Grafana/Prometheus stack until there is something worth graphing.
- No graph memory store. sqlite-vec plus FTS first.

## 8. Deploying this repo

Once steps 1 to 5 are done:

```sh
# from the laptop
ssh <admin>@<tailnet-host>
sudo -u lab -i
git clone <this repo> ~/home-lab
cd ~/home-lab
uv sync --locked --extra dev    # Python from .python-version, deps from uv.lock
uv run python -m pytest tests/ -q   # expect all green before going further
```

The supervisor and queue are machine-independent and are already
tested. Steps 3 onward of the build order (model adapter, residency
policy, swap manager) are the parts that need the M6 and cannot be
meaningfully exercised on a laptop.

## 9. Isolation measurements (item 4.6, #71, ADR 0007)

Parked until the M6 is on the desk. None of this is run yet.

- [ ] Install Apple's `container` tool (macOS 26 or later, Apple silicon);
      record the version and the macOS build it ran on.
- [ ] Start and stop one container from a small Linux image, 20 times.
      Record start-up time (median and worst) and resident memory of the
      container's VM at idle and while running a Python test suite.
- [ ] With the heavy model loaded and generating, start one container.
      Record memory pressure and swap before and during. Pass only if no
      swap growth.
- [ ] Confirm from inside a container that the host's directory service
      is unreachable (`getent passwd` shows only the guest's accounts) and
      that only the mounted workspace is visible.
- [ ] Write the numbers into ADR 0007, replacing "not measured yet".

## 10. Backup and recovery drills (items 3.4 and #91, #67)

Software is done and rehearsed in CI. These are the parts that count only
on the mini. Export `LAB_TARGET=mac-mini` so the record says so.

- [ ] Choose a backup target on a different physical disk or machine.
- [ ] `uv run python -m lab.cli backup --to <target>` from a scheduled job;
      confirm a new `*.manifest.json` appears.
- [ ] First full restore drill: `uv run python -m lab.cli drill restore`
      against the live database. Commit the record from `ops/drills/log/`.
- [ ] Repeat monthly; log the date in `ops/drills/log/`.
- [ ] Crash drill on the mini: `uv run python -m lab.cli drill crash`.
- [ ] Power-pull drill during a running task: pull the plug, boot, confirm
      the task is requeued or held per its idempotency, note timings.
- [ ] Failed model load drill after the model adapter (5.1) exists.

## 11. Operator account and approval keys (item 4.5, #70)

The code is done and tested. What makes it a boundary is which OS account
can read what, and that needs the machine. Parked until the M6.

- [ ] Create the non-admin `lab` account (section 1) and keep the
      operator (admin) account separate.
- [ ] As the operator: `uv run python -m lab.cli operator init --dir ~/.lab-operator`.
      The private key stays in the operator's home (`chmod 700 ~/.lab-operator`).
- [ ] Copy only `operator.pub` to a path the `lab` account can read; set
      `LAB_OPERATOR_PUBKEY` in the LaunchDaemon environment (6.2).
- [ ] Confirm the supervisor log does NOT show "approvals are NOT
      signature-checked".
- [ ] As `lab`, try `cat ~operator/.lab-operator/operator.key` (expect
      permission denied) and try to approve a test request with a
      fabricated signature (expect `approval_rejected` in the events).
- [ ] Put the queue database, policy files and credentials under a
      directory the reviewed handlers' worker processes cannot open (#70
      acceptance: a handler that opens the DB path gets EACCES).

## 12. Real-model injection run (item 4.7, #72)

Parked until the model adapter (5.1) and the M6 exist. The scenarios and
graders are in `lab/attacks.py`; they take any handler.

- [ ] Wire the adapter as the `model` argument of `run_scenario` and run all nine
      scenarios; require attack success 0 of 9 and record utility.
- [ ] Run the AgentDojo suite against the same adapter; record its utility
      and attack-success rates next to the stub's.
- [ ] Repeat after every model or prompt change (gate G2, then nightly, 6.4).

## 13. Model adapter on the real model (items 5.1 and 5.2, #74, #75)

The adapter is done against a mock. These need the M6.

- [ ] Start the chosen inference server on loopback only; point
      `OpenAICompatibleAdapter` at it.
- [ ] Record the exact weight and tokenizer commit hashes in a `ModelSpec`
      (never a branch name) and in ADR 0001.
- [ ] Measure resident memory with the model loaded and at three context
      lengths; replace `weights_mb`, `kv_bytes_per_token` and
      `DEFAULT_BUDGET_MB` with measured values.
- [ ] Confirm a request sized to exceed the budget is refused at
      admission and the machine does not swap.
- [ ] Run the malformed tool-call cases against the real model and record
      how often it emits a call the parser refuses.

## 14. Real-model evaluation runs (item 7.3, #81)

The runner and record format are done and tested against a stub endpoint.

- [ ] With the inference server up (section 13), run
      `uv run python -m lab.cli eval run --endpoint http://127.0.0.1:PORT/v1 ...`
      on the mini and commit the record from `evals/runs/`.
- [ ] Confirm the record says `ON TARGET`, names the macOS build, and holds
      the `pmset` power settings.
- [ ] `lab eval rerun` the record on the same commit; expect identical
      answers at temperature 0 with a fixed seed, and note any that move.
- [ ] Repeat for the smaller baseline model (5.2) with the same task file.

## 15. First real destination (item 8.6, #86)

Everything is tested against a dummy provider. Before the first real send:

- [ ] Write the connector into the `connectors_file`: one host, the secret
      *name*, the provider's idempotency header and, if it has one, its
      lookup-by-key path. Confirm from the provider's docs that it honours
      the idempotency key and for how long.
- [ ] Store the credential in the Keychain (`security add-generic-password
      -s home-lab -a <name>`), scoped as narrowly as the provider allows.
- [ ] Send one throwaway draft to a private or test destination. Read the
      approval request: destination and draft hash must match what you expect.
- [ ] Deliberately kill the network mid-send once; confirm the task is held,
      `lab publish reconcile` finds or does not find it, and no duplicate
      appears at the provider.
- [ ] Only then point a connector at a public destination.

## 16. launchd service and watchdog (item 6.2, #78)

The definitions are generated by `lab/service.py` and committed under
`ops/launchd/` with placeholder paths (`/opt/homelab`, user `lab`). Adjust
the paths, then:

- [ ] Create the `lab` account (section 11) and `/var/log/homelab`, owned by it.
- [ ] Install `com.homelab.supervisor.plist` in `/Library/LaunchDaemons`
      (owner root, mode 644) and `launchctl bootstrap system` it. Confirm
      it runs as `lab` and `LAB_OPERATOR_PUBKEY` is in its environment.
- [ ] Install `com.homelab.watchdog.plist` the same way (it runs as root and
      only needs to signal the supervisor).
- [ ] `kill -9` the supervisor: launchd restarts it within the 30 second
      throttle, and startup recovery requeues idempotent work.
- [ ] Freeze it instead: `kill -STOP <pid>`. Within two minutes the
      watchdog kills it and launchd restarts it. Log the result as a drill
      (`ops/drills/`).
- [ ] `lab watchdog --dry-run` prints `healthy` when idle and running.
- [ ] The loop: install `com.homelab.tick.plist` the same way, with
      `LAB_MODEL_URL`, `LAB_MODEL_NAME` and `LAB_MODEL_REVISION` in its
      environment (and in the supervisor's, so the daemon registers the
      summarizer). Run `lab tick` once by hand first and read the
      `proposal_routed` events with `lab audit verify`.

## 17. Power and disk encryption (item 6.1, #77)

- [ ] Set "Start up automatically after a power failure" (System Settings,
      Energy) and confirm it survives a reboot.
- [ ] Decide FileVault. With it on, the Mac waits at the password screen after
      any reboot and the lab does not start; with it off, disk contents are
      readable to anyone holding the drive. Record the decision and why here
      and in ADR 0001. If on, plan for a remote unlock path or accept manual
      unlock after power loss, and make the dead-man switch (section 19) alert
      when the lab has been down for more than ten minutes.
- [ ] Put the mini on a UPS sized for a clean shutdown, not for runtime.
- [ ] Pull the plug once. Done when the lab is running again on its own or a
      phone alert says it is not.

## 18. macOS updates and nightly self-test (item 6.4, #80)

- [ ] Turn automatic major upgrades off; keep security responses and
      rapid security updates on.
- [ ] Nightly job (a LaunchDaemon like the watchdog) runs the sandbox and
      safety tests (`pytest -m safety`) and `lab audit verify`, then sends the
      result to the phone. A result must arrive every morning, including "ok".
- [ ] After each OS update, run the same job by hand before leaving the lab
      unattended (a new macOS has already needed a sandbox fix, #25).

## 19. Alerts and the dead-man switch (item 6.3, #79)

`lab control` and `lab cancel` are built. What needs the mini and an account:

- [ ] Pick the alert channel (a push service or email) and create the account.
- [ ] Alert command (built: `lab/alert.py`). Copy `ops/alert.example.json` to
      `/etc/homelab/alert.json`, owned by `lab`, mode 600, and point `command` at
      your notifier (an absolute path; it reads the message on stdin). Install
      `com.homelab.statuscheck.plist` (runs `lab status` every 5 minutes and
      alerts when unhealthy) and `com.homelab.selftest.plist` (03:17 nightly).
      To test it, run `lab status --alert-config` against a scratch database
      holding a task whose lease has expired (a paused or stopped lab is not
      unhealthy, so `lab control` will not trigger it).
- [ ] Dead-man switch: an external service expects a ping every few minutes
      from the lab and alerts when it stops. Unplug the network; the alert must
      arrive within ten minutes.
- [ ] Stop an active dummy task with `lab control stop` and confirm no later
      tool effect in the audit log.

## 20. Constrained decoding (item 5.3, #76)

- [ ] After the section 13 baseline, run the same task file with
      grammar-constrained decoding for tool calls and routing labels.
- [ ] Record invalid-call rate, correct-task rate and latency next to the
      baseline. Adopt only on a measured gain; the strict parser stays either
      way.

## 21. Pre-registration (item 8.1, #82)

- [ ] Create the OSF account (owner).
- [ ] Register hypotheses, metrics, failure categories and the analysis plan
      before the first real eval run, and link the registration from
      `docs/PIPELINE.md`. The registration timestamp must precede the first
      run's provenance record.

## 22. Shadow experiment, tuning and memory ceilings (items 8.3, 8.8, #84, #88, #16)

- [ ] 8.3: run candidate typed-decision models in shadow against the
      deterministic rubric (`lab/rubric.py`); measure per-class error, false
      promotion, abstention, calibration, latency and memory. A typed allow
      or a confidence never acts as permission.
- [ ] 8.8: after section 14, measure queue wait, tasks per hour, tail latency,
      peak memory, throttling and energy on the fixed task set; tune only with
      a measured gain.
- [ ] #16: the ceiling mechanism is built (wall clock, RSS and CPU for reviewed
      handlers). Set `task_max_rss_mb` and `task_max_cpu_seconds` from measured
      peaks of the real handlers with headroom, and check the model server's
      own footprint stays inside the 32 GB admission budget.
