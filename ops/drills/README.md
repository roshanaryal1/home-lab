# Recovery drills

A unit test shows one piece of recovery works. A drill injects a real
failure and records what happened. Recovery counts as **demonstrated**
only for a drill that ran on the Mac mini with `LAB_TARGET=mac-mini`
exported; anywhere else the record says "rehearsal" and does not count.

```sh
uv run python -m lab.cli drill crash              # SIGKILL mid-task, restart, recover
uv run python -m lab.cli --db ~/.local/share/home-lab/lab.db drill restore
```

Each run writes a dated file in `ops/drills/log/` using the layout of
`TEMPLATE.md`. Commit the record; a drill that is not committed did not
happen.

| Drill | Command | Software ready | Demonstrated on the mini |
|---|---|---|---|
| Crash mid-task | `drill crash` | yes | 2026-09-29, both kinds PASS ([idempotent](log/2026-09-29T081543Z-crash-idempotent.md), [non-idempotent](log/2026-09-29T081543Z-crash-non-idempotent.md)) |
| Backup then restore | `drill restore` | yes | not yet |
| Power pull during a task | manual, see `ops/mac-mini-setup.md` | checklist | not yet |
| Interrupted task after reboot | manual with launchd (6.2) | after 6.2 | not yet |
| Failed model load | `drill model-load [--endpoint URL]` | yes | 2026-09-29, all three PASS on the rerun ([server down](log/2026-09-29T185021Z-model-load-server-down.md), [wrong model](log/2026-09-29T185021Z-model-load-wrong-model.md), [too big](log/2026-09-29T185021Z-model-load-too-big.md)); the first run's wrong-model case FAILed because `mlx_lm.server` rejects an unknown model with 404 instead of answering as another model, which the drill had not allowed for; the drill now accepts exactly that 404 or a `ModelMismatch`, nothing else, and passed again under that stricter check ([records](log/2026-09-29T190149Z-model-load-wrong-model.md)) ([record](log/2026-09-29T184943Z-model-load-wrong-model.md)) |
| Monthly restore drill | `drill restore` against the live database | yes | not yet |
