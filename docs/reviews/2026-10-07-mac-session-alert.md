# Mac mini session report

- started: 20261006T233320Z
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
- steps: alert

What each step proves, and which issue to paste it into: ops/mac-session.md.

> **Reading notes, added after the run (the transcript below is otherwise unedited).**
> 1. The hostname and the operator's home path were replaced with `<mac-mini>` and `/Users/<operator>`; nothing else was changed.
> 2. The installed alert hook is the Telegram command (`lab.telegram_alert`, config `/etc/homelab/telegram-alert.json`, owned by `lab`, mode 600), set up by hand from `ops/mac-mini-setup.md` section 19 earlier the same hour. A first test message ("homelab test alert") reached the phone, and this step sent a second one through the same hook as `lab`, the path `status` and `selftest` use. The operator confirmed it arrived on the phone; that confirmation is the operator's answer at the prompt, not something the script can see.
> 3. `min_interval_seconds` is 3600, so the hook sends at most one alert an hour for the same text. The bot's first token was pasted into a chat by mistake and revoked before this setup; the token in use is the replacement.
> 4. Only the alert step ran (`--only alert`). The dead-man switch and the chat bot are not set up yet.

## 1. A test alert reaches the phone (#79, #80)

Step `alert`. Sends one alert through the lab's alert hook, as lab and with the installed configuration, the same path `lab.cli status` and `lab.cli selftest` use. Then asks whether it arrived.

```text
$ sudo -u lab /bin/cat "$ALERT_CONFIG"
{"command": ["/opt/homelab/.venv/bin/python", "-m", "lab.telegram_alert", "--config", "/etc/homelab/telegram-alert.json"], "timeout_seconds": 30, "min_interval_seconds": 3600}
[exit 0]
```
- asked: Send one test alert through the lab's alert hook now? [y/N]; answer: y
The alert command: `import sys; from lab import alert; c = alert.load(sys.argv[1]); sys.exit(0 if alert.send(c, kind="test", message="home-lab test alert from the Mac mini session (#79)") else 1)`
```text
$ sudo -u lab "$PY" -c "$ALERT_PY" "$ALERT_CONFIG"
[exit 0]
```
- asked: Did the test alert arrive on your phone? [y/N]; answer: y

**Result: PASS.** the hook ran and the operator saw the alert on the phone

## Summary

| # | Step | Issues | Result | Note |
|---|---|---|---|---|
| 1 | A test alert reaches the phone | #79, #80 | PASS | the hook ran and the operator saw the alert on the phone |
