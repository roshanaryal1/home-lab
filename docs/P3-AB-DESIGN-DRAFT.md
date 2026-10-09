# P3 cheap A/Bs: design draft

**DRAFT. Not registered. Owner to approve before any run.**

Drafted 2026-10-09 (NZDT) on `origin/main` at commit `9847071`, for issue #321.
Nothing in this draft has been run. No file under `lab/` or `evals/` changed.

**Scope.** The four A/Bs in [PLAN.md](PLAN.md), section 3.2: MLX against llama.cpp,
32K against 64K context, speculative decoding on and off, and one resident model
against swap-per-task. They are not H1 to H4 in [PREREGISTRATION.md](PREREGISTRATION.md).
This draft does not change that file.

**How to read it.** Each number cites the file and section it comes from. A fact this
draft could not confirm in the repo is marked **UNVERIFIED**. A value the owner must
still choose is marked **owner to set**.

## 1. Shared rules

1. **Machine and provenance.** Mac mini M6, 32 GB, as in
   [decisions/0001-heavy-model.md](decisions/0001-heavy-model.md) ("Measured on the M6").
   Each eval record seals the lab commit, the dirty flag, the macOS build and the power
   settings (`lab/evals.py`, `collect_provenance`, lines 123 to 137).

2. **Sampling.** Temperature 0 (`lab/model.py`, payload, line 269). Seed 0 and a
   256-token output limit with a 120 second timeout per task (`lab/evals.py`,
   `make_config`, lines 244 and 245), unless an arm says otherwise.

3. **Task set.** `evals/tasks.jsonl`, 24 tasks, SHA-256
   `42cd5fd5fc2de93757e3362fb52bd1402bf2a9dd5d6717c9768fe5e396db8f0b`
   ([PREREGISTRATION.md](PREREGISTRATION.md), Amendment 1, "What happened" table).
   This set cannot test context length or decode speed (sections 4 and 5), so those
   A/Bs need their own sets.

4. **Repeats.** The H3 operating rule, written before H3 ran
   ([PREREGISTRATION.md](PREREGISTRATION.md), H3 Results). Per arm: one unrecorded
   warm-up request, then five `lab eval run` records. Pass count and errors come from
   run 1. p95 latency and tokens per second are the medians of the five runs.

5. **Threshold.** A gain counts only at 10 percent or more. This is H3's threshold and
   the default `min_gain` of `tuning_verdict` (`lab/bench.py`, line 145).

6. **No task lost.** A task is lost when it passes in the incumbent's run 1 and fails
   in the candidate's run 1. Compare the two records task by task, by id.

7. **Nondeterminism.** On one commit, the same grammar configuration passed 69 of 70,
   then 58 of 70 ([PREREGISTRATION.md](PREREGISTRATION.md), Results, H2). A pass count
   can move with no change of setting. Each arm's run 1 is reported beside its
   `lab eval rerun`, never in its place (Amendment 1, item 8).

8. **Intervals.** Rates get a 95 percent paired bootstrap interval, 10,000 resamples,
   fixed seed ([PREREGISTRATION.md](PREREGISTRATION.md), Analysis plan). Each resample
   draws task ids from the A/B's task set with replacement and keeps both arms' run-1
   result for every task drawn. Each arm's rate and the difference between them are
   computed on every resample. With 24 tasks, one task is 1/24 of the set, and the
   intervals are wide. The five-run medians are for latency and tokens per second
   only, never for rates.

9. **Peak memory footprint.** Required in all four A/Bs. The harness does not measure
   it. Section 2 and section 7 say what exists and how the draft proposes to measure it.

## 2. What the harness measures today

