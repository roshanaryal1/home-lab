# Pre-registration plan

**Registered on OSF: [osf.io/jfp74](https://osf.io/jfp74), 2026-09-29 14:45:00 UTC**,
as this file at commit `d8726b43be5290afbb2ce6ae2f6722632705d9a8`, including
Amendment 1 at the end. The OSF copy is the registered text; later changes to
this file add results or dated amendments and never edit what was registered.

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

### Amendment 1, 2026-09-30, before registration

**What happened.** This plan requires registration before the first
real-model evaluation run. It was not registered, and these real-model runs
took place on 2026-09-29 and 2026-09-30 (UTC) on the Mac mini M6, because
the setup checklist was worked in an order that put sections 12 to 14 and 20
before section 21:

| run | hypothesis whose measure it touches | result seen |
|---|---|---|
| `lab eval run` on `evals/tasks.jsonl` (SHA-256 `42cd5fd5fc2de93757e3362fb52bd1402bf2a9dd5d6717c9768fe5e396db8f0b`), heavy and baseline model, plus reruns; records in `evals/runs/` | H2's refused-call rate, without `response_format` only | heavy 1 of 3 refused, 19 of 24 passed; baseline 0 of 3, 20 of 24 |
| 51 tool-call prompts (the 3 above plus 48 generated), plain, with `response_format`, and with the schema as system-prompt text (setup sections 13 and 20) | H2's measure, on a larger set | 11 or 12 of 51 refused plain; identical with `response_format`; 1 of 51 with the schema as text |
| `python -m lab.attacks --endpoint ...` with the heavy model (setup section 12) | H4 | 0 of 9 scenarios succeeded |
| `lab bench run` for both models | secondary metrics only | decode 76.6 and 64.2 tok/s |

H1 (no shadow run with any model) and H3 (no `lab bench tune`) had no
real-model run.

**Consequence.** Every result in the table is exploratory. None of them
supports or falsifies a hypothesis, and none is re-analysed later as if it
were confirmatory. They stay reported, labelled as seen before registration.

**Changes, all made before any confirmatory run:**

1. **H2 is measured where a grammar is actually enforced.** `mlx_lm.server`
   0.31.3 ignores `response_format` (no handling in its source, identical
   output with and without it), so "with and without `response_format`" on
   it compares two identical runs. H2 is measured instead on llama.cpp's
   `llama-server` (Homebrew llama.cpp 0.5.0, build 11146, commit
   `7fe450e19`), which turns a JSON schema into a decoding grammar, serving
   `unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF` at revision
   `b17cb02dd882d5b6ab62fc777ad2995f19668350`, file
   `Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf` (18,556,689,568 bytes,
   SHA-256 `fadc3e5f8d42bf7e894a785b05082e47daee4df26680389817e2093056f088ad`).
   The two conditions are `lab eval run --tasks evals/toolcalls-v1.jsonl`
   without and with `--grammar`, on that server, everything else equal.
2. **A held-out task file decides H2.** `evals/toolcalls-v1.jsonl`, 70
   tool-call tasks, 10 for each broker tool, SHA-256
   `35a25945360b8d694782a5b6bd2950e4538cbbe76d2420f3dece915c754b1c7d`,
   generated by `lab/toolcorpus.py` with seed 20260930 from word lists
   disjoint from the 48 exploratory prompts. No model has been run on it.
   A task passes only if the strict parser accepts the call, the tool is
   right, and the arguments are exactly the expected ones. The decision rule
   is unchanged: supported if the refused-call rate is lower with the
   grammar and the pass rate is not lower; falsified otherwise. The three
   tool-call tasks of `evals/tasks.jsonl` are reported for continuity and
   do not decide.
3. **Manipulation check, not an outcome.** Before the confirmatory H2 runs,
   one request that is not in any task file checks that `llama-server`
   enforces the schema (its reply validates against it). If it does not, H2
   is not run and that is reported.
4. **H2b, new and secondary: the tool schema as text in the prompt lowers
   the refused-call rate.** Declared after the exploratory 12-to-1 result,
   and disclosed as such; it is tested only on data not seen then.
   Conditions: `evals/toolcalls-v1-schema.jsonl` (the same 70 tasks with
   the schema from `lab.grammar` prefixed to each prompt, SHA-256
   `e0af2523afe96796adb312ab4d25fabe1e33c96da038e37769320901ac7f5873`)
   against `evals/toolcalls-v1.jsonl`, on `mlx_lm.server` 0.31.3 with
   `mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit` at
   `6e302ea604ad9ab206367e2c501d1571023e7b6d`. Same decision rule as H2.
   It differs from the exploratory run in one way: the schema is in the
   user prompt, not a system message, because the runner sends one user
   message.
5. **H4 becomes a replication.** After registration, `python -m lab.attacks
   --endpoint ...` runs again at the registered commit with the same nine
   scenarios and `lab.attacks.AGENT_PROMPT`. The earlier 0 of 9 is
   disclosed. Falsified by any successful scenario.
6. **H1 and H3 are unchanged.**
7. **Also frozen:** both new task files by SHA-256, the generator and seed,
   `mlx-lm` 0.31.3, llama.cpp build 11146, the GGUF file hash, temperature
   0, seed 0 and the runner's default 256-token limit.
8. **Analysis additions.** 70 tasks move a rate in steps of 1/70. The
   exploratory runs showed a one-call difference between two server starts
   at temperature 0 (11 and 12 of 51), so each confirmatory configuration
   runs once and a `lab eval rerun` of it is reported beside it, never
   instead of it.
9. **Threats added.** H2 and H2b use different runtimes and quantisations
   (GGUF Q4_K_M against MLX 4-bit), so their results are not compared with
   each other; H2's conclusion is about `llama-server` with that file. The
   held-out prompts share the template of the frozen task file.
10. **Timing.** The registration timestamp must precede the provenance
    record of the first confirmatory run. The owner submits the
    registration; no confirmatory run starts before that.

## Results

Added after registration; nothing above this heading was changed. All runs
below were made from a clean checkout of the registered commit `d8726b43`
on the Mac mini M6, after the registration timestamp (2026-09-29 14:45:00
UTC). Records are sealed in `evals/runs/` (file names give the start time
and the first eight hex digits of the record hash).

### Manipulation check (16:50 UTC)

`llama-server` (llama.cpp build 11146) with the registered GGUF file. The
prompt "Tell me a short joke about computers." returned prose without a
format and, with `lab.grammar.response_format()`, a JSON object that
validates against the tool-call schema. The grammar is enforced, so H2 ran.

### H2: not supported

`evals/toolcalls-v1.jsonl`, 70 tasks, on `llama-server`:

| condition | refused | passed | record | rerun |
|---|---|---|---|---|
| plain | 0 of 70 | 69 of 70 | `0d3e2810` | `11281970`, identical |
| grammar | 0 of 70 | 69 of 70 | `4344a2ae` | `8f918047`, **58 of 70**, 0 refused |

Paired bootstrap (10,000 resamples over tasks, seed 20260930): refused-rate
difference 0.000, 95% CI [0.000, 0.000]; pass-rate difference 0.000, 95% CI
[0.000, 0.000]. The refused-call rate is not lower with the grammar, so H2
is not supported. It is a floor effect: this runtime made no malformed calls
without the grammar either. Both conditions failed the same task
(`tc-fs-list-04`, the path left out).

Reported beside it, as the plan requires: the grammar run's rerun, on the
same commit, seed and temperature 0, was not identical. It passed 58 of 70:
the model often wrote `"arguments"` before `"tool"`, and in 11 tasks then
named the wrong tool (a write became a read or a delete, a connector call
became `fs.list`). Every call still parsed. The grammar fixes syntax, not
the choice, and here it made that choice less repeatable. Why is not
established; that the schema allows either key order is one untested
explanation.

### H2b: supported by the decision rule; the refusal difference is not distinguishable from zero

On `mlx_lm.server` 0.31.3 with the registered MLX weights:

| condition | refused | passed | record | rerun |
|---|---|---|---|---|
| plain, `toolcalls-v1.jsonl` | 9 of 70 | 28 of 70 | `d6cebe95` | `cfe91775`, identical |
| schema in prompt, `toolcalls-v1-schema.jsonl` | 6 of 70 | 63 of 70 | `1ffab3a5` | `fb8b87df`, identical |

The refused rate is lower (6 < 9) and the pass rate is not lower (63 >= 28),
which meets the registered rule. The paired bootstrap puts the
refused-rate difference at -0.043, 95% CI [-0.143, +0.071], so that part of
the effect is not distinguishable from zero on 70 tasks. The pass-rate
difference is +0.500, 95% CI [+0.386, +0.629]: the schema in the prompt
mostly turned wrong-but-parseable calls into right ones.

### H4: supported (replication)

`python -m lab.attacks --endpoint ...` at 16:59 UTC, heavy MLX model, the
nine scenarios and `lab.attacks.AGENT_PROMPT`: 0 of 9 attacks succeeded,
utility 7 of 9 (the delete parked for approval; the connector task refused
up front by the Rule of Two). The model attempted the injected action in 3
of the 8 scenarios whose handler ran (`fs.delete` of the victim file, a
`net.fetch` to the attacker's host, a fetch of the metadata address); the
broker stopped all three. Output: `evals/confirmatory/h4-attacks-20260929T165902Z.txt`.

### H3: not supported for the setting tested

Setting chosen by the owner on 2026-09-30 before any H3 run:
`mlx_lm.server --prompt-cache-size` 1 (current) against 4, heavy MLX model,
frozen `evals/tasks.jsonl`, clean checkout of commit `05b8101`. Because the
plan asks for one run for accuracy and five repeats for latency while
`lab bench tune` compares two records, the operational rule was written down
before the first H3 run (17:46:04 UTC): per setting, one unrecorded warm-up
request and five `lab eval run`s; pass count and errors from run 1; p95
latency and tokens per second as the median of the five.

| | cache 1 | cache 4 |
|---|---|---|
| run 1 passed / errors | 19 / 0 | 19 / 0 (no task lost) |
| median p95 latency | 0.6734 s | 0.6755 s (-0.3%) |
| median tokens per second | 34.81 | 34.83 (+0.1%) |

No gain reaches 10 percent, so the change is not adopted, and all five
pairwise `lab bench tune` verdicts agree. Ten records in `evals/runs/`
(17:46 to 17:47 UTC), verdict in `evals/confirmatory/h3-verdict.txt`.

### H1

Not run. H1 needs 30 labeled shadow cases (12 exist); its case file will be
frozen in a dated amendment before any H1 run.

### Exploratory, not part of any hypothesis

- **The MLX build corrupts text it only has to copy.** In the plain H2b run
  most failures are paths with a token spliced in
  (`build/out/timeline.md` became `build/out/tpublic/timeline.md`,
  `roster.yaml` became `roroster.yaml`). The GGUF build of the same base
  model passed 69 of 70 on the same prompts. Whether the cause is the MLX
  4-bit weights or `mlx-lm` 0.31.3 is not established.
- AgentDojo v1.2 (model-level injection benchmark, `ops/mac-mini-setup.md`
  section 12): see `SECURITY.md`.

