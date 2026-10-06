# Mac mini session report

- started: 20261006T232018Z
- host: <mac-mini>
- mode: live
- REPO=/Users/<operator>/Research and Development /home-lab
- DB=/var/homelab/lab.db
- PY=/opt/homelab/.venv/bin/python
- MODEL_URL=http://127.0.0.1:8080/v1
- BACKUP_VOLUME=/Volumes/labbackup
- PUBKEY=/etc/homelab/operator.pub
- ALERT_CONFIG=/etc/homelab/alert.json
- LOG_DIR=/var/log/homelab
- LAB_CONTAINER_IMAGE=not set
- MCP_CONFIG=/etc/homelab/mcp.json
- steps: backup

What each step proves, and which issue to paste it into: ops/mac-session.md.

> **Reading notes, added after the run (the transcript below is otherwise unedited).**
> 1. The hostname and the operator's home path were replaced with `<mac-mini>` and `/Users/<operator>`; nothing else was changed.
> 2. This is the passing run after three failed attempts earlier the same hour (reports not committed: each failed with `backup: unable to open database file`). Causes, in order: the T7 was mounted `noowners` so `chown` was refused; after `diskutil enableOwnership` and a remount, the encrypted APFS volume had to be unlocked again (`diskutil apfs unlockVolume`); and `chown` was still refused for root until the terminal app was given Full Disk Access (System Settings, Privacy & Security) and restarted. The folder is now owned by `lab`, mode 700.
> 3. The transcript prints the script's own variable names. `MANIFEST` was set by the script to the manifest the backup printed, `/Volumes/labbackup/home-lab-backups/lab-20261006T232020Z.manifest.json` (see the result line). To replay by hand, set `MANIFEST` to that path before `restore-check`.
> 4. Only the backup step ran (`--only backup`). It wrote a manifest, restored it into a temporary folder as `lab`, verified it (8 audit events, 0 artifact blobs) and removed the temporary folder. The scheduled 02:47 job (`com.homelab.backup`) is not installed yet.

## 1. Backup to the backup volume and a restore check (#67)

Step `backup`. Writes a backup of the live database to the backup volume as lab, restores it into a temporary folder and checks its integrity, then removes the temporary folder.

- asked: Write a new backup of /var/homelab/lab.db to /Volumes/labbackup/home-lab-backups? [y/N]; answer: y
```text
$ sudo -u lab env LAB_TARGET=mac-mini "$PY" -m lab.cli --db "$DB" backup --to "$BACKUP_VOLUME/home-lab-backups"
wrote /Volumes/labbackup/home-lab-backups/lab-20261006T232020Z.manifest.json
[exit 0]
```
```text
$ RESTORE_DIR=$(sudo -u lab /usr/bin/mktemp -d /tmp/homelab-restore.XXXXXX)
[exit 0]
```
```text
$ sudo -u lab "$PY" -m lab.cli --db "$DB" restore-check "$MANIFEST" --into "$RESTORE_DIR/restore"
ok: 8 audit events, 0 artifact blobs checked, restored to /tmp/homelab-restore.uLLTxa/restore/lab.db
[exit 0]
```
```text
$ sudo -u lab /bin/rm -rf "$RESTORE_DIR"
[exit 0]
```

**Result: PASS.** backup /Volumes/labbackup/home-lab-backups/lab-20261006T232020Z.manifest.json restored and verified: ok: 8 audit events, 0 artifact blobs checked, restored to /tmp/homelab-restore.uLLTxa/restore/lab.db

## Summary

| # | Step | Issues | Result | Note |
|---|---|---|---|---|
| 1 | Backup to the backup volume and a restore check | #67 | PASS | backup /Volumes/labbackup/home-lab-backups/lab-20261006T232020Z.manifest.json restored and verified: ok: 8 audit events, 0 artifact blobs checked, restored to /tmp/homelab-restore.uLLTxa/restore/lab.db |
