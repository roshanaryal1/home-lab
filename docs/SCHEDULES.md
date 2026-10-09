# Schedules

A schedule starts a task on a calendar rule, so the lab can work on a
standing goal while you are away. It can start work. It can never approve
work. This is feature 5 in [FEATURE-PLAN.md](FEATURE-PLAN.md), issue
[#361](https://github.com/roshanaryal1/home-lab/issues/361).

## Add one

You sign every schedule with the operator key, the same key that signs
approvals.

```sh
uv run lab schedule add morning-digest --daily 07:30 --tz Pacific/Auckland \
  --kind git.read --title "Morning digest" --payload '{}' \
  --key ~/.lab-operator/operator.key --by you
```

- `--daily HH:MM`, `--weekly DAY HH:MM` (mon to sun) or `--every-minutes N`
  with N from 15 to 1440. Pick one.
- `--tz` is an IANA time zone. A daily 07:30 stays 07:30 across daylight
  saving. On the night the clocks go forward, a time that does not exist
  that day (02:30 in New Zealand) runs an hour later. On the night they go
  back, a time that happens twice runs once, the first time.
- `--kind` is the task kind the schedule starts, such as `git.read`,
  `workspace.files` or `web.summary`. If the supervisor has no handler for
  the kind, the task is cancelled with "no handler", like any other task.
- `--tier` is `autonomous` (the default), `notify` or `approve`. An
  approve-tier task waits for your signed approval, as every approve-tier
  task does.
- `--weight` is `light` (the default) or `heavy`.

On the deployed Mac, add `--db /var/homelab/lab.db` before `schedule`, and
run it as the `lab` account with your key, as you do for approvals.

## See and remove

```sh
uv run lab schedule list
uv run lab schedule remove morning-digest --by you
```

`list` shows each live schedule, its rule, kind, tier, when it is next due,
when it last fired and whether it is signed. Removing needs no key, because
it only takes work away. A removed schedule keeps its row, and its nonce
goes on an append-only list that every firing checks, so its signed spec
cannot be put back. To change a schedule, remove it and add a new one.

## When it runs

`lab tick` checks schedules first on every pass (every 5 minutes on the
deployed Mac) and queues the task of each one that is due. The supervisor
runs it with the handlers it has, as it runs any task. So a due schedule
starts within about 5 minutes of its time.

- Missed slots fire once. If the Mac was off for three days, a daily
  schedule runs once when it comes back, not three times.
- If the task from the last firing is still queued, running or waiting for
  approval, the slot is skipped and logged, so work does not pile up.
- A firing is one transaction. The next slot, the task and the record of
  it are written together, so a crash leaves all of them or none, and the
  next pass fires a slot the crash undid. A slot never fires twice.

Each firing writes an event: `schedule_fired`, `schedule_skipped` or
`schedule_refused`. The task it creates has an `operator` origin with the
source id `schedule:<name>`, so you can tell it from work you queued by
hand.

## What keeps it safe

- With the operator public key configured (the deployed default), every
  firing checks the signature over the whole spec: name, rule, time zone,
  kind, title, payload, weight, tier, who added it, when, and a random
  nonce. A row nobody signed, or one changed after signing, never creates a
  task. It is logged as refused and parked: it never comes due again, and
  `list` shows `next never (refused)`. Remove it and add it again. One
  refused schedule never stops the others.
- The nonce is unique in the table, so a copied or replayed spec is refused
  by the database.
- A scheduled task carries its tier like any other. An approve-tier step
  parks for your signature. A schedule cannot approve anything.
- The agent has no command or tool that adds a schedule. Only `lab schedule
  add` with your private key does.
- Without an operator key (tests, a laptop), schedules fire unsigned, and
  every firing records `signed: false`.

## The claim to register

The safety tests in `tests/test_schedule.py` are marked `safety`. They are
the draft claim S1, to register before this ships in a release:

> S1: an unsigned, altered or replayed schedule never creates a task, and a
> task a schedule creates never reaches an approve-tier tool without the
> owner's signature.