| Metric | What exists | Gap |
|---|---|---|
| p95 latency | Eval summary `latency_p95` (`lab/evals.py`, `summarise`, lines 211 to 213). Bench `first_token_p95` (`lab/bench.py`, lines 98 and 99). | Bench p95 over five repeats is the maximum, because `_p95` takes index `int(n * 0.95)` (`lab/bench.py`, lines 71 to 73). The A/Bs use the eval summary. |
| Tokens per second | Eval summary `tokens_per_second`: completion tokens over total request time (`lab/evals.py`, line 210). Bench `decode_tokens_per_second` (`lab/bench.py`, lines 101 to 106). | Two definitions. `tuning_verdict` reads the eval one (`lab/bench.py`, lines 160 and 161). The A/Bs use the eval one. |
| Peak memory footprint | Nothing in `lab/`. Bench reads RSS with `ps -o rss=` once, after all requests (`lab/bench.py`, lines 59 to 68 and 107 and 108). The ADR used the `footprint` command by hand, in whole GiB, plus or minus 0.5 GiB ([decisions/0001-heavy-model.md](decisions/0001-heavy-model.md), "Memory"). | No sampler and no peak. RSS stayed near 16 GB while the cache grew by about 7 GiB (ADR 0001, "Memory"). |
| Tasks passed, errors | Eval summary `passed` and `errors` (`lab/evals.py`, lines 207 and 208). | A per-task comparison needs the `results` list. |
| Context length | `lab eval run --context-tokens` defaults to 8192 (`lab/evals.py`, line 336). Bench hard-codes 8192 (`lab/bench.py`, line 205). | The value is used only for admission (`lab/model.py`, `AdmissionController.admit`, lines 164 to 167). It is not sent to the server (payload, lines 266 to 274). The server's own context setting is not in the repo (**UNVERIFIED**). |

**Admission budget.** Both harnesses build `BoundedModel` with the default budget
(`lab/evals.py`, line 236, `lab/bench.py`, line 207, `lab/model.py`, `BoundedModel.__init__`,
line 312). With `--db`, `lab eval` also takes the heavy slot the supervisor shares
(`slot_controller`, line 297, since #335). The default budget is 20,500 MB
(`lab/model.py`, line 58), and `ops/mac-mini-setup.md` section 13 says it stays 20,500 as
policy.

A request is admitted only while the weights plus `kv_bytes_per_token` times the
token count, divided by 1,000,000, plus 1, stays within the budget (`lab/model.py`,
`admit`, lines 169 to 172). With the 17,180 MB weights (`lab/model.py`, line 62) and
200,000 bytes per token (`lab/model.py`, line 89), the largest request admitted is
about 16,600 tokens of prompt plus output. This is arithmetic on those constants. It
matches the ADR's "about 16K tokens" ([decisions/0001-heavy-model.md](decisions/0001-heavy-model.md),
"Measured on the M6").

## 3. A/B 1: MLX against llama.cpp

**Question.** On this machine, does llama.cpp beat the MLX build now served on p95
latency, tokens per second, footprint or tasks passed?

**Arms.** The incumbent is MLX. The candidate is llama.cpp.

- **A, MLX (incumbent).** `mlx_lm.server` 0.31.3 on `127.0.0.1:8080` with
  `--prompt-cache-size 1`, serving `mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit-DWQ`
  at revision `cfcade7221ccd128681961446e5f7906c08cae55`
  (`ops/mac-mini-setup.md`, section 13, and ADR 0001, "Where the measurements disagree", point 5, "The served build is now the DWQ one"). No `response_format`.
- **B, llama.cpp (candidate).** `llama-server` build 11146, commit `7fe450e19`, serving
  `Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf` from `unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF`
  at revision `b17cb02dd882d5b6ab62fc777ad2995f19668350`, SHA-256
  `fadc3e5f8d42bf7e894a785b05082e47daee4df26680389817e2093056f088ad`
  ([PREREGISTRATION.md](PREREGISTRATION.md), Amendment 1, item 1). No `response_format`.
  The launch flags, port and context setting are **UNVERIFIED**: the repo does not record
  them. The H2 record used port 8090 (`evals/runs/run-20260929T165052+0000-4344a2ae.json`,
  `config.endpoint`).
