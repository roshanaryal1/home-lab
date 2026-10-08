# H1c run plan: constrained decoding on the same weights (#179, option 2)

Written 2026-10-09. **Not run.** Nothing in this file has been sent to the model.
H1c is exploratory, under the same rules as H1b
([PREREGISTRATION-AMENDMENT-2-REGISTRATION.md](PREREGISTRATION-AMENDMENT-2-REGISTRATION.md),
"H1b, exploratory"): it cannot change H1's verdict, it is reported beside H1 and H1b, and it is
run once, with no retry and no second design.

## The question

H1b gave the candidate the five route definitions and its accuracy rose from 0.10 to 0.43, with
coverage 1.00 and 8 false promotions. Does constraining the reply, token by token, to the one
shape the candidate's parser accepts change which route the model picks?

## What is the same as H1b

| | H1b | H1c |
|---|---|---|
| Cases | `evals/h1_review/final-cases-v2.jsonl` | same file |
| System prompt | `evals/h1_review/h1b-system-prompt.txt` (SHA-256 `be093c94...6944`) | same file |
| Weights | `mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit-DWQ`, revision `cfcade7221ccd128681961446e5f7906c08cae55` | same snapshot |
| Decoding | temperature 0, seed 0, `--max-tokens` 256 (the `lab shadow` defaults) | same |
| Instrument | `lab/shadow.py`, `lab/rubric.py`, hash-checked by `tests/test_h1_amendment.py` | same, unchanged |
| Server | `mlx_lm.server` 0.31.3 | `lab.constrained_server` (below) |

## What is different: the server

`lab.constrained_server` loads the same snapshot through `mlx_lm` and applies
`lab.constrained.TokenConstraint` as a logits processor. The model may only emit:

```text
{"route": "<route>", "confidence": <number>}
```

with exactly that spacing, `<route>` one of the five routes, and `<number>` one of `0`, `1`,
`0.d`, `0.dd`, `0.ddd`, `1.0`, `1.00`, `1.000`. Every finished reply passes
`lab.shadow.model_candidate`'s checks. The grammar version is `h1c-route-v1`, and the server puts
it in every reply's `system_fingerprint`. The client side is unchanged: `lab shadow --endpoint`
talks to this server as it talked to `mlx_lm.server`.

Two things this cannot isolate, stated before the run:

- The constraint fixes the spacing and the number format as well as the route. H1b's replies
  already parsed on all 30 cases (coverage 1.00), so a format change alone cannot move coverage,
  but it can move which tokens come before the route. Any difference is the constraint as a
  whole, not the route enum alone.
- Greedy decoding under a mask picks the best allowed token at each step. It does not search
  for the best whole reply.

## Checks before any case is sent

Each must pass, in this order, or the run does not happen. Record every output on #179.

1. **Self-check (no model output).** The tokenizer can spell every route through the mask, has
   a one-character token for each character of the language (so the mask has no dead ends), and
   decodes each spelled sentence back to the same text.
2. **Equivalence check (no case text).** With the constraint off (`--unconstrained`), this
   server must give the same reply as `mlx_lm.server` to the same probe, at temperature 0 and
   seed 0. If they differ, the two servers build the prompt or decode differently, and H1c would
   measure the server, not the constraint. Then stop and report.
