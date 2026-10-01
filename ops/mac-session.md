# One session at the Mac mini

`ops/mac-session.sh` runs every open check that needs the Mac mini, in one
sitting, and writes one Markdown report (#241). Each step has a title, its issue
number, the exact commands it ran with their output, and a result: PASS, FAIL,
SKIPPED or MANUAL. One failed check does not stop the rest.

It changes nothing on the machine unless a step says so and you answer `y` at its
prompt. Every prompt defaults to no: Enter, any other answer, or no answer at all
is no. It asks for `sudo` once at the start.

Plan on about 15 minutes for the automatic steps, and another 30 to 45 for the two
manual ones.

## Before you start

- Sit at the Mac mini, logged in as the operator (the admin account), with your
  phone in reach.
- The lab is deployed (runbook steps 1 to 5), the model server is running and the
  queue is idle: `status` shows `IDLE`.
- The backup disk is plugged in.

## Run it

Check out `main` and read what the script would do. A dry run prints every command
and every prompt, runs nothing and asks nothing:

```sh
cd "$HOME/home-lab"
git pull
./ops/mac-session.sh --dry-run
```

Then run it for real. Set `BACKUP_VOLUME` if the backup disk is not
`/Volumes/labbackup`. Set `LAB_CONTAINER_IMAGE` to a digest-pinned image for
the `skillrun` step, or it is skipped:

```sh
cd "$HOME/home-lab"
./ops/mac-session.sh
```

The report is written to `./mac-session-<UTC timestamp>.md`, or to the path given
with `--report`. To redo one or more steps, name them:

```sh
./ops/mac-session.sh --only caffeinate,concurrency
```

These settings come from the environment. The defaults match the runbook:

| Variable | Default |
|---|---|
| `REPO` | `$HOME/home-lab` |
| `DB` | `/var/homelab/lab.db` |
| `PY` | `/opt/homelab/.venv/bin/python` |
| `MODEL_URL` | `http://127.0.0.1:8080/v1` |
| `BACKUP_VOLUME` | `/Volumes/labbackup` |
| `LAB_CONTAINER_IMAGE` | not set. The `skillrun` step needs a guest image pinned by digest (`name@sha256:...`), and the checkout needs `uv` and the dev extras. |

## The steps, in order

| # | Step | Issue | What it proves | Time | Asks before |
|---|---|---|---|---|---|
| 1 | `context` | #241 | Which commit is deployed, the macOS version, and that `lab.cli status` exits 0. Ties every result to a build. | 10 s | nothing |
| 2 | `home` | #225 | As `lab`, `ls "$HOME"` and `ls "$HOME/Public"` both say `Permission denied`, so `lab` cannot read the operator's files. | 5 s | `chmod 700 "$HOME"`, only if `lab` can read it |
| 3 | `caffeinate` | #235 | `lab` can start `caffeinate -i`, and `pmset -g assertions` shows it holding `PreventUserIdleSystemSleep`. This is what the keep-awake daemon relies on. | 5 s | nothing |
| 4 | `signature` | #70 | As `lab`, an approval made without the operator key is refused at the gate three ways: unsigned through `lab.cli approve`, signed with a key `lab` made itself, and written straight into the table. Each must leave an `approval_rejected` event. It uses a scratch database in a temporary folder and the real `/etc/homelab/operator.pub`; the live database is not opened. | 10 s | nothing |
| 5 | `backup` | #67 | `lab` can write a backup to `$BACKUP_VOLUME/home-lab-backups`, and `restore-check` restores it into a temporary folder and verifies it. The temporary folder is removed after. | 30 s | writing the backup |
| 6 | `alert` | #79, #80 | One test alert goes through the installed alert hook (`/etc/homelab/alert.json`) as `lab`, the same path `status` and `selftest` use, and reaches the phone. | 1 min | sending it; then whether it arrived |
| 7 | `selftest` | #80 | The nightly self-test log was written in the last 26 hours and its recent lines have no `FAIL`. | 5 s | nothing |
| 8 | `skillrun` | #255 | As you, from the checkout in `$REPO`, the gated real-container test of the broker tool `skill.run` (`tests/test_skillrun.py`, `-k real_container`). An active skill's script runs in an Apple container with no network and the task workspace as the only mount, its output is marked untrusted, and the container is gone afterwards. It must pass, not skip. Without `LAB_CONTAINER_IMAGE` the step is skipped. | 1 min | nothing |
| 9 | `concurrency` | #211 | Two chat completions sent to the model server at the same moment: each one's wall time, whether they overlapped, peak memory (`top` PhysMem, sampled every second) and swap (`sysctl vm.swapusage`) before, during and after. | 1 to 3 min | nothing |
| 10 | `drills` | #78 | The two timed drills from runbook step 6, with the same commands: after `kill -9` a new supervisor within about 30 s; after `kill -STOP` the frozen one gone and replaced within 120 s, and the watchdog then says `healthy`. | up to 6 min | killing the supervisor |
| 11 | `network` | #79 | Manual. Unplug the network; the dead-man switch alert must reach the phone within ten minutes. | 10 to 15 min | nothing, you do it |
| 12 | `power` | #77, #91 | Manual. The power-pull drill: pull the plug during a task, restore power, time the lab's return, and check the task was requeued or held. The report gives the drill template path. | 20 to 30 min | nothing, you do it |

What each result means:

- **PASS** and **FAIL** come from the check's own output, never from the script's
  opinion.
- **SKIPPED** means you answered no, or it was a dry run.
- **MANUAL** means the step only printed instructions. Its result is what you
  write in the issue.

The concurrency step reports overlap from the start and end times. Two requests
sent together always overlap in wall time, so also read the ratio of the longer
to the shorter request: close to 2 means the server answered them one after the
other.

## After the session: what goes where

Each step's section in the report is self-contained. Paste it into its issue:

| Report section | Paste into |
|---|---|
| `context` | #241, with the summary table |
| `home` | #225 |
| `caffeinate` | #235 |
| `signature` | #70 |
| `backup` | #67 |
| `alert` | #79 and #80 |
| `selftest` | #80 |
| `skillrun` | #255 and #181 |
| `concurrency` | #211 |
| `drills` | #78 |
| `network`, with your two times | #79 |
| `power`, with your timings | #77 and #91 |

Drills also need a committed record. Copy `ops/drills/TEMPLATE.md` to
`ops/drills/log/<UTC timestamp>-<drill>.md` for the kill, freeze and power-pull
drills, fill it in from the report, and commit it (see
[the drills README](drills/README.md)). Tick the matching items in
[the setup checklist](mac-mini-setup.md) only for a PASS.

The report holds command output from the machine, such as the deployed commit,
file names in the home folder if `lab` could list them, and the alert hook's
command line. It holds no keys or tokens: the Telegram token lives in a separate
file the script never reads. Read it once before pasting it anywhere public.