- **ModelSpec, both arms.** Context 8,192 and max output 1,024, with 200,000 bytes per
  token, the values in the H2 record (`config.model`). Weights are per build: 17,180 MB
  for MLX (ADR 0001, "Measured on the M6") and 18,600 MB for the GGUF (`config.model.weights_mb`,
  record `4344a2ae`). The configured context is the same in both arms, but the admission
  headroom is not: admission counts the weights too, and the GGUF's are 1,420 MB larger.

**Note on the comparison.** The arms differ in quantisation as well as runtime (Q4_K_M
against 4-bit DWQ). A win here is a win for this pair of builds, and it cannot be given
to the runtime alone. [PREREGISTRATION.md](PREREGISTRATION.md) makes the same point for
H2 against H2b (Amendment 1, item 9).

**Task set and repeats.** `evals/tasks.jsonl`, with the repeat rule in section 1.

**Metrics.** p95 latency and tokens per second from the eval summaries, medians of five.
Peak footprint over the five runs, as section 7 measures it. Tasks passed and errors from
run 1, by task id.

**Decision rule (written now).** MLX stays unless llama.cpp meets all four conditions:

1. No task lost (section 1, item 6).
2. Errors in run 1 are not higher than MLX's.
3. Median p95 latency or median tokens per second is at least 10 percent better than MLX.
4. Peak footprint is not higher than MLX's on the whole-GiB reading. A difference under
   0.5 GiB counts as no difference (ADR 0001, "Memory").

If condition 3 is met and condition 4 is not, the result is reported and llama.cpp is
not adopted.

**Needed before it can run.**
- A written launch command for `llama-server` (flags, port, context), committed before
  the run. **UNVERIFIED**: not in the repo.
- The GGUF file on disk checked against its SHA-256 before the run.
- The footprint sampler (section 7), tested on both servers.
- The owner's decision on registration (section 10).

**What could invalidate it.**
- The quantisation differs (above).
- A build change has already moved pass counts on this task file. The plain 4-bit build
  passed 19 of 24 (`ops/mac-mini-setup.md`, section 14). The DWQ build passed 22 of 24
  (ADR 0001, "Where the measurements disagree", point 5). The build moved the count by three tasks.
- The ADR says MLX uses about 10 percent less memory and runs 15 to 30 percent faster
  (ADR 0001, "Decision", point 1). The repo has no measurement of either claim
  (**UNVERIFIED**). Do not use them as a prior.
- Run-to-run change at temperature 0 (section 1, item 7).
- Lab workers or other apps using the machine during the arms. `ops/mac-mini-setup.md`
  section 13 notes that one Safari tab held 16 GB. The repo has no switch that pauses
  model calls for a measurement (**UNVERIFIED**, not checked exhaustively).

## 4. A/B 2: 32K against 64K context

**Question.** Can this machine run 64K context inside its memory ceiling? And does 32K
against 64K change latency, tokens per second, footprint or passes on long prompts?

**Arms.** Both on the MLX DWQ build with `--prompt-cache-size 1`
(`ops/mac-mini-setup.md`, section 13, and ADR 0001, "Memory"). Each arm sets its own
ModelSpec context: 32,768 tokens (A) and 65,536 tokens (B). The server's context flag
for `mlx_lm.server` 0.31.3 is **UNVERIFIED**: the repo does not set it.

**What the ADR already measured.** The measured rows go up to 37,440 prompt tokens,
at about 23 GiB of footprint, plus or minus 0.5 GiB (ADR 0001, "Memory" table). Metal's
ceiling is 24.96 GiB. A request of about 75K tokens failed with `Insufficient Memory` and
pushed about 3 GB into swap. The 64K row was not run, because "the measured slope
predicts ~28 GiB" (ADR 0001, "Memory"). So 32K sits between measured rows, and 64K is
predicted to exceed the ceiling.

**Task set.** The 24-task file cannot test this. Every prompt in it is one short line
(`evals/tasks.jsonl`), and so is the bench prompt (`lab/bench.py`, line 33). The A/B
needs a long-prompt set:

- Lengths of 8,900, 18,015 and 37,440 prompt tokens, the lengths the ADR measured
  (ADR 0001, "Memory" table), so the new rows line up with the old ones. The 64K probe
  length is **owner to set**, because the ADR has no 64K row.
