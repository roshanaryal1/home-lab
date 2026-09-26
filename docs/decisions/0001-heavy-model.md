# ADR 0001: the heavy model

**Status:** recommended, pending benchmark. **Date:** 2026-09-26.

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
