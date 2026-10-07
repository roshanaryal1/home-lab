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
