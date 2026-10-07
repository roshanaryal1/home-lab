# Recovery drills

A unit test shows one piece of recovery works. A drill injects a real
failure and records what happened. Recovery counts as **demonstrated**
only for a drill that ran on the Mac mini with `LAB_TARGET=mac-mini`
exported; anywhere else the record says "rehearsal" and does not count.

> **zsh note.** The blocks below have `#` comments at the end of some lines. macOS's default zsh does not treat those as comments when you paste, so run `setopt interactivecomments` first (it lasts for that Terminal window), or leave the comments out.

```sh
uv run python -m lab.cli drill crash              # SIGKILL mid-task, restart, recover
uv run python -m lab.cli --db ~/.local/share/home-lab/lab.db drill restore
```

Each run writes a dated file in `ops/drills/log/` using the layout of
`TEMPLATE.md`. Commit the record; a drill that is not committed did not
happen.

**Interrupted task across a restart or a power pull** (#91). No process
survives either, so the drill has two halves. `arm` leaves two dummy tasks
running, one idempotent and one not. They run on a scratch database in
`~/.local/share/home-lab/drill-interrupted`, never the lab's own, and a
background process renews their leases every second, so the failure lands
mid-write. Then you pull the power or restart the Mac. After you log in again,
`check` reads the scratch database. It must pass SQLite's integrity check and
the audit chain check. Recovery must requeue the idempotent task and hold the
other for review. The record says whether the Mac restarted, and whether it
was a clean shutdown (the holder got SIGTERM) or a power loss (it got nothing).
Neither half needs sudo. With `LAB_TARGET=mac-mini`, a check with no restart in
between is a FAIL. If you never run the check, the armed drill removes itself
after 30 minutes (`--hold-minutes`). Run both halves from the checkout:

```sh
LAB_TARGET=mac-mini uv run python -m lab.cli drill interrupted --phase arm
LAB_TARGET=mac-mini uv run python -m lab.cli drill interrupted --phase check
```

The drill does not test the deployed supervisor's own startup recovery under
launchd with a task in flight. That supervisor has no handler that could run
a dummy task, and adding one is a decision for the owner (setup section 16).
The same `recover()` runs in both places.

**The restart drills need sudo.** The kill and freeze drills of the
supervisor under launchd are a step of the session script. The first command
below prints every command and changes nothing. The second runs them and
asks before it kills anything:

```sh
./ops/mac-session.sh --dry-run --only drills
./ops/mac-session.sh --only drills
```

**The monthly restore drill** restores the newest backup the scheduled job
wrote to the backup disk into a fresh temporary folder and checks it. The
backup folder is only read. That folder belongs to `lab`, so the drill runs as
`lab`, and the record goes where `lab` can write. Copy it into
`ops/drills/log/` and commit it:

```sh
sudo -u lab env LAB_TARGET=mac-mini /opt/homelab/.venv/bin/python -m lab.cli --db /var/homelab/lab.db drill restore --from-backup /Volumes/labbackup/home-lab-backups --log /var/log/homelab/drills
```

| Drill | Command | Software ready | Demonstrated on the mini |
|---|---|---|---|
| Crash mid-task | `drill crash` | yes | 2026-09-29, both kinds PASS ([idempotent](log/2026-09-29T081543Z-crash-idempotent.md), [non-idempotent](log/2026-09-29T081543Z-crash-non-idempotent.md)) |
| Backup then restore | `drill restore` | yes | 2026-09-30, PASS against the live database, which was still empty ([record](log/2026-09-30T010636Z-restore.md)) |
| Power pull during a task | `drill interrupted --phase arm`, pull the plug, then `--phase check` (the `power` step of `ops/mac-session.sh`) | yes | not yet |
| Supervisor killed under launchd | `kill -9`, in `./ops/mac-session.sh --only drills` (sudo) or the runbook step 6 | yes | 2026-09-30, PASS: restarted within 40 s ([record](log/2026-09-30T0100Z-supervisor-kill.md)); 2026-10-06, PASS again, new supervisor at the first check ([record](log/2026-10-06-supervisor-kill.md)); 2026-10-06, PASS again ([record](log/2026-10-07-supervisor-kill.md)) |
| Supervisor frozen, watchdog replaces it | `kill -STOP`, in `./ops/mac-session.sh --only drills` (sudo) or the runbook step 6 | yes | 2026-09-30, recovery observed at 150 s but the two-minute target was not demonstrated ([record](log/2026-09-30T0100Z-supervisor-freeze.md)); **2026-10-06, FAIL: still frozen after 182 s** ([record](log/2026-10-06-supervisor-freeze.md), #271); **2026-10-06, PASS in 96 s** after the fix ([record](log/2026-10-07-supervisor-freeze.md), #271); **PASS again in 97 s**, the second of two required passes ([record](log/2026-10-07-supervisor-freeze-pass-2.md)) |
| Interrupted task after reboot | `drill interrupted --phase arm`, restart the Mac, then `--phase check` | yes | not yet |
| Startup recovery of the deployed supervisor with a task in flight | none: the deployed supervisor has no handler for a dummy task | no, owner's decision | not yet |
| Failed model load | `drill model-load [--endpoint URL]` | yes | 2026-09-29, all three PASS on the rerun ([server down](log/2026-09-29T185021Z-model-load-server-down.md), [wrong model](log/2026-09-29T185021Z-model-load-wrong-model.md), [too big](log/2026-09-29T185021Z-model-load-too-big.md)); the first run's wrong-model case FAILed because `mlx_lm.server` rejects an unknown model with 404 instead of answering as another model, which the drill had not allowed for; the drill now accepts exactly that 404 or a `ModelMismatch`, nothing else, and passed again under that stricter check ([records](log/2026-09-29T190149Z-model-load-wrong-model.md)) ([record](log/2026-09-29T184943Z-model-load-wrong-model.md)) |
| Monthly restore drill | `drill restore --from-backup DIR` as `lab`, the newest backup on the backup disk | yes | not yet (2026-10-08: the backup folder is `lab`'s, mode 700, so only the owner with sudo can run it) |
