# Amendment 2 is registered (H1 labels), with its errata

Written 2026-10-07 for [#84](https://github.com/roshanaryal1/home-lab/issues/84). This note records
what was registered and what was checked. It does not change the registration.

## What was registered

| Item | Value |
|---|---|
| Registration | https://osf.io/q75bx/ |
| DOI | `10.17605/OSF.IO/Q75BX` (resolves to the registration) |
| Amends | the registration at https://osf.io/jfp74 , hypothesis H1 only |
| Registered (OSF `date_registered`) | 2026-10-07 08:12:02 UTC |
| State when checked | public, no approval pending, no embargo, not withdrawn; visible without logging in |
| Text | `docs/PREREGISTRATION-AMENDMENT-2-DRAFT.md` as it stood at `main` commit `7856e5af2adf85f028f16658490b6e31e54096b9`, SHA-256 `d50eb365d9045540095e40eb22d1fe27f9e45f57a9079d24a9fb83300bfaaec3` |
| Files | 15 files in the registration's archive folder: the 14 frozen files of the amendment's table and `MANIFEST.txt`, uploaded as flat names (slashes become underscores) |

## What was checked, and how

On 2026-10-07 the registration was read back through OSF's API, as its owner and then without
logging in:

- The 15 files in its archive folder have the same SHA-256 as the local upload folder and as
  `MANIFEST.txt`.
- 28 of the 29 form answers are identical to the intended text. The one that differs is **Study
  design**, by a single missing space (erratum 1). The **Description**, which is a separate
  metadata field and not one of the 29, differs only in whitespace (erratum 2). The selected options match:
  foreknowledge "Authors have observed the data, but have not performed the proposed analyses",
  study type Simulation study, no causal inference, blinding by those who code or interpret.
- The draft the registration came from was started at 07:54 UTC, after the files were uploaded
  (07:52 UTC); OSF copies a project's files when a draft is started.

## Errata (permanent; a registration cannot be edited)

1. In **Study design**, the registered text reads `as registered inosf.io/jfp74`. It should read
   `as registered in osf.io/jfp74`.
2. In the **Description**, the second and third paragraphs begin with two spaces.
3. The registered **files** are the amendment's frozen files under flat names, so the paths in the
   amendment's table (for example `evals/h1_review/review-sheet.md`) appear in OSF as
   `evals_h1_review_review-sheet.md`.
4. The registered "Context and additional information" says "no AI reviewer has seen any case and no
   model has produced any label or proposal". Read it as: none of the three registered reviewers and
   not the candidate model. The same registration discloses, in "Explanation of foreknowledge", that
   an earlier Claude session wrote the 18 draft labels (16 of those cases are in the frozen set), and
   that Claude wrote the 14 new cases and the 8 spares without labels.
5. The status words in files that were registered as they stood ("DRAFT, not registered, not run" at
   the top of `ai-reviewer-prompt.md`, "not yet registered" in the amendment text) are **historical**.
   Registration is the state in this note. The registered copies are binding and are not edited.

None of these changes a number, a rule or a hash.

## Which version is binding

The binding versions are the files in the registration (https://osf.io/q75bx/), with the SHA-256 in
`MANIFEST.txt` and in the amendment's table. Within `ai-reviewer-prompt.md`, the binding text is the
prompt between the two horizontal rules; the surrounding instructions describe how it is used. The
repository copies of these files are kept identical, and a test (`tests/test_h1_amendment.py`) fails
if any file the amendment freezes changes. Any change to them is a departure from the registered
protocol and has to be stated as one.

## A gap the registration states

The amendment defines no majority rule for a reviewer reply that is missing. The registered
"Missing data" answer says that if any reviewer's reply is missing the study is reported as not run
as registered, and no label is produced from fewer than three reviewers.

## State of the study at registration

None of the three registered AI reviewers had seen any case, the candidate model had produced no
proposal, and no accuracy figure existed. Claude, which is not a registered reviewer, had written
the 18 draft labels (16 of those cases are in the frozen set) and the 14 new cases and 8 spares, as
the registration itself discloses. The owner's own labels (30 main cases, 8 spares) existed and were hashed. The 30-case
masked sheet had been generated and published in this public repository before registration; the
registration says so.

## Next

Each of three reviewers (`gpt-5.6-sol`, `gemini-3.6-flash`, `deepseek-flash`) labels the main sheet in
one new conversation and the spare sheet in another, with the prompt in
`evals/h1_review/ai-reviewer-prompt.md`; replies are saved as
`answers-<reviewer>-<attempt>.json` and `answers-<reviewer>-spares-<attempt>.json`. This note will be
extended with what happened, including any retry or departure.

## What happened (2026-10-07)

All six conversations are in, saved byte for byte in `evals/h1_review/ai-replies/`, each recorded
on [#84](https://github.com/roshanaryal1/home-lab/issues/84) with its SHA-256. Only their structure
was checked. None has been compared with any label.

| Reviewer | Main sheet, used | Spare sheet, used | Set aside, kept |
|---|---|---|---|
| `gpt-5.6-sol` | `answers-gpt-5.6-sol-1.json` | `answers-gpt-5.6-sol-spares-1.json` | none |
| `gemini-3.6-flash` | `answers-gemini-3.6-flash-1.json` | `answers-gemini-3.6-flash-spares-1.json` | none |
| `deepseek-flash` | `answers-deepseek-flash-3.json` | `answers-deepseek-flash-spares-2.json` | `answers-deepseek-flash-1.json`, `answers-deepseek-flash-2.json`, `answers-deepseek-flash-spares-1.MALFORMED.txt` |

Settings, as reported by the owner. ChatGPT: model gpt-5.6-sol, thinking high, search, memory,
tools and custom instructions off, new chats. Gemini: model shown as 3.6 Flash, Fast, Google
Search, personal context, saved info and memory off, new chats, the same for both sheets. DeepSeek:
Search and DeepThink off, new chats. The DeepSeek page shows no model name or version. The three
vendors' thinking modes are not equal, because the protocol does not set one. This is a stated
limit.

Retries and departures:

1. DeepSeek main, attempt 1, was made with Search on. It is set aside under a rule fixed on #84
   before any comparison.
2. DeepSeek spares, attempt 1, was malformed (an essay, not one JSON object). The one permitted
   retry, attempt 2, is used. No route was taken from attempt 1.
3. **Departure.** DeepSeek main, attempt 2, was well formed, but the page had turned the pasted
   message (15,019 bytes) into an attachment shown as 13.64 KB. Compared with the frozen sheet, the
   attachment was missing text in nine cases (case-07, 11, 13, 15, 17, 19, 20, 25 and 26), so the
   reviewer did not see the frozen sheet. The protocol allows a retry only for a malformed reply, so
   a third attempt is a departure. It was decided before any label was compared. Attempt 3 was sent
   in a new chat with the registered prompt and sheet (15,018 bytes, SHA-256
   `d240e344455ee9d759bb68221b755741bd404687a47e5991caf697650af648a1`). The attachment showed
   14.67 KB and the owner checked that every case was complete. Attempt 3 is used. The owner also
   checked that the other five conversations received their full sheets.

The final labels are made once by `lab.h1_labels` (see `evals/h1_review/README.md`) and frozen by
the SHA-256 it prints.

## The final labels (2026-10-07)

`lab.h1_labels` was run once, on the owner's Mac mini, at `main` commit
`cac9c83fcb766858c7ca01a1f22317a925857405`, with the command in `evals/h1_review/README.md` and
the replies in the table above. It wrote 30 cases, so H1 is testable at the registered size.

| File | SHA-256 |
|---|---|
| `evals/h1_review/final-cases.jsonl` | `6fe6e3fb3d39d27365bfa5da46e25bfa072f95b494b3f43be17326593236b485` |
| `evals/h1_review/final-report.json` | `330348cd340fbfc9298e4312b522e004a929a741f15722beb39b90efdf98ecf2` |

The same command on a second machine gave the same case-file hash. These labels are frozen. They
were not compared with the owner's labels or the drafts before they were fixed.

## Running H1

`lab shadow` ran only the rubric from the command line; the candidate could be wired only from
Python. The command now takes the candidate's endpoint, name and revisions, sends temperature 0 and
one fixed seed (default 0) with every request, prints the adoption verdict, and can write the whole
run (provenance with the lab commit, settings, every row, the verdict) to a record it never
overwrites. A request that fails (server down, timeout, refused, a different model answering)
would otherwise count as the candidate abstaining, so any such failure marks the run invalid: no
verdict is printed or recorded, and the command exits with an error. The candidate takes the
same heavy-slot lock as the supervisor, so no other heavy request overlaps the run. A
`--revision` that is not the snapshot `--model` names is refused. This changes the command-line
wiring only. The instrument the amendment freezes,
`lab/shadow.py` and `lab/rubric.py`, is unchanged, and `tests/test_h1_amendment.py` fails if
either file's SHA-256 differs from the registered one. The run is at a later lab commit than the
one registered, and the record states which.

The H1 run, on the Mac mini with the model server from `ops/mac-mini-setup.md` section 13 up:

```sh
uv run python -m lab.cli shadow --cases evals/h1_review/final-cases.jsonl \
  --endpoint http://127.0.0.1:8080/v1 \
  --model ~/.cache/huggingface/hub/models--mlx-community--Qwen3-Coder-30B-A3B-Instruct-4bit-DWQ/snapshots/cfcade7221ccd128681961446e5f7906c08cae55 \
  --revision cfcade7221ccd128681961446e5f7906c08cae55 --weights-mb 17200 \
  --record evals/h1_review/h1-run.json
```

After it: the comparisons the amendment lists under "Reported with the result".
