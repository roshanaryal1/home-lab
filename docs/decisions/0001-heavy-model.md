# ADR 0001: the heavy model

**Status:** recommended; memory, speed and tool calls measured on the M6
(2026-09-30), comparison against a second candidate still open.
**Date:** 2026-09-26.

The reference architecture deliberately left this open: the eleven systems
in the study reached no consensus, and section 16 records it as a
non-consensus decision rather than resolving it quietly. The instruction
attached to it was "pick one, write down why, and benchmark before
committing". This is the writing down.

## The constraint everyone gets wrong

Most "best local model for 32 GB" recommendations assume the whole 32 GB
is available for weights. It is not.

The study measured what the surveyed systems actually budgeted and found
they reserved for the **operating system alone**, between 4 and 8 GB, and
omitted the supporting services their own designs require: a headless
browser, a Python worker pool, a SQLite and vector index. Adding those
takes the real reserve to **11.5 GB**.

```
32.0 GB   unified memory
-11.5 GB   OS, browser, workers, database, index
= 20.5 GB   actually available for model weights
```

Every candidate below is judged against 20.5 GB, not 32 GB. That single
correction eliminates most of the models that top the public lists.

## Candidates

| Model | Arch | Memory | Speed | Verdict |
|---|---|---|---|---|
| **Qwen3-Coder-30B-A3B** | MoE, 3B active | ~18.6 GB GGUF Q4_K_M, **~16.7 GB MLX 4-bit** | ~100+ tok/s | **fits with headroom** |
| Qwen3.6-35B-A3B | MoE, 3B active | **24 GB floor** | fast | **does not fit** |
| Gemma 4 26B-A4B | MoE, 4B active | **24 GB minimum** | ~80 tok/s | **does not fit** |
| Gemma 4 31B | dense | ~25.2 GB at Q6_K | slow | **does not fit** |
| Qwen 3.6 27B | dense | fits at Q4 | **15.3 to 15.5 tok/s** | too slow |
| Qwen 3.8 27B | dense | fits at Q4 | ~8 tok/s on M6 | too slow |

Two things decide this, and neither is benchmark score.

**Memory.** Qwen3.6-35B-A3B and Gemma 4 26B-A4B both carry a 24 GB floor.
On paper they are the better models, and Qwen3.6 reportedly leads on
agentic SWE-bench. With 20.5 GB available they are simply not options.
They would fit a 48 GB machine; this is a 32 GB machine.

**Architecture, not parameter count.** Dense models hit a
memory-bandwidth wall on Apple Silicon: a 27B dense model runs at roughly
8 to 15 tokens per second. Mixture-of-experts models activate a fraction
of their weights per token, so a 30B MoE with 3B active runs at roughly
100 tokens per second on the same hardware. That is not a marginal
difference, it is **six to twelve times**.

For an agent this compounds badly. A dense model at 10 tok/s takes about
a minute to produce a 600-token response. An agent loop with twenty turns
becomes twenty minutes of waiting. At 100 tok/s the same loop is two
minutes. A slightly better model that is ten times slower is the worse
model for this workload.

## Decision

**Qwen3-Coder-30B-A3B, MLX 4-bit.**

1. **It fits.** About 16.7 GB in MLX 4-bit against 20.5 GB available,
   leaving genuine headroom rather than running at the edge. MLX uses
   roughly 10% less memory than GGUF at the same quantisation and runs
   15 to 30% faster.
2. **MoE, so it is fast enough to be used in a loop.** 3B active
   parameters per token.
3. **Coder-tuned**, and coding is the primary workload for the first two
   workers.
4. **Already a study candidate**, so choosing it is not a departure from
   the adjudicated design.
5. **Apache 2.0.**

MLX was unanimous across all ten non-anchor systems as the runtime for
this hardware, so that part was never in question.

## What would change this

