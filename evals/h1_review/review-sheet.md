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

**Claim 1** (finding): Reruns change answers
- supports: source `rec-1` (incident): Rerun differed in 11 tasks.
- supports: source `rec-2` (incident): Rerun differed in 3 tasks.
- supports: source `rec-3` (incident): Rerun differed in 5 tasks.

**Claim 2** (mechanism): Key order is free in the schema
- supports: source `rec-1` (incident): Arguments came before the tool name.

Route: ______

## case-02

**Claim 1** (finding): Dashboard shows stale counts
- supports: source `issue-12` (incident): Stale counts on Monday.
- supports: source `issue-12` (incident): Stale counts again on Tuesday.
- supports: source `issue-12` (incident): Still stale on Wednesday.

**Claim 2** (mechanism): The page cached a read-only snapshot
- supports: source `pr-13` (incident): Cache removed.

Route: ______

## case-03

**Claim 1** (finding): Backups skip the WAL file
- supports: source `issue-31` (incident): Restore missed recent rows.
- supports: source `issue-44` (incident): Restore missed the last hour.

**Claim 2** (mechanism): The copy ran before the checkpoint
- supports: source `pr-50` (incident): Backup now checkpoints first.

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

**Claim 1** (finding): An entry point built a supervisor without the operator key, so it would run tasks with unsigned approval
- supports: source `issue-190` (incident): lab tick built a Supervisor with no key.
- supports: source `issue-200` (incident): An audit of every entry point that builds a Supervisor was needed to find the others.

Route: ______

## case-06

**Claim 1** (finding): Tests and instructions break when the clone path contains a space
- supports: source `issue-273` (incident): Two MCP tests failed on the Mac mini, whose clone path has a space, and passed on CI.
- supports: source `report-2026-10-06-session` (incident): A documented cd into a home-lab folder failed because the clone is not at that path and the path has a space.

**Claim 2** (mechanism): A shebang line cannot carry an interpreter path that has a space
- supports: source `pr-274` (incident): The test stand-in now uses an sh wrapper with the quoted path, and the two tests pass.

Route: ______

## case-07

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

## case-08

**Claim 1** (finding): The backup to the removable volume needed three separate permissions
- supports: source `issue-67` (incident): The volume was mounted without owners, so the folder could not be given to the lab account.
- supports: source `report-2026-10-07-backup` (incident): After a remount the encrypted volume had to be unlocked again.
- supports: source `issue-67-comment` (incident): The scheduled job was refused by macOS until the lab interpreter was given Full Disk Access.

Route: ______

## case-09

**Claim 1** (measurement, checked by a person): Schema text raises correct calls
- supports: source `run-m` (measurement): The run recorded a result.
- supports: source `run-c` (control): The control run recorded a result.

Route: ______

## case-10

**Claim 1** (finding): Some tests failed intermittently because of a race in the test itself
- supports: source `issue-198` (incident): A lease-loss test read the pid file before the worker had written it.
- supports: source `issue-229` (incident): A redirect test reset the connection on macOS CI, reproduced 119 times in 400.

**Claim 2** (mechanism): The tests did not wait for the other side to finish its part
- supports: source `pr-230` (incident): The server now reads the request body before it replies, and the reset no longer occurs.

Route: ______

## case-11

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

## case-12

**Claim 1** (finding): The nightly self-test failed every night on its safety tests
- supports: source `issue-270` (incident): The log showed FAIL safety_tests with no detail on every run since 2026-09-30, and an alert was sent each night.

**Claim 2** (mechanism): pytest was not installed in the deployed environment
- supports: source `report-2026-10-07-deploy` (incident): python -m pytest --version as the lab account answered: No module named pytest.
- supports: source `report-2026-10-07-selftest` (measurement): After installing it, the same command passed 636 safety tests in 90 seconds.

Route: ______

## case-13

**Claim 1** (finding): A frozen supervisor was not replaced by the watchdog
- supports: source `issue-271` (incident): After kill -STOP the process was still there after 182 s; the target was 120 s.

**Claim 2** (mechanism): The watchdog only judged the pid named in the heartbeat file
- supports: source `pr-275` (incident): A supervisor frozen before its first heartbeat left a file naming a dead pid, so the watchdog took no action; it now also finds the supervisor by its command line.

Route: ______

## case-14

**Claim 1** (finding, checked by a person): The lab account could list the operator's home folder and its Public folder
- supports: source `report-2026-10-06-session` (measurement): As lab, ls of the home folder and ls of the Public folder both listed files; after chmod 700 on the home folder both said Permission denied.

Route: ______

## case-15

**Claim 1** (measurement, checked by a person): The server ignores the schema option
- supports: source `s-b` (baseline): The baseline run recorded a result.
- supports: source `s-c` (control): The control run recorded a result.
- supports: source `s-i` (incident): Identical output with and without it.

Route: ______

## case-16

