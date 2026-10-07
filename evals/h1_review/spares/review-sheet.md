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

## spare-01

**Claim 1** (finding): The rule that an approval decision is final was not covered by a test
- supports: source `issue-221` (incident): A coverage run showed the idempotency paths untested.

Route: ______

## spare-02

**Claim 1** (finding): Status text in several documents falls behind what is deployed
- supports: source `issue-210` (incident): Documents still described the lab as not deployed.
- supports: source `issue-246` (incident): A README row still described keyless startup.
- supports: source `issue-285` (incident): The README said alerts, the dead-man switch and the nightly backup were not installed after they were.

**Claim 2** (mechanism): The same status is written by hand in several places
- no evidence

Route: ______

## spare-03

**Claim 1** (finding): The README still said the supervisor allows keyless startup after that had been changed
- supports: source `issue-246` (incident): README status row 8 described a state the code no longer had.

Route: ______

## spare-04

**Claim 1** (finding): Two simultaneous requests to the model server are safe for memory
- supports: source `issue-211-2026-09-30` (measurement): Swap did not move and the server kept the same process id.
- contradicts: source `issue-211-2026-09-30-peak` (measurement): Wired memory reached about 25.8 GB on the 32 GB machine, closer to its limit than planned.

Route: ______

## spare-05

**Claim 1** (measurement): The three reviewed handlers peak at about 36 MB and 0.05 s of CPU
- supports: source `report-2026-10-07-ceilings` (measurement): Five repeats of each handler's sample tasks on the Mac mini peaked at 36.3 MB and 0.054 s.
- supports: source `report-linux-ci-ceilings` (measurement): The same tool on a Linux machine suggested limits of 102 MB and 1 s.

Route: ______

## spare-06

**Claim 1** (finding): The first scheduled backup failed and the failure alert reached the phone
- supports: source `issue-67-comment` (incident): macOS refused the lab interpreter access to the removable volume, the job exited 1 and sent an alert.
- supports: source `report-2026-10-07-backup-alert` (incident): The operator saw the message on the phone.

Route: ______

## spare-07

**Claim 1** (finding): One module had no caller and no test
- supports: source `issue-232` (incident): lab/slice.py was not imported anywhere and nothing tested it.
- supports: source `pr-237` (incident): It was removed because lab tick superseded it.

Route: ______

## spare-08

**Claim 1** (finding): Review comments on three pull requests found real defects in code that had passed the tests
- supports: source `pr-275` (incident): The watchdog fallback also matched a process that only carried the supervisor's words in its arguments.
- supports: source `pr-274` (incident): The guard against a bare shebang scanned only top-level test files.
- supports: source `pr-279` (incident): The host-path cases referred to paths the guest cannot see, so they could pass without testing them.

**Claim 2** (mechanism): The tests checked the cases their author thought of
- no evidence

Route: ______