- **Measured memory above 20 GB.** The figures above are reported, not
  measured on an M6. Section 17 of the reference architecture requires
  checking `memory_budget.py` predictions against real RSS, and this is
  exactly the prediction to check.
- **Tool-calling reliability below what the agent needs.** Speed is
  worthless if structured calls fail. Qwen 3.8 27B is reported to produce
  flawless JSON and strict zero-prose tool calls; if Qwen3-Coder-30B-A3B
  cannot match that, the slower model may win on total task time despite
  being slower per token.
- **More memory.** On a 48 GB or 64 GB machine this decision reverses and
  Qwen3.6-35B-A3B becomes the obvious pick.

## Measured on the M6 (2026-09-30)

Machine: Mac mini, Apple M6, 32 GB, macOS 27.0 (26A428). Model:
`mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit` at commit
`6e302ea604ad9ab206367e2c501d1571023e7b6d` (weights and tokenizer from the
same commit; 17.2 GB on disk; base `Qwen/Qwen3-Coder-30B-A3B-Instruct`).
Server: `mlx-lm` 0.31.3 (`mlx_lm.server`, loopback only), driven through
`OpenAICompatibleAdapter`. Every weight shard's SHA-256 matched its blob
name after download.

These are the values a `ModelSpec` for this model uses:

```python
REV = "6e302ea604ad9ab206367e2c501d1571023e7b6d"   # the plain build measured here; see point 5
# Served since 2026-09-30: the DWQ build, REV = "cfcade7221ccd128681961446e5f7906c08cae55"
ModelSpec(name="<snapshot path the server reports>",
          revision=REV, tokenizer_revision=REV,
          context_tokens=16_384, max_output_tokens=1024,
          weights_mb=17_180, kv_bytes_per_token=200_000)
```

**Memory.** Physical footprint of the server process (`footprint`, whole
GiB, so each figure is plus or minus 0.5 GiB), a fresh server per length
with `--prompt-cache-size 1`:

| prompt tokens | footprint | over idle | wall time | prefill |
|---|---|---|---|---|
| none (weights loaded) | 16 GiB (17,180 MB) | | | |
| 8,900 | 18 GiB | +2 GiB | 6.7 s | ~1,330 tok/s |
| 18,015 | 20 GiB | +4 GiB | 17.6 s | ~1,020 tok/s |
| 37,440 | 23 GiB | +7 GiB | 56.8 s | ~660 tok/s |

About **200 KB of cache per token**, twice the 100,000 bytes assumed before
(`lab/model.py` now defaults to the measured figure). Process RSS does not
show this: the cache lives in Metal allocations, so RSS stayed near 16 GB
throughout. Measure footprint, not RSS.

Metal's `max_recommended_working_set_size` on this machine is **24.96
GiB**. That is a hard ceiling: a ~75K-token request failed with `[METAL]
Command buffer execution failed: Insufficient Memory` and pushed about 3 GB
into swap. 64K was not run because the measured slope predicts ~28 GiB.
Under the 20.5 GB policy budget the model gets about **16K tokens** of
context.

`mlx_lm.server` keeps each request's cache after the request ends unless
told otherwise. With the default, 8.9K then 18K then 37K tokens on one
server ran out of Metal memory at 37K, which fits alone. **Run the server
with `--prompt-cache-size 1`**, or the admission controller's accounting,
which assumes the cache is freed after each request, is wrong.

**Speed.** Generation, 600 output tokens from a 26-token prompt: 57.1,
66.5, 67.6 tok/s (the first run cold). **About 67 tok/s, not the "~100+"
reported above.**