- Each prompt has an exact-match check that `grade` can run (`lab/evals.py`, `grade`,
  lines 84 to 104), for example one fact placed inside filler text.
- Generated by a seeded script and frozen by SHA-256 before any run. Precedent:
  `lab/toolcorpus.py` with seed 20260930 for H2's held-out file
  ([PREREGISTRATION.md](PREREGISTRATION.md), Amendment 1, item 2).
- Prompts per length: **owner to set**.
- Lengths counted in real tokens, with the real tokenizer. Admission counts tokens with
  an estimate that errs high (`lab/model.py`, `estimate_tokens`, line 104), so the two counts differ.

**Metrics.** Peak footprint at each length, with a fresh server per length as the ADR
did, plus the sampler. p95 latency and tokens per second only for the lengths both arms
accept, from the eval summary, so the paired numbers compare like with like. Tasks passed
per length.

Any length one arm does not accept is reported apart, as a capacity probe, not in the
paired numbers. For each such request the record says where it stopped: refused by the
lab's admission check before the server saw it, refused by the server at its context
limit, or failed in the server (for example `Insufficient Memory`). A request counts as
reaching the 32K context limit only when the server's own context is set to 32,768 for
that arm. Today admission alone refuses anything over its budget (section 2) before any
server limit is reached.

**Decision rule (written now).** This is mainly a capacity question, so feasibility comes first:

1. 64K is adopted as the working context only if every 64K request completes with no
   Metal error and no swap growth.
2. Its peak footprint at the longest length stays below 24.96 GiB, by a margin the owner
   sets before the run (**owner to set**).
3. No prompt that passes at 32K fails at 64K.

If condition 1 or 2 fails, 64K is not adopted, whatever the speed numbers say.

32K replaces the current working context of about 16K only if the owner first makes a
dated change to the admission budget (section 2), and then the 32K arm, on its own:

- completes every request up to its longest accepted length with no Metal error and no
  swap growth,
- keeps its peak footprint at that length below 24.96 GiB by the owner's margin, and
- passes every prompt that the current 16K working context passes, measured the same way
  on the same server start.

**Expected result, stated before the run.** The ADR's arithmetic puts 64K above the
ceiling, so the likely result is that 64K is not adopted. That is a prediction from the
ADR, not a result. If it holds, the 64K arm is a measured ceiling, and its task passes
are not a comparison.

**Needed before it can run.**
1. A budget decision. Under the default 20,500 MB budget, requests above about 16,600
   tokens are refused at admission (section 2), so both longer arms are refused. The
   owner must either change the budget as a dated policy change (`ops/mac-mini-setup.md`,
   section 13, says it stays 20,500), or pass a controller with a higher budget into
   `BoundedModel`. Neither harness does that today (`lab/evals.py`, line 223 and
   `lab/bench.py`, line 207).
2. A way to set the context in bench. `lab/bench.py` hard-codes 8,192 (line 205), so a
   bench record made now would seal 8,192 whatever the server did.
3. The server's context flag for each arm, written down (**UNVERIFIED**).
4. The long-prompt set, frozen by SHA-256.
5. A probe protocol for 64K: other apps closed (`ops/mac-mini-setup.md`, section 13),
   and the probe stopped at the first Metal error. Whether the owner must be present
   for the probe is **owner to set**.

**What could invalidate it.**
- Using RSS would miss the failure. RSS stayed near 16 GB while the cache grew (ADR 0001, "Memory").
- The 64K figure is an extrapolation of the measured slope. The measured rows stop at 37,440 tokens.
- Without `--prompt-cache-size 1`, earlier requests keep their cache and the admission
  accounting is wrong (ADR 0001, "Memory"). Each length needs a fresh server.
- Token counts are estimates in admission (section 2).
- Footprint readings are in whole GiB, plus or minus 0.5 GiB.

## 5. A/B 3: speculative decoding on and off

**Question.** Does a draft model, used for speculative decoding, raise tokens per second
or lower p95 latency on this machine without changing answers, and what does it cost in memory?