**Claim 1** (finding): A trailing comment on a pasted shell line breaks it in macOS's default zsh
- supports: source `issue-208` (incident): A variable assignment followed by a # comment left the variable empty, and a command passed the comment words as arguments.

Route: ______

## case-17

**Claim 1** (finding): The watchdog restarts a frozen supervisor
- contradicts: source `drill-9` (incident): The frozen PID was still present.

Route: ______

## case-18

**Claim 1** (finding): Swap grows during long prompts
- supports: source `issue-70` (incident): Swap rose at 40K tokens.
- supports: source `issue-71` (incident): Swap rose at 60K tokens.
- supports: source `issue-72` (incident): Swap rose at 75K tokens.

**Claim 2** (mechanism): The prompt cache keeps old requests
- supports: source `pr-73` (incident): Cache capped to one entry.
- contradicts: source `issue-74` (incident): Swap still grew with the cap on.

Route: ______

## case-19

**Claim 1** (finding): Downloads stall halfway
- supports: source `log-1` (incident): Stalled at 65%.
- supports: source `log-2` (incident): Stalled at 10.4 GiB.
- supports: source `log-3` (incident): Stalled again after restart.
- supports: source `log-4` (incident): Stalled on a second model.

**Claim 2** (mechanism): The default transfer protocol hangs on this network
- supports: source `run-x` (incident): Plain HTTP downloads finished.

Route: ______

## case-20

**Claim 1** (finding): pipefail is needed before test-gated commits
- supports: source `commit-1` (incident): A failing test was committed through a pipe.

Route: ______

## case-21

Route: ______

## case-22

**Claim 1** (finding): Checks built from a list of bad forms keep missing a variant of the bad form
- supports: source `issue-212` (incident): The egress address check let NAT64, IPv4-compatible and site-local IPv6 addresses through.
- supports: source `issue-215` (incident): Hook and start-up file protection missed nested paths and case variants.
- supports: source `issue-219` (incident): The secret redactor missed JSON-escaped, lowercase-percent and HTML-escaped echoes of a credential.

**Claim 2** (mechanism): Each check enumerated the forms its author thought of
- no evidence

Route: ______

## case-23

**Claim 1** (finding): The model server queues two simultaneous requests one after the other
- supports: source `issue-211` (incident): The code reading assumed one request at a time.
- contradicts: source `report-2026-10-06-session` (measurement): Two requests sent within 1 ms of each other both finished together at 36.59 s, so they ran together.

Route: ______

## case-24

**Claim 1** (mechanism): plutil -replace inserts into arrays
- supports: source `inc-5` (incident): A second argument appeared.

Route: ______

## case-25

**Claim 1** (measurement): Thirty hostile scripts could not reach the network, the host or outlive their container
- supports: source `report-2026-10-07-m5` (measurement): 30 cases ran in Apple's container with zero failures, graded from outside the container.
- supports: source `report-2026-10-07-m5-control` (control): A connection made from the host to the listener was counted, and a background process wrote its marker while its container was alive.

Route: ______

## case-26

**Claim 1** (finding): Links break after copying
- supports: source `c-1` (incident): Two links dangled.
- supports: source `c-2` (incident): Another dangled.
- supports: source `c-3` (incident): A third.
- contradicts: source `c-4` (incident): A later copy had none.

**Claim 2** (mechanism): The cache links blobs into a shared store
- supports: source `c-5` (incident): Links point outside the model folder.

Route: ______

## case-27

**Claim 1** (finding): A running shell command could outlive the task that started it
- supports: source `issue-223` (incident): A command that called setsid escaped the process-group kill.
- supports: source `issue-228` (incident): An emergency stop did not end a shell.run command that was already running.

**Claim 2** (mechanism): The kill reached the process group and the waiting coroutine, not the command
- supports: source `pr-231` (incident): A cancel flag the run loop checks now ends the command within a second, and the tests show it.

Route: ______

## case-28

**Claim 1** (finding): The keep-awake service can run as the lab account
- supports: source `report-2026-10-06-session` (measurement): As lab, caffeinate -i held a PreventUserIdleSystemSleep assertion for 15 seconds, started from an operator's login session.

Route: ______

## case-29

**Claim 1** (measurement, checked by a person): model-E copies paths correctly
- supports: source `run-model-E` (measurement): The run recorded a result.
- supports: source `run-model-B` (baseline): The baseline run recorded a result.
- supports: source `run-model-G` (control): The control run recorded a result.

**Claim 2** (measurement, checked by a person): model-E keeps the same footprint
- supports: source `fp-model-E` (measurement): The run recorded a result.
- supports: source `fp-model-B` (baseline): The baseline run recorded a result.
- supports: source `fp-idle` (control): The control run recorded a result.

Route: ______

## case-30

**Claim 1** (finding): Things got slower
- no evidence

**Claim 2** (mechanism): Memory is the cause
- no evidence

**Claim 3** (measurement): It is 10% slower
- no evidence

Route: ______

