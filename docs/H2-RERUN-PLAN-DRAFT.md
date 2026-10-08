# H2 rerun plan: exploratory draft

**DRAFT. Exploratory. Not registered. Not a rerun of H2. Owner to approve before any run.**

Drafted 2026-10-09 (NZDT) on `origin/main` at commit `9847071`, for issue #321.
Nothing in this draft has been run.

## 1. Why this exists

H2 is "not supported" ([PREREGISTRATION.md](PREREGISTRATION.md), Results, H2). Its
grammar condition passed 69 of 70 on the first run (record `4344a2ae`). Its rerun,
on the same commit, seed and temperature, passed 58 of 70 with no refused calls
(record `8f918047`). The Results text says the model "often wrote `"arguments"` before
`"tool"`, and in 11 tasks then named the wrong tool". Why is not established. The
Results text says that the schema's allowance of either key order is "one untested explanation".

This plan tests that explanation as an exploratory question. It does not change H2.

## 2. What the schema allows (from `lab/grammar.py` only)

**The schema allows either key order. It does not fix one.**

- Each tool branch is an object with exactly two keys, `tool` and `arguments`
  (`lab/grammar.py`, `_branch`, lines 72 to 75). Both are listed as required (line 73),
  and `additionalProperties` is false (line 73).
- `tool` comes first in the `properties` mapping (line 74), and first in the `required`
  list (line 73). Neither list sets the order of the keys in the output. JSON Schema does
  not order an object's keys, so `{"tool": ..., "arguments": ...}` and
  `{"arguments": ..., "tool": ...}` are both valid.
- Nothing else in the file orders keys. `conforms` (lines 100 to 125) checks which keys
  are present and their types, not their position. The `arguments` object (lines 65 to 71)
  has the same property. Its `required` list is sorted (line 69). That sorts the list in
  the schema, and it does not order the output.
- The top level is `oneOf` over the tool branches (line 87). That does not order keys either.
- The module says that whether a server honours `response_format`, and how strictly,
  is server-specific (`lab/grammar.py`, module docstring, lines 15 to 17). How `llama-server` build 11146 turns
  this schema into a grammar, and whether it keeps the textual order, is **UNVERIFIED**.

**What the saved records show.** This is not from `lab/grammar.py`. It is a read-only
check of the four H2 records. Each saved answer was parsed as JSON, and its first key was read.
The check was run from a scratch file and is not committed. It can be repeated.

| Record | Condition | Passed | First key in answers |
|---|---|---|---|
| `evals/runs/run-20260929T165018+0000-0d3e2810.json` | plain | 69 of 70 | `tool` in 70 of 70 |
| `evals/runs/run-20260929T165052+0000-4344a2ae.json` | grammar | 69 of 70 | `tool` in 70 of 70 |
| `evals/runs/run-20260929T165132+0000-8f918047.json` | grammar, rerun | 58 of 70 | `arguments` in 70 of 70 |
| `evals/runs/run-20260929T165159+0000-11281970.json` | plain, rerun | 69 of 70 | `tool` in 70 of 70 |

Two points follow. The same schema gave two different orders in two grammar runs, so
the schema did not fix the order in practice. And the Results text says "often", but the
rerun has every answer with `arguments` first. The owner may want a dated note on that
wording. This draft does not edit the registered Results.

**The prompts ask for `tool` first.** Each of the 70 prompts in `evals/toolcalls-v1.jsonl`
contains `Format: {"tool": ..., "arguments": {...}}`. A fixed order of `tool` first matches
the prompt. The H2b schema text is written with sorted keys (`lab/toolcorpus.py`, lines 152
to 154), so `arguments` appears first in that prompt. That condition is not part of this plan.

## 3. The question

With the key order fixed to `tool` first, does the wrong-tool count or the pass count
change, compared with the registered grammar condition on the same server start?

This is exploratory. No direction is registered, and no threshold applies.

## 4. What changes from the registered grammar condition