**What the repo has.** Nothing. The search of `lab/`, `ops/` and `docs/` finds no draft
model, no draft option in any launch command, and no code path for speculative decoding.
[PLAN.md](PLAN.md) section 3.2 names the A/B and nothing more. Whether `mlx_lm.server`
0.31.3 or `llama-server` build 11146 offers speculative decoding is **UNVERIFIED**. The
source was not checked here.

**Arms.**
- **A, off (incumbent).** The MLX DWQ build alone, as in section 3.
- **B, on.** The same server with a draft model. The draft is **owner to set**. The repo names
  one other small model, `mlx-community/Qwen3-4B-Instruct-2507-4bit` at `50d42775`,
  2.28 GB on disk (`ops/mac-mini-setup.md`, section 14). Whether it shares the heavy
  model's tokenizer is **UNVERIFIED**. A draft normally needs the target's tokenizer,
  and that requirement is **UNVERIFIED** for these builds too.
- Draft tokens per step: **owner to set**. The repo has no value.

**Task set and repeats.** The 24-task file cannot measure speed here. Every prompt asks
for a short answer: a word, a number, yes or no, or a short tool call (`evals/tasks.jsonl`).
Decode speed barely matters for those. The A/B therefore needs two sets:

- The 24-task set, for answer identity and passes, with the section 1 repeats.
- A generation-heavy set, for speed, frozen by SHA-256 before any run. It is not written yet.
  Its size and content are **owner to set**.

**Metrics.** Tokens per second (decode), p95 latency, peak footprint with the draft's
weights inside it (section 7), tasks passed, and answer identity. For answer identity,
`compare` in `lab/evals.py` (lines 290 to 301) reports which tasks changed between two records.

**Decision rule (written now).** Speculative decoding is adopted only if all four hold:

1. Answers are identical on all 24 tasks between arm A run 1 and arm B run 1
   (`identical_answers`, `lab/evals.py`, lines 290 to 301). Any changed answer blocks
   adoption. The owner reviews each changed answer and writes down why it changed, but
   that review cannot waive this condition. Nothing is adopted automatically.
2. No task lost (section 1, item 6).
3. Median tokens per second on the generation-heavy set is at least 10 percent better,
   or median p95 latency is at least 10 percent better.
4. Peak footprint with the draft included stays below 24.96 GiB, by the owner's margin
   (**owner to set**).

**Needed before it can run.**
1. A draft chosen, with full revision pins. `ModelSpec.__post_init__` refuses branch
   names and tags (`lab/model.py`, line 92 onward).
2. A tokenizer check: draft and target give the same token ids. The ADR did this kind of
   check for two MLX builds (ADR 0001, "Where the measurements disagree", point 5).
3. A launch command with the draft option (**UNVERIFIED**).
4. Admission accounting. `AdmissionController` counts one weights figure per model name
   (`lab/model.py`, `load` at line 140, and `admit` at line 158). A draft in the same
   server is not counted unless the arm adds its weights to `weights_mb`. The owner
   decides which approach to use.
5. The generation-heavy set (above).

**What could invalidate it.**
- Acceptance rates depend on the prompts. A short-answer set shows little either way.
- Only temperature 0 is measured (`lab/model.py`, line 269). Other settings may behave differently.
- A speed gain that comes with changed answers is not a gain under condition 1.
- If the draft's weights are missing from admission, the arm looks cheaper than it is.

## 6. A/B 4: one resident model against swap-per-task

**Question.** Is it better to keep one model loaded, or to load and unload a model for each task?

**What the repo has.** The swap manager is not built (`README.md`, roadmap row 5,
`docs/SUBSYSTEMS.md`, "Model adapter and swap manager", and `ops/mac-mini-setup.md`, section 8).
`AdmissionController` records residency with `load` and `unload` (`lab/model.py`, lines
140 to 151), but nothing starts or stops an inference server. Bench's "cold start" is the
first request on a server that is already running (`lab/bench.py`, lines 93 and 94). So
the repo has no model load time. How long a load takes on this machine is **UNVERIFIED**.

