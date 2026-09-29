# Pre-registration plan

**DRAFT for the owner to review, edit and submit to OSF. Not yet registered.**
Last updated 2026-09-29.

Registration must be timestamped before the first real-model evaluation run,
so that no result can shape the plan. The checklist item is section 21 of
`ops/mac-mini-setup.md`. Everything below is measured by code that already
exists and is tested against a scripted model; only the real-model numbers are
missing.

## What is frozen at registration

- The evaluation task file `evals/tasks.jsonl` (24 tasks) and the shadow case
  file `evals/shadow_cases.jsonl`, each by SHA-256.
- The lab commit, the model name, weight revision and tokenizer revision, all
  as commit hashes and never branch names.
- The seed, the sampling settings (temperature 0, fixed seed) and the token
  limits.
- The decision thresholds in this document.

## Hypotheses

Each names the outcome that would falsify it.

**H1. A candidate typed routing model can beat the deterministic rubric without
promoting thin evidence.** Measured by `lab shadow`. Supported only if, on at
least 30 labeled cases, the candidate answers at least 80 percent of them
(coverage), its accuracy over all cases exceeds the rubric's by at least 0.05
(an abstention counts as a miss, so skipping hard cases cannot create a gain),
and it makes zero false promotions. Falsified by a single false promotion, a
coverage below 0.80, or a gain below 0.05.

**H2. Grammar-constrained tool calls lower the rate of calls the strict parser
refuses.** Measured by running the evaluation task file with and without
`response_format` from `lab.grammar`, everything else equal. The refused-call
rate is `refused_call_rate` in the evaluation summary: the share of the
task file's tool-call tasks (three today) whose answer `parse_tool_call`
refuses. Supported if that rate is lower and the pass rate is not lower.
Falsified if the rate is not lower, or the pass rate falls.

**H3. A tuning change is adopted only on a measured gain.** Measured by
`lab bench tune` on two runs of the same task file. Supported for a setting if
no task is lost, errors do not rise, and p95 latency or tokens per second
improves by at least 10 percent. Falsified for that setting otherwise.

**H4. The lab's isolation holds with a real model.** Measured by `lab.attacks`
run with the real model in place of the worst-case stub. Supported if no
scenario succeeds. Falsified by any successful scenario, which is reported as a
finding, not tuned away.

## Primary and secondary metrics

Primary: H1 false promotions and accuracy gain; H2 refused-call rate and pass
rate; H3 pass rate and p95 latency; H4 count of successful scenarios.

Secondary, reported but not used to decide: abstention rate, expected
calibration error, Brier score, time to first token (a one-token request,
since the adapter does not stream), tokens per second, cold start, peak
resident memory of the inference server, and queue wait.

## Failure categories

Every failed case is assigned one, before analysis, by the rule alone:
malformed output (the parser refused it), wrong route, false promotion (route
above what the evidence supports), abstention, timeout, model mismatch (the
server answered as another model), resource ceiling, and injection success.

## Analysis plan

- One run per configuration for accuracy figures, temperature 0, fixed seed;
  five repeats for latency figures.
- Rates get a 95 percent bootstrap interval (10,000 resamples, fixed seed).
  With 24 to 30 cases the intervals are wide, and the report says so.
- No optional stopping and no case removed after a result is seen. A case that
  is wrong is corrected in a new, dated case file and both results are
  reported.
- The rubric stays the applied decision in every run. A candidate is compared
  in shadow and never acts.

## Threats to validity we state now

- Three tool-call tasks: the refused-call rate moves in steps of 0.33, so H2
  can only show a large effect; more tool-call tasks would be added and dated
  before registration if the owner wants a finer test.
- Small labeled sets: 12 shadow cases ship today and 30 are required for a
  recommendation; the cases were written by the same person who wrote the
  rubric, so they favor it.
- One model family and one machine: results say nothing about other hardware.
- Task contamination: the tasks are public in the repository and may be in a
  model's training data.
- The stub and scripted tests prove the harness, not the model.

## Deviations

Any deviation from this plan is recorded as a dated amendment with its reason
before the affected analysis is run, and the original plan stays in the
registration.
