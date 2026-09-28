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
- [ ] Structured rotating logs, on the external SSD for the long-term
      set.
- [ ] Queue-aware sleep prevention: `caffeinate` while work is pending,
      normal sleep once the queue has been empty for a configured
      period. Do not hold the machine awake unconditionally.
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

## 9. Isolation measurements (item 4.6, ADR 0007)

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

## 10. Backup and recovery drills (items 3.4 and #91)

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