The baseline model has run beside the heavy model "with no swap growth"
(`ops/mac-mini-setup.md`, section 14). That run recorded RSS, not the footprint of the
pair. Whether the pair fits under the ceiling is not measured.

**Arms.**
- **A, one resident model (incumbent).** The MLX DWQ heavy model stays loaded. Every task runs on it.
- **B, swap-per-task.** A task's model is started, used and stopped. This needs a second
  model and a task-to-model assignment.
- **Assignment:** **owner to set**, frozen before the run. If every task goes to the
  heavy model, arm B does no swapping and the arm means nothing.
- **Second model:** not chosen (`README.md`, row 3: "the light model is not chosen yet").
  The 4B baseline is the only candidate the repo names (`ops/mac-mini-setup.md`, section 14).

**Task set and repeats.** The 24-task set with the frozen assignment, the section 1
repeats, and one timed load for each swap.

**Metrics.** Wall time for the whole task set, loads included. p95 per-task latency,
loads included. Load time per swap (new, with no repo value). Peak footprint (section 7),
and the sampler's readings showing that a swapped-out model leaves the footprint. Tasks passed.

**Pass counts differ by model.** The baseline passed 20 of 24 and the heavy model 19 of
24 (`ops/mac-mini-setup.md`, section 14). A task moved to the other model can change its
result for reasons that have nothing to do with swapping. The assignment must be fixed
before the run.

**Decision rule (written now).**
- One resident model stays the default. Swap-per-task is adopted only if, with no task
  lost and no added errors, it gives a median wall time or a p95 per-task latency at
  least 10 percent better.
- If the pair cannot stay resident under the 24.96 GiB ceiling, with the owner's margin,
  swapping is forced. The A/B then reports the swap cost, and no adoption rule applies,
  because the choice is not free.

**Needed before it can run.**
1. A swap driver: start, health check, stop, and load and unload accounting. It is not
   built (`README.md`, row 5).
2. A second model chosen, and a frozen assignment.
3. A load-time method. None is in the repo.
4. The heavy slot respected across processes. `AdmissionController` takes a `slot_lock`
   file for this (`lab/model.py`, `AdmissionController.__init__`, lines 121 to 131).

**What could invalidate it.**
- Load time depends on the disk cache, and a second load may be faster (**UNVERIFIED**).
- If a swapped-out model's memory is not returned, the footprint result is wrong. The
  sampler must show the drop.
- Pass count differences between the two models (above).

## 7. Measuring peak footprint, for all four A/Bs

- The lab has no footprint measurement. The ADR's figures came from `footprint` on the
  server process, run by hand, in whole GiB ([decisions/0001-heavy-model.md](decisions/0001-heavy-model.md), "Memory").
  The command's options and its sampling method are **UNVERIFIED** here.
- The deciding number is the peak footprint the system itself keeps for the server
  process, read once after each arm's five runs. A sampler that reads the footprint at a
  fixed interval can miss a short peak between two readings, so a sampled maximum is only
  a lower bound and decides nothing on its own. macOS has two candidates for the kept
  peak: the `phys_footprint_peak` line in `footprint`'s output, and the "peak memory
  footprint" line that `/usr/bin/time -l` prints for a process it started. Which kernel
  counter each reads, and whether they agree, is **UNVERIFIED**. The first run checks it
  by reading both on the same server start.
- Each arm starts a fresh server, so the kept peak covers that arm's runs only.
- The sampler still runs, at an interval the owner sets (the repo has no value). It shows
  when in a run the footprint rose, and a swapped-out model's drop in A/B 4. If its
  maximum is above the kept peak, the kept peak is wrong, and the run is not used.
- RSS (`lab/bench.py`) is reported beside footprint for continuity. It decides nothing.
  The ADR says "Measure footprint, not RSS" (ADR 0001, "Memory").
- The ceiling is 24.96 GiB (ADR 0001, "Memory"). The admission budget of 20,500 MB
  (`lab/model.py`, line 58) is a different number, and it is not the ceiling.