1. **The constraint.** A grammar that allows only `tool` first, then `arguments`, with
   the same branches and the same argument rules. It must be generated from the branch
   table in `lab/grammar.py`, not typed by hand. The module says the schema is derived
   from the broker table so the two cannot drift apart (`lab/grammar.py`, lines 3 to 8).
   Checks before any run:
   - Every value from `sample` (`lab/grammar.py`, lines 143 to 149) passes `conforms`.
   - Every such value parses with `parse_tool_call`.
   - A test that the constraint accepts each sampled call written with `tool` first,
     and rejects the same call written with `arguments` first.

2. **Delivery.** `ops/mac-mini-setup.md`, section 20, says `llama-server` takes a
   `grammar` request field as well as `json_schema`, and that this was checked in the
   server's source. That check was not repeated here. The adapter sends only
   `response_format` (`lab/model.py`, lines 273 and 274). So the run needs an adapter option
   to send the grammar, and `RunConfig` (`lab/evals.py`, lines 141 to 153) must record it.
   Both are code changes. None is made here.

Nothing else changes. Section 5 lists what stays the same.

## 5. What stays the same

- **Runtime.** `llama-server` build 11146, commit `7fe450e19`
  ([PREREGISTRATION.md](PREREGISTRATION.md), Amendment 1, item 1).
- **Weights.** `unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF` at revision
  `b17cb02dd882d5b6ab62fc777ad2995f19668350`, file `Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf`,
  SHA-256 `fadc3e5f8d42bf7e894a785b05082e47daee4df26680389817e2093056f088ad`
  (Amendment 1, item 1).
- **Tasks.** `evals/toolcalls-v1.jsonl`, 70 tasks, SHA-256
  `35a25945360b8d694782a5b6bd2950e4538cbbe76d2420f3dece915c754b1c7d`
  (Amendment 1, item 2). The hash was checked against the file in the repo and matches.
- **Sampling.** Temperature 0 (`lab/model.py`, line 269), seed 0 (record `4344a2ae`,
  `config.seed`).
- **Limits.** Output limit 256 tokens and timeout 120 seconds (record `4344a2ae`, `config.max_tokens`
  and `config.timeout_seconds`).
- **ModelSpec.** Copied from record `4344a2ae`, `config.model`: name
  `qwen3-coder-30b-a3b-q4km`, weights 18,600 MB, context 8,192, output 1,024, 200,000 bytes per token.
- **Parser and grader.** `parse_tool_call` (strict, `lab/model.py`) and `grade`
  (`lab/evals.py`, lines 96 to 103), unchanged.
- **Server launch.** The same flags as the H2 run. The repo does not record the
  `llama-server` command line. The H2 record shows port 8090 (`config.endpoint`). The
  rest is **UNVERIFIED**, so it must be written down before the first run.

## 6. Conditions and runs

Four records, all on one commit, all 70 tasks:

1. **Start A, C1.** The registered grammar, as H2 ran it (the existing `--grammar` option,
   `lab/evals.py`, lines 342 to 364). This is the control.
2. **Start A, C2.** The fixed-order grammar (section 4). This is the exploratory condition.
3. **Rerun of C2** on start A, with `lab eval rerun`, before the server is restarted.
   `lab eval rerun` reuses the endpoint in the record, so it must run while start A is
   still the server behind it.
4. **Start B, C1.** The server restarted with the same flags, then C1 again. This tests
   whether the order holds across a second start.

Each record is reported. None replaces another (Amendment 1, item 8).

The C1 command, with values copied from record `4344a2ae`:

```sh
uv run python -m lab.cli eval run --endpoint http://127.0.0.1:8090/v1 --model qwen3-coder-30b-a3b-q4km --revision b17cb02dd882d5b6ab62fc777ad2995f19668350 --tokenizer-revision b17cb02dd882d5b6ab62fc777ad2995f19668350 --context-tokens 8192 --max-output-tokens 1024 --weights-mb 18600 --tasks evals/toolcalls-v1.jsonl --grammar
```

