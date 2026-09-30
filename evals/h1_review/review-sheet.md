# Review sheet: what should each research task become?

For each case, decide the route it deserves, in your own judgement, from the claims and evidence shown. Do not look for the drafted labels: they are in the repository, so please finish this sheet first.

## The routes

- **post**: One incident, commit or lesson worth a short note. At least one claim is backed by evidence that supports it.
- **blog**: A pattern seen in more than one incident, together with an explanation of why it happens.
- **paper**: A measurement that holds up when it is checked against a baseline and a control.
- **insufficient_evidence**: There are claims, but not enough behind them for even a short post.
- **no_artifact**: There is nothing to say: no claims at all.

Evidence either *supports* or *contradicts* its claim. A claim marked *checked by a person* has been confirmed after review.

Copy `answers-template.json` to `answers.json` and replace each null with one route name.

## case-01

**Claim 1** (mechanism): plutil -replace inserts into arrays
- supports: source `inc-5` (incident): A second argument appeared.

Route: ______

## case-02

**Claim 1** (measurement, checked by a person): DWQ copies paths correctly
- supports: source `run-dwq` (measurement): The run recorded a result.
- supports: source `run-4bit` (baseline): The baseline run recorded a result.
- supports: source `run-gguf` (control): The control run recorded a result.

**Claim 2** (measurement, checked by a person): DWQ keeps the same footprint
- supports: source `fp-dwq` (measurement): The run recorded a result.
- supports: source `fp-4bit` (baseline): The baseline run recorded a result.
- supports: source `fp-idle` (control): The control run recorded a result.

Route: ______

## case-03

**Claim 1** (finding): Swap grows during long prompts
- supports: source `issue-70` (incident): Swap rose at 40K tokens.
- supports: source `issue-71` (incident): Swap rose at 60K tokens.
- supports: source `issue-72` (incident): Swap rose at 75K tokens.

**Claim 2** (mechanism): The prompt cache keeps old requests
- supports: source `pr-73` (incident): Cache capped to one entry.
- contradicts: source `issue-74` (incident): Swap still grew with the cap on.

Route: ______

## case-04

**Claim 1** (finding): Tool calls invent parameters
- supports: source `run-a` (incident): Invented 'format'.
- supports: source `run-b` (incident): Invented 'public'.
- supports: source `run-c` (incident): Invented 'json'.

**Claim 2** (mechanism): The quantisation damages rare tokens
- no evidence

Route: ______

## case-05

**Claim 1** (finding): Backups skip the WAL file
- supports: source `issue-31` (incident): Restore missed recent rows.
- supports: source `issue-44` (incident): Restore missed the last hour.

**Claim 2** (mechanism): The copy ran before the checkpoint
- supports: source `pr-50` (incident): Backup now checkpoints first.

Route: ______

## case-06

**Claim 1** (finding): Downloads stall halfway
- supports: source `log-1` (incident): Stalled at 65%.
- supports: source `log-2` (incident): Stalled at 10.4 GiB.
- supports: source `log-3` (incident): Stalled again after restart.
- supports: source `log-4` (incident): Stalled on a second model.

**Claim 2** (mechanism): The default transfer protocol hangs on this network
- supports: source `run-x` (incident): Plain HTTP downloads finished.

Route: ______

## case-07

**Claim 1** (finding): Links break after copying
- supports: source `c-1` (incident): Two links dangled.
- supports: source `c-2` (incident): Another dangled.
- supports: source `c-3` (incident): A third.
- contradicts: source `c-4` (incident): A later copy had none.

**Claim 2** (mechanism): The cache links blobs into a shared store
- supports: source `c-5` (incident): Links point outside the model folder.

Route: ______

## case-08

**Claim 1** (measurement, checked by a person): The server ignores the schema option
- supports: source `s-b` (baseline): The baseline run recorded a result.
- supports: source `s-c` (control): The control run recorded a result.
- supports: source `s-i` (incident): Identical output with and without it.

Route: ______

## case-09

**Claim 1** (finding): The watchdog restarts a frozen supervisor
- contradicts: source `drill-9` (incident): The frozen PID was still present.

Route: ______

## case-10

**Claim 1** (finding): Dashboard shows stale counts
- supports: source `issue-12` (incident): Stale counts on Monday.
- supports: source `issue-12` (incident): Stale counts again on Tuesday.
- supports: source `issue-12` (incident): Still stale on Wednesday.

**Claim 2** (mechanism): The page cached a read-only snapshot
- supports: source `pr-13` (incident): Cache removed.

Route: ______

## case-11

**Claim 1** (finding): pipefail is needed before test-gated commits
- supports: source `commit-1` (incident): A failing test was committed through a pipe.

Route: ______

## case-12

**Claim 1** (finding): Reruns change answers
- supports: source `rec-1` (incident): Rerun differed in 11 tasks.
- supports: source `rec-2` (incident): Rerun differed in 3 tasks.
- supports: source `rec-3` (incident): Rerun differed in 5 tasks.

**Claim 2** (mechanism): Key order is free in the schema
- supports: source `rec-1` (incident): Arguments came before the tool name.

Route: ______

## case-13

**Claim 1** (measurement, checked by a person): Schema text raises correct calls
- supports: source `run-m` (measurement): The run recorded a result.
- supports: source `run-c` (control): The control run recorded a result.

Route: ______

## case-14

**Claim 1** (measurement, checked by a person): Admission refuses oversized prompts
- supports: source `adm-m` (measurement): The run recorded a result.
- supports: source `adm-b` (baseline): The baseline run recorded a result.
- supports: source `adm-c` (control): The control run recorded a result.

**Claim 2** (finding): Oversized prompts arrive often
- supports: source `i-1` (incident): A 60K prompt.
- supports: source `i-2` (incident): An 80K prompt.
- supports: source `i-3` (incident): A 100K prompt.

**Claim 3** (mechanism): Callers paste whole files
- supports: source `i-4` (incident): A caller pasted a log file.

Route: ______

## case-15

**Claim 1** (finding): Tabs leak memory
- supports: source `m-1` (incident): 16 GB in one tab.
- supports: source `m-2` (incident): A second tab grew.
- supports: source `m-3` (incident): It happened again after restart.

**Claim 2** (mechanism): A long-lived page never frees its caches
- supports: source `m-4` (incident): Reloading the page freed it.

**Claim 3** (measurement): Closing the tab frees 16 GB
- supports: source `mm` (measurement): The run recorded a result.
- supports: source `mb` (baseline): The baseline run recorded a result.
- supports: source `mc` (control): The control run recorded a result.

Route: ______

## case-16

**Claim 1** (finding): Things got slower
- no evidence

**Claim 2** (mechanism): Memory is the cause
- no evidence

**Claim 3** (measurement): It is 10% slower
- no evidence

Route: ______