3. **Manipulation check (no case text).** With the constraint on, the probe
   `Reply with exactly one word: hello` must come back as a sentence of the language, where
   `mlx_lm.server` answered `Hello` (#179).

## Freeze

After this plan's pull request merges, a second pull request records the SHA-256 of
`lab/constrained.py` and `lab/constrained_server.py` in a test, as
`tests/test_h1_amendment.py` does for H1. The run uses that commit. Nothing is changed after the
freeze; a fault found later is reported, not fixed and rerun.

## The owner's steps on the Mac mini

Use one Terminal window, with `REPO` set to your clone at the frozen commit. Close large apps
first, as for section 13 of `ops/mac-mini-setup.md`.

Only one copy of the weights fits in memory (about 17 GB of 32 GB). So `mlx_lm.server` and this
server never run at the same time. Find the LaunchAgent that runs `mlx_lm.server` with
`launchctl list | grep -i mlx`, and stop it with `launchctl bootout gui/$(id -u)/<label>` when a
step says so. Start it again at the end.

Set the paths once:

```sh
MLXPY="$HOME/.local/share/uv/tools/mlx-lm/bin/python"
SNAP="$HOME/.cache/huggingface/hub/models--mlx-community--Qwen3-Coder-30B-A3B-Instruct-4bit-DWQ/snapshots/cfcade7221ccd128681961446e5f7906c08cae55"
cd "$REPO"
```

Step 1, the self-check, with `mlx_lm.server` stopped:

```sh
HF_HUB_OFFLINE=1 PYTHONPATH="$REPO" "$MLXPY" -m lab.constrained_server --model "$SNAP" --self-check
```

It must end with `PASS`.

Step 2, the equivalence check. First the probe through `mlx_lm.server`, while it runs:

```sh
curl -sS http://127.0.0.1:8080/v1/chat/completions -H 'Content-Type: application/json' -d "{\"model\": \"$SNAP\", \"messages\": [{\"role\": \"user\", \"content\": \"Reply with exactly one word: hello\"}], \"temperature\": 0, \"seed\": 0, \"max_tokens\": 16}"
```

Then stop `mlx_lm.server`, start this server without the constraint in a second window, and
send the same probe to port 8081:

```sh
HF_HUB_OFFLINE=1 PYTHONPATH="$REPO" "$MLXPY" -m lab.constrained_server --model "$SNAP" --port 8081 --unconstrained
```

```sh
curl -sS http://127.0.0.1:8081/v1/chat/completions -H 'Content-Type: application/json' -d "{\"model\": \"$SNAP\", \"messages\": [{\"role\": \"user\", \"content\": \"Reply with exactly one word: hello\"}], \"temperature\": 0, \"seed\": 0, \"max_tokens\": 16}"
```

The two `content` values must be identical. Stop the server (Control-C).

Step 3, the manipulation check: start it again without `--unconstrained`, and send the same
probe. The `content` must be a sentence such as `{"route": "post", "confidence": 1}`, and
`system_fingerprint` must be `h1c-route-v1`.

Step 4, the run, once, with the constrained server still up:

```sh
uv run python -m lab.cli shadow --cases evals/h1_review/final-cases-v2.jsonl \
  --endpoint http://127.0.0.1:8081/v1 --model "$SNAP" \
  --revision cfcade7221ccd128681961446e5f7906c08cae55 --weights-mb 17200 \
  --system-file evals/h1_review/h1b-system-prompt.txt \
  --record evals/h1_review/h1c-run.json
```

The `--model` value must be exactly the `--model` the server was started with: the client
refuses a reply that names other weights (`ModelMismatch`).

Then stop the server and start the `mlx_lm.server` LaunchAgent again with
`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<label>.plist`.

## What is reported

Beside H1 and H1b, in the same table: accuracy on all 30 cases, coverage, false promotions,
and the confusion matrix, from `evals/h1_review/h1c-run.json`, with its SHA-256. Plus the outputs
of the three checks. No conclusion beyond the one run on 30 known cases.

## Unverified until the Mac mini

- That `mlx_lm` 0.31.3 calls a logits processor with the generated tokens and the logits of
  one step, the way `lab.constrained_server.MLXBackend` assumes. The manipulation check shows
  it, or shows that it does not.
- That `tokenizer.apply_chat_template(messages, add_generation_prompt=True)` builds the same
  prompt as `mlx_lm.server`. The equivalence check shows it.
- That the tokenizer's `eos_token_ids` names the token the model ends a reply with.