The rerun of a record uses the record file. `lab eval rerun` refuses if the checked-out
commit differs from the record's commit (`lab/evals.py`, `rerun`, lines 282 to 285).

```sh
uv run python -m lab.cli eval rerun evals/runs/NEW-RECORD.json
```

The C2 command does not exist yet. It needs the adapter option from section 4.

## 7. Manipulation check first

Before any C2 record, send one request that is in no task file, with the fixed-order
grammar, as Amendment 1, item 3 did for H2. The reply must parse, and its first key must
be `tool`. After the C2 run, every answer must start with `tool`. If any does not, the
C2 record is reported as a failed constraint, and no claim about key order is made from it.

## 8. What is reported

For each record:

- passed, refused calls and the refused-call rate.
- the count of answers by first key.
- the wrong-tool count, and the ids of the failed tasks.
- the start time and commit, from the record.
- the rerun comparison (`compare`, `lab/evals.py`, lines 290 to 301).

For C2 against C1 on start A, a paired bootstrap as H2 used it (10,000 resamples,
seed 20260930, [PREREGISTRATION.md](PREREGISTRATION.md), H2 Results), labelled exploratory.

## 9. How to read the records

These readings are for the owner's notes. None changes H2.

- If C2 has no more wrong-tool calls than C1 on start A, and C2 passes the constraint check,
  the fixed order is a candidate fix in this runtime. It is a candidate only. Any further
  claim needs a new hypothesis, registered before its run (section 10).
- If C1 on start B gives the same order as C1 on start A, the order was stable across these
  two starts. That does not show the start has no effect: both starts could set the same order.
- If C1 on start B gives `arguments` first in every answer, as record `8f918047` did, the order
  changed between starts, and the schema did not set it.
- If C2 fails the constraint check, the record says so, and no order claim is made.

## 10. What this plan cannot do

- **It cannot change H2's verdict.** H2 stays "not supported." Reasons:
  - H2's rule is applied to the registered records in the H2 Results table. A new record
    is not added to that table.
  - The fixed-order grammar is a different condition. H2 registered "with and without
    `response_format` from `lab.grammar`", with everything else equal (Hypotheses, H2, and Amendment 1, item 1).
  - The 70 held-out tasks were used for H2. They are no longer held out, and nothing can
    be confirmed on them (Amendment 1, item 2, and Analysis plan, "no optional stopping").
  - Exploratory results go under the Results heading "Exploratory, not part of any
    hypothesis". Changes are dated notes, and they never edit registered text.
- **It cannot confirm a key-order effect.** A claim about order needs a new hypothesis,
  H2c, registered before its run, with a new held-out file made with a new seed. The
  precedent is `lab/toolcorpus.py` with seed 20260930 (Amendment 1, item 2).
- **It cannot be compared with H2b.** H2b used MLX weights with a different quantisation
  (Amendment 1, item 9).

## 11. Before the run

1. The owner approves this draft.
2. This draft is committed before the first run. Each record seals its start time and
   its commit (`lab/evals.py`, `run_suite`, lines 225 and 240 and 241), so the order of
   events can be checked from git history.
3. The adapter option and the `RunConfig` field are merged, with their tests. This is a
   separate change, and it is not part of this draft.
4. The `llama-server` command line for start A and start B is written down, with its port and
   flags. These are **UNVERIFIED** today.
5. The manipulation check (section 7) passes.
6. The four records are made. Their results go under the "Exploratory, not part of any
   hypothesis" heading in the Results section of `docs/PREREGISTRATION.md`, as a dated note.

## 12. Facts not confirmed in the repo (UNVERIFIED)

- How `llama-server` build 11146 converts this schema, and whether it keeps the textual key order.
- The `llama-server` launch flags and context setting for the H2 run. The H2 record shows
  only the port (`config.endpoint`).
- That `llama-server` accepts a `grammar` request field. `ops/mac-mini-setup.md`, section 20, says so,
  and this draft did not re-check the server's source.
- Whether fixing the order to match the prompt lowers the wrong-tool count. Nothing in the repo has tested this. Section 6 proposes the test.