- Readings are whole GiB, plus or minus 0.5 GiB (ADR 0001, "Memory"). A difference
  under 0.5 GiB is no difference.

## 8. Analysis plan

- Per arm: run 1 gives the pass count and errors. Five runs after one warm-up give the
  medians of p95 and tokens per second.
- Each arm's run 1 gets a `lab eval rerun`, reported beside it, never in its place.
- Rates get 95 percent paired bootstrap intervals over task ids, 10,000 resamples, with
  a fixed seed, as section 1, item 8 says.
- The p95 of each run comes from the eval summary (`lab/evals.py`, lines 211 to 213),
  not from bench (`lab/bench.py`, lines 71 to 73).
- No task is removed, and no threshold is changed, after a result is seen.
- Results go in a Results section added later, with start times and commits, as
  [PREREGISTRATION.md](PREREGISTRATION.md) does.

## 9. Threats to validity

- One machine, one model family, one pair of builds per A/B.
- 24 tasks, all short answers. This gives low power for quality and none for decode speed (section 5).
- A build change moved pass counts by three tasks on this file (section 3).
- Nondeterminism at temperature 0 (section 1, item 7).
- The tasks are public in the repository and may be in training data, as
  [PREREGISTRATION.md](PREREGISTRATION.md) says of the H tasks.
- The tasks were written by the lab's owner.
- Thermal state is not recorded. Power settings are (`lab/evals.py`, lines 127 and 134 and 135).

## 10. Decisions the owner needs to make

1. Approve or change each rule. The 10 percent threshold and the 1/24 resolution come
   from the repo. The margins and the sampler interval are **owner to set**.
2. The admission budget. Keep 20,500 MB and accept that the longer arms are refused, or
   change it as a dated policy change, or pass a separate controller (section 4).
3. Whether the 64K arm runs at all, given the ADR's prediction (section 4).
4. The draft model, and the tokenizer check (section 5).
5. The second model and a frozen task assignment (section 6).
6. Whether these four A/Bs are registered before the first run, like H1 to H4. The
   process for that is in [PREREGISTRATION.md](PREREGISTRATION.md), Amendment 1, item 10.
   The repo does not say whether the A/Bs will be registered.
7. Who writes the long-prompt and generation-heavy sets, and when they are frozen.

## 11. Facts not confirmed in the repo (UNVERIFIED)

- The `llama-server` build 11146 launch flags: port, context and speculative options.
- The `mlx_lm.server` 0.31.3 context flag and draft option. Not in the repo, and not checked in its source.
- Whether either server supports speculative decoding. Not checked.
- The `footprint` command's options and sampling method, and which counter its
  `phys_footprint_peak` and `/usr/bin/time -l`'s "peak memory footprint" read.
- The ADR's MLX claims of about 10 percent less memory and 15 to 30 percent more speed
  (ADR 0001, "Decision", point 1). No measurement in the repo.
- Whether `memory_budget.py` exists. [PLAN.md](PLAN.md) section 3.2 and ADR 0001 name it, and
  no file of that name is in this repo.
- The load time of each model. Not measured.
- Whether lab workers can be paused for a measurement.
- Whether the 4B baseline shares the heavy model's tokenizer.
- Whether a 64K run would fail. The ADR's "~28 GiB" is an extrapolation, not a measurement.
- Thermal state during the arms.

## 12. Discrepancies noticed, not changed here

- ADR 0001's candidates table gives the MLX 4-bit model as "~16.7 GB"
  (ADR 0001, "Candidates"). The measured build is 17.2 GB on disk, and the ModelSpec uses 17,180 MB
  (ADR 0001, "Measured on the M6").
- The saved bench record `evals/bench/bench-20260929T180609+0000-ca6c2573.json` holds
  `weights_mb` 17,200. The ADR and `lab/model.py` (line 62) use 17,180.
- The same bench record holds `context_tokens` 8,192, because `lab/bench.py` (line 205)
  hard-codes it. The ADR's ModelSpec uses 16,384 (ADR 0001, "Measured on the M6").
