# Chat bot installed on the Mac mini, first checks (2026-10-07, #239, #288)

Written from what the operator saw and pasted at the Mac mini. No script produced a log for
this, so each line below says who observed it. Nothing here was run by the repository's tests.

## What was done
- A second Telegram bot, separate from the alert bot and from the old shell bot, made in
  BotFather. Its token was typed at a hidden prompt and written only into the installed,
  root-owned `/Library/LaunchDaemons/com.homelab.chat.plist` (mode 600) with the paired chat
  id from the Keychain item `homelab-telegram-chat`. The token never went on a command line.
- `launchctl bootstrap system` loaded a 600-mode definition without trouble (the setup guide
  had listed that as to be confirmed).
- The first start, made before the file held the token and chat id, exited with code 2 and
  logged `chat: no paired chat`. After the config was written and the service restarted it
  stayed running (`state = running`, `last exit code = (never exited)`).

## What the operator observed (read from the phone)
- `/status` answered: `Lab mode: running. Tasks from this chat: none. Approvals waiting: 0.`
- A plain message, `hello`, was answered `Queued as 7d622a1d4990. The reply comes here when
  it finishes.`, and the reply `7d622a1d4990: Hello! How can I assist you today?` arrived. So
  the paired chat reached the broker as a task, the real model answered it, and the result
  went back to the same chat.

## What was not checked
- A message from an account that is not paired, which must get no answer (the setup guide
  checks it in `chat.log` as `unpaired`).
- `/stop` from the phone and `lab control show` on the Mac.
- The boundary: a chat task that waits for approval, and that nothing in the chat approves it.
- The retirement of the old raw-shell bot (#184, setup section 23 last item).
- Whether the service survives a reboot.