**Admission.** A request sized past the budget was refused before the
server was called ("would need 25233 MB resident against a 20500 MB
budget"); the server logged no request for it and swap did not change. A
small request was admitted at 17,184 MB resident.

**Tool calls.** 51 prompts asking for one JSON tool call (the 3 in
`evals/tasks.jsonl` plus 48 generated: `fs.read`, `fs.write`, `fs.list`
over 16 paths), temperature 0, checked with `parse_tool_call`: 40 parsed
as the right tool, **11 refused (21.6%)**. Every refused call inspected
was `fs.write`: invented parameters (`format`, `public`, `json`) or broken
JSON. The parser refused them all and repaired none, as designed. Section
20 of `ops/mac-mini-setup.md` (constrained decoding) is the planned fix.

**Beside a container.** With the model generating 2,000 tokens, an Apple
container started and ran; swap went from 1,247.8 MB to 1,239.8 MB (no
growth). ADR 0007's coexistence check passes.

### Where the measurements disagree with this ADR

Recorded as measured; no figure above was adjusted to fit.

1. **Speed**: about 67 tok/s, not ~100+. Still several times the dense
   candidates' reported speed, so the architecture argument holds, but the
   twenty-turn loop is closer to three minutes than two.
2. **Tool-call reliability**: 21.6% of calls refused. This is the second
   "what would change this" condition below. It is not yet a reason to
   switch: in an exploratory run (not the pre-registered H2, which uses
   only the three frozen tool-call tasks), putting the tool schema in the
   system prompt cut it to 2.0% (1 of 51) on the same prompts (setup
   section 20, 2026-09-30), and the server ignores `response_format`, so
   true constrained decoding is still unmeasured.
3. **Context**: about 16K tokens under the budget, and 64K is impossible
   on this machine (Metal ceiling), whatever the model supports.
4. **Memory itself fits**: 17.2 GB against the 20.5 GB budget, as claimed.
5. **The MLX 4-bit build corrupts text it only has to copy** (pre-registered
   H2b run, 2026-09-29, `docs/PREREGISTRATION.md` Results): on 70 held-out
   tool calls it passed 28, mostly because paths came back with a token
   spliced in (`build/out/tpublic/timeline.md`, `roroster.yaml`); the same
   odd `public` showed up as an invented parameter in section 13. The GGUF
   Q4_K_M build of the same base model under `llama-server` passed 69 of
   70 on the same prompts.

   **Cause found, 2026-09-29 (exploratory).** Twenty copy prompts (the
   `fs.read` and `fs.write` tasks of `evals/toolcalls-v1.jsonl`), temperature
   0, exact path in the reply:

   | build, same runtime unless noted | exact paths |
   |---|---|
   | plain 4-bit (`...-4bit` @ `6e302ea6`), through the server | 28/70 on the full set |
   | plain 4-bit, `mlx_lm.generate` directly, no server or cache | 4/20 |
   | plain 4-bit, `mlx` 0.31.2 instead of 0.32.3 | 4/20, identical text |
   | DWQ 4-bit (`...-4bit-DWQ` @ `cfcade72`) | **20/20** |

   The server, the prompt cache and the `mlx` version are ruled out, and the
   two builds' tokenizers produce identical token ids (checked against the
   base model's tokenizer too). What differs is the quantisation: the plain
   4-bit build is defective for copying; the DWQ build of the same model is
   not. On the full sets the DWQ build passed 66 of 70 tool calls and 22 of
   24 utility tasks, decoding at 77.3 tok/s with the same 16 GiB footprint.

   **The served build is now the DWQ one** (2026-09-30):
   `mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit-DWQ` at commit
   `cfcade7221ccd128681961446e5f7906c08cae55`, weight shards checked against
   their SHA-256. The pre-registered results above were measured on the plain
   build and stay as they are; they describe that build.

## Before committing

The study's instruction was to benchmark, and that has not happened. On
the mini, measure for at least two candidates on identical prompts:

- peak resident memory, not the advertised figure
- tokens per second at 8K, 32K and 64K context
- structured tool-call success rate over at least 50 calls
- model swap latency

Until those numbers exist this remains a recommendation with reasoning,
not a settled choice. Recording it now means the reasoning is auditable
later, including if it turns out to be wrong.
