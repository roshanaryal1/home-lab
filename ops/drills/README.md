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
| Crash mid-task | `drill crash` | yes | not yet |
| Backup then restore | `drill restore` | yes | not yet |
| Power pull during a task | manual, see `ops/mac-mini-setup.md` | checklist | not yet |
| Interrupted task after reboot | manual with launchd (6.2) | after 6.2 | not yet |
| Failed model load | needs the model adapter (5.1) | no | not yet |
| Monthly restore drill | `drill restore` against the live database | yes | not yet |
