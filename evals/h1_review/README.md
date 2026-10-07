# H1 independent review: how to get labels that are not the rubric's

Status: process and tooling, written 2026-09-30 for
[#84](https://github.com/roshanaryal1/home-lab/issues/84). Nothing here is
registered or frozen, and no H1 run has happened.

## Why this is needed

H1 (`docs/PREREGISTRATION.md`) is supported only if a candidate model beats the
deterministic rubric's accuracy by at least 0.05, with zero false promotions,
on at least 30 labeled cases. The 18 draft cases were labeled by someone who
had read the rubric. Checked on 2026-09-30, without any model:

- 2 of the 18 cannot be built in a ledger, so `lab shadow` crashes on the file:
  `d-paper-one-source` (a claim marked verified with one source) and
  `d-paper-control-contradicts` (a verified claim that is contradicted).
- On the other 16, the rubric alone agrees with the drafted label on 15. The
  one difference, `d-blog-two-sources`, looks like a slip in the draft.

If the labels mostly reproduce the rubric, the rubric scores near 0.94 on them
and H1 is very unlikely to be supported however good the candidate is, so a
"not supported" result would say little. Labels that come from a person's own
judgement are what can make the test informative.

## Process

1. **Give the reviewer only the sheet.** `review-sheet.md` and
   `answers-template.json` in this folder were generated from
   `evals/shadow_cases_DRAFT.jsonl` without the two unbuildable cases:

   ```sh
   uv run python -m lab.reviewsheet sheet --cases evals/shadow_cases_DRAFT.jsonl \
     --out evals/h1_review --exclude d-paper-one-source d-paper-control-contradicts
   ```

   Names of the models and builds (Qwen, DWQ, MLX, GGUF and so on) are masked with
   neutral labels by default (`--mask` changes the list), so the sheet does not
   reveal the candidate.
   Cases have neutral ids in a seeded shuffle (seed 20260930), the original ids
   and drafted labels are left out, and the routes are described in plain
   words without the rubric's counts. The drafted labels are still in the
   repository, so ask the reviewer to finish the sheet before looking.
2. **The reviewer** copies `answers-template.json` to `answers.json` and fills
   in one route per case. They should not read `lab/rubric.py`.
3. **Compare:**

   ```sh
   uv run python -m lab.reviewsheet compare --cases evals/shadow_cases_DRAFT.jsonl \
     --answers answers.json --exclude d-paper-one-source d-paper-control-contradicts
   ```

   It prints where the reviewer differs from the draft and a drafted-to-reviewer
   count.
4. **Decide, then amend, then run.** Only step 4 is not automated, and it is the
   owner's decision (below).

**Spare cases (2026-10-07).** `spare-cases-UNLABELED.jsonl` holds 8 more unlabeled cases, replaced
into the 30 only by the rule in the amendment when all three AI reviewers differ on a main case. Their
masked sheet is `spares/review-sheet.md` (ids `spare-01` to `spare-08`), made with
`uv run python -m lab.reviewsheet sheet --cases evals/h1_review/spare-cases-UNLABELED.jsonl
--prefix spare --out evals/h1_review/spares`. The owner labels them blind too.

**2026-10-07: the sheet now has 30 cases** (16 drafts that build, plus 14 new unlabeled cases
from this repository's real history in `extra-cases-UNLABELED.jsonl`). Generate it with both
files: `uv run python -m lab.reviewsheet sheet --cases evals/shadow_cases_DRAFT.jsonl
evals/h1_review/extra-cases-UNLABELED.jsonl --out evals/h1_review --exclude d-paper-one-source
d-paper-control-contradicts`. The owner labels all 30 on the masked sheet before seeing any
AI answer.

**2026-10-07: the owner chose an AI reviewer.** The draft amendment is
`docs/PREREGISTRATION-AMENDMENT-2-DRAFT.md` and the blind prompt is
`ai-reviewer-prompt.md`. **Status (updated after registration): both were registered on
2026-10-07 at https://osf.io/q75bx/ and are frozen; see
`docs/PREREGISTRATION-AMENDMENT-2-REGISTRATION.md`. Words such as "draft" or "not registered" in
them are historical.** Reviewers chosen: GPT, Gemini
and DeepSeek; not Claude (it wrote the drafts) and not Qwen (the candidate). It weakens the claim
from independent human labels to AI-assigned labels, and the amendment says so.

**2026-10-07: the final labels are made by `lab.h1_labels`.** It applies the registered rule
mechanically: majority of three, a main case on which all three differ is replaced by the
lowest-numbered unused spare in case order, a spare on which all three differ is skipped, H1 is not
testable if the spares run out, and no label is made from fewer than three replies. It rebuilds both
sheets from the case files and refuses to run unless they equal the frozen sheets byte for byte, so
each neutral id names the case the reviewers saw. It does not read the owner's labels. It writes the
final case file and a report together, once: if either exists, or either write fails, neither is
left. If fewer than 30 cases keep a label, the remaining cases go to a file marked `EXPLORATORY`
instead, never to the final file. Run it once, from the repository
root:

```sh
uv run python -m lab.h1_labels \
  --cases evals/shadow_cases_DRAFT.jsonl evals/h1_review/extra-cases-UNLABELED.jsonl \
  --exclude d-paper-one-source d-paper-control-contradicts \
  --sheet evals/h1_review/review-sheet.md \
  --spares evals/h1_review/spare-cases-UNLABELED.jsonl \
  --spare-sheet evals/h1_review/spares/review-sheet.md \
  --answers gpt=evals/h1_review/ai-replies/answers-gpt-5.6-sol-1.json \
    gemini=evals/h1_review/ai-replies/answers-gemini-3.6-flash-1.json \
    deepseek=evals/h1_review/ai-replies/answers-deepseek-flash-3.json \
  --spare-answers gpt=evals/h1_review/ai-replies/answers-gpt-5.6-sol-spares-1.json \
    gemini=evals/h1_review/ai-replies/answers-gemini-3.6-flash-spares-1.json \
    deepseek=evals/h1_review/ai-replies/answers-deepseek-flash-spares-2.json \
  --out evals/h1_review/final-cases.jsonl --report evals/h1_review/final-report.json
```

The replies used are the ones named in the registration note,
`docs/PREREGISTRATION-AMENDMENT-2-REGISTRATION.md`.

## What the owner decides before the amendment (superseded 2026-10-07)

**This section is the earlier, human-reviewer plan and is superseded** by
`docs/PREREGISTRATION-AMENDMENT-2-DRAFT.md`, which is the protocol to follow: three AI reviewers
(GPT, Gemini, DeepSeek) with a majority rule, the owner labelling all 30 cases blind, the two
unbuildable cases left out, and 14 new unlabeled cases from this repository's history (not
twelve). The old list is kept below only for the reasoning behind it.

- **Who reviews (old).** Someone other than the drafter and the owner, or the owner
  after a stated gap. The reviewer's independence is the point.
- **Disagreements (old).** A case where the reviewer differs from the draft
  goes to a second independent reviewer; the drafter never breaks the tie.
- **The two unbuildable cases (old).** Rewrite them as states the ledger can reach, or
  drop them. Now: left out.
- **Twelve more cases (old)** to reach 30. Now: fourteen, since two drafts are left out.
- **The instrument.** `lab shadow` changing (for example to handle a verified
  claim that cannot be verified) changes what was frozen at registration, so it
  needs the same dated amendment. Still true.

## Draft amendment fields (not registered)

| Field | Value |
|---|---|
| Date and time (UTC) | to fill in before any run |
| Case file | new dated file, SHA-256 to fill in |
| Number of cases | at least 30 |
| Label source | independent reviewer, name or role to fill in |
| Disagreement rule | as decided above |
| Lab commit | to fill in (full 40 characters) |
| Reviewer agreement with the drafts | from `compare`, to fill in |
| Rubric accuracy on the final file | to fill in, reported next to the candidate's |
