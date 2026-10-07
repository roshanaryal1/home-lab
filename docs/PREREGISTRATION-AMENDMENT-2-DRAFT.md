# Amendment 2 to the pre-registration: AI-assigned labels for H1

**Status: complete and ready to register; not yet registered.** Drafted 2026-10-07 for
[#84](https://github.com/roshanaryal1/home-lab/issues/84), at the owner's direction that AI
reviewers will label the H1 cases. The owner's decisions of 2026-10-07 are in. The registration
time, which the owner supplies after registering on OSF, is the one field left empty. No AI
reviewer has seen any case and no H1 run has happened, and none may until this is registered.

It amends `docs/PREREGISTRATION.md`, H1 only. H2, H2b, H3 and H4 are untouched.

## Why an amendment is needed

H1 says a candidate model beats the deterministic rubric on at least 30 labeled cases. The 18
draft cases were labeled by a drafter who had read the rubric, so most labels reproduce it
(checked 2026-09-30: the rubric alone agreed with 15 of 16 buildable drafts). A model cannot
beat a rubric on labels that are the rubric, so a "not supported" result would say little.
`evals/h1_review/README.md` has the full reasoning.

The pre-registration says the case file is frozen in a dated amendment before any H1 run. This
is that amendment, and it changes the label source from a person to an AI reviewer.

## What stays as registered

The H1 decision rule is unchanged: coverage at least 0.80, accuracy over all cases above the
rubric's by at least 0.05 (an abstention counts as a miss), zero false promotions, on at least
30 cases. Falsified by one false promotion, coverage below 0.80, or a gain below 0.05.

## What changes

| Field | Registered | This amendment |
|---|---|---|
| Label source | one person's judgement (drafter) | an AI reviewer, blind to the rubric and the drafts |
| Claim about the labels | independent human ground truth | AI-assigned reference labels, partly checked by a person |

**The claim therefore weakens, and the paper must say so.** H1 would be reported as "beats the
rubric on labels assigned by an AI reviewer", never as "on independent human labels". A model
and a reviewer can share blind spots, and nothing here shows that the AI's labels are correct,
only that they were not copied from the rubric.

## The reviewer

- **Not the drafter's family.** The draft labels were written by an earlier AI session of
  Claude on this project. A Claude reviewer is not independent of them, so Claude is excluded.
- **Not the candidate's family.** The candidate under test is Qwen3-Coder (the DWQ build served
  on the Mac mini). A Qwen reviewer would be marking its own relatives, so Qwen is excluded.
- **Which models (owner's choice 2026-10-07, ids checked against the providers' own pages on
  2026-10-07):**

  | Vendor | Model id | What was checked |
  |---|---|---|
  | OpenAI | `gpt-5.6-sol` | The [model page](https://developers.openai.com/api/docs/models/gpt-5.6-sol) names this id as the default snapshot. It does not say whether `temperature` can be set. |
  | Google | `gemini-3.6-flash` | The [model page](https://ai.google.dev/gemini-api/docs/models/gemini-3.6-flash) lists it as stable, last updated 2026-07-30, with no dated snapshot string. |
  | DeepSeek | `deepseek-flash` | The [change log](https://api-docs.deepseek.com/updates/) lists DeepSeek-V4.1-Flash, 2026-09-10, as the newest model under this name. |

  Not chosen, and why. `gemini-3.1-pro-preview` is a preview model, so the model behind the id
  can change between the first and the second reply [UNVERIFIED: this rests on a search
  snippet, not on Google's own page]. A Gemini 3.7 Flash page exists in Google's documentation
  [UNVERIFIED: its id and stage were not read]. DeepSeek's larger `deepseek-v4-pro` (2026-08-13)
  is an older and larger model; the owner asked for the latest, which is V4.1 Flash. The owner
  may change any of these before registration.

  The version string the provider reports in each reply is recorded at run time; the id above
  is what is requested. If the provider does not report one, the amendment records the date
  and time and says so. DeepSeek and Qwen come from different vendors but may share training
  data and habits, which is a stated threat, not a proof of independence.
- **Perplexity is not used as a reviewer.** It is a search service that can browse the web and
  routes a question to whichever underlying model it picks, which may be Claude or GPT. That
  breaks two rules: no web, and a reviewer that is named and not Claude. It could join only
  with web search off and one named underlying model fixed, which its interface may not allow;
  if the owner wants it, the underlying model must be shown on the reply and be none of the
  excluded families.
- **Interface (owner's choice 2026-10-07): each vendor's chat page, an API only where one is
  free.** A chat page does not let the temperature be set and may add hidden instructions or
  tools of its own. So web search, memory and personalisation are turned off by hand before each
  conversation, each conversation is new, the model name the page shows is recorded, and the
  amendment says plainly that the settings are the page's defaults and could not be controlled.
  This is a stated threat to repeating the run.
- **Blind.** The reviewer sees only the generated review sheet (seeded shuffle, neutral case
  ids, the five route definitions, no rubric counts, no drafted labels, and the names of the
  models and builds masked by the generator with neutral labels, since the cases mention the
  build under test), in a new conversation
  with no memory, no tools, no web and no file access, using the prompt in
  `evals/h1_review/ai-reviewer-prompt.md`. It is never told the hypothesis, the candidate, or
  the word "rubric".
- **Settings:** [temperature 0 or the provider's lowest; record every setting the interface
  exposes]. One reply per model, saved byte for byte. A reply is not regenerated because the
  labels look odd; a malformed reply (not valid JSON, a missing case, a route outside the five)
  is the only reason to ask again, once, and both replies are kept.
- **Recorded for each reviewer:** model name and version string, UTC time, settings, SHA-256 of
  the prompt file, the sheet and the reply. Each reply is saved in its own file named for the
  reviewer and the attempt (a retry never overwrites the first reply).

## Disagreement rule (written before the sheet goes out)

- Three reviewers label every case. A case takes the label that at least two of them chose.
- A case where all three reviewers choose three different routes is **replaced by a spare
  case** (owner's decision, 2026-10-07). It leaves the 30-case H1 set; nobody, including the
  owner and Claude, picks its label after seeing the AI outputs. Its three labels and the owner's
  label stay in the record.
- **The replacement rule, frozen before registration.** There are 8 spare cases
  (`evals/h1_review/spare-cases-UNLABELED.jsonl`, masked sheet `evals/h1_review/spares/review-sheet.md`,
  ids `spare-01` to `spare-08`). The three reviewers label them in a separate conversation with the
  same prompt, and the owner labelled them blind. Split main cases are replaced in ascending order
  of case number, each by the lowest-numbered unused spare. A spare takes its final label by the
  same majority rule. A spare on which all three differ is skipped, not used, and reported, and the
  next unused spare is tried. All of this is mechanical; no one chooses which spare replaces which
  case.
- If the spares run out before every split main case has a replacement, the set has fewer than 30
  cases, H1's registered rule is **not applied**, and H1 is reported as not testable at the
  registered size. The candidate's and the rubric's accuracy on the remaining cases are still
  reported, marked exploratory. No case is written or added after registration.
- Reported: the number of three-way disagreements among the main cases, the number of
  replacements, which spares were used and which were skipped, and for each replaced case its
  three labels and the owner's label. Replaced cases are not part of H1's calculation.
- Reported: how many cases were unanimous, how many were two to one, how many split three ways,
  and how each split was resolved. A reviewer that disagrees with the other two on a large
  share of the cases is reported by name.

## The owner labels all of the cases

The owner (not the drafter: the 18 draft labels were written by an earlier AI session) labels
every case on the same masked sheet the AI reviewers get, **before seeing** any AI reviewer's
labels, the drafted labels, `lab/rubric.py`, or any other prior label. The owner confirmed on
2026-10-07 that they had not seen the drafts. The owner built the project and knows how the
routes are meant to work, so this is a weaker check than an outside person, and the paper says
the labels were checked by the project owner.

**Recorded:** the owner's 30 labels were received on 2026-10-07 at 06:57 UTC and saved as
`evals/h1_review/answers-owner.json` (SHA-256
`3133d71ea59a1497e93b01d5a885053888755a25e05cb4a67ce299f09516b9f1`), made on the masked sheet at
`main` commit `d2866baa430d99d98b6d0283be0ed6386a29f123`. The counts are post 15, blog 6, paper 4,
insufficient_evidence 4, no_artifact 1. At that time no AI reviewer had seen the sheet, the
owner states they had seen neither the draft labels nor any AI labels, and the labels had not
been compared with the drafts or with the rubric's output. Only the file's structure was
checked (every case id present, every route one of the five).

The owner's labels are an **independent human check on the AI labels, not a fourth vote**, and
are kept separate from the AI consensus: the final label of a case is the label at least two of
the three AI reviewers chose, and the owner's label never enters that count. The owner's
agreement with the final labels is reported next to the H1 result as a count and a table of
which routes were swapped, across all cases that have a final label. There is no pass or fail
threshold on it and no result depends on it; low agreement weakens the claim that the labels
are right, and the paper says so in those words.

## The case file

- 30 buildable cases: the 16 draft cases that build in a ledger, and 14 new cases, plus 8 spare
  cases that are used only under the replacement rule above.
  `evals/h1_review/extra-cases-UNLABELED.jsonl` holds the 14. The two draft cases that cannot
  be built are left out.
- **The 14 new cases and the 8 spares were written by Claude from real events in this repository's history**
  (closed issues, merged pull requests and the reports in `docs/reviews/`), with the sources
  named in each case, and the issue and pull request numbers were checked to exist. They carry
  **no label**: the labels come only from the reviewers. Claude chose which events to include
  and how to word each claim, and so shaped what the reviewers see; that is a stated threat. A
  reviewer or the owner who finds a case unfair, wrong or leading can say so before the case
  file is frozen. Claude did not assign or hint at any label.
- The 14 cover single incidents, patterns across incidents with a stated mechanism, a
  measurement with a control but no baseline, a claim with contradicting evidence, thin evidence,
  and a case with no claims.
- The case file is frozen by its SHA-256 before the sheet is generated; labels for the 14 are
  added afterwards from the reviewers and the freeze records both files.
- The instrument (`lab shadow`) is frozen by the lab commit (40 characters), as registered.

## Reported with the result

Reviewer agreement with the drafts (from `lab reviewsheet compare`); agreement between the
reviewers; the disagreements and their resolution; the owner's agreement with the final labels,
kept apart from the AI consensus; the main cases replaced after a three-way split, with their
three labels and the owner's label, and the spares used and skipped; the rubric's
accuracy on the final file next to the candidate's; and the AI-labels caveat above, in the
abstract, not only in the text.

## Timing, and one departure stated now

Registered on OSF before any model sees any case. The first reviewer reply's provenance record
must come after the registration timestamp. **Departure:** this amendment's earlier text said it
would be registered before the sheet was generated. The sheet was generated and published in
this public repository on 2026-10-07 (`main` commit
`d2866baa430d99d98b6d0283be0ed6386a29f123`), and the owner labelled it, before registration.
No AI reviewer has seen it. The registered text states this so the order is not misread.

## What is frozen

| Field | Value |
|---|---|
| Registration date and time (UTC) | [to be given by the owner after registering on OSF] |
| Draft case file `evals/shadow_cases_DRAFT.jsonl` (16 of its 18 cases are used) | `0e5cf74440e7d2c9737d8ed995e14d8d2052e7dbf7f1fc63858777bdcf4d3274` |
| Left out because a ledger cannot be built from them | `d-paper-one-source`, `d-paper-control-contradicts` |
| New unlabeled case file `evals/h1_review/extra-cases-UNLABELED.jsonl` (14 cases) | `fb3e1ea5fcfa97a3ddab918b214f6f85a686dcfb65a7d9c9693aafcf29b6e925` |
| Number of cases | 30, plus 8 spares used only under the replacement rule |
| Spare case file `evals/h1_review/spare-cases-UNLABELED.jsonl` (8 cases, frozen before registration) | `a47b5012941ccfed69bd08fafa97d82b6fe1cf9a711c9f16adc3a748710364a3` |
| Masked spare sheet `evals/h1_review/spares/review-sheet.md` | `77ec11c473a6d3c04b40423aea9bb0903c0462c06fd96bcfa20c4f3848785c1e` |
| Spare answers template `evals/h1_review/spares/answers-template.json` | `609beea3ed9261ad261c79ee5e157b3dd51c208284ddb2040e5fe7e22b946e09` |
| Masked review sheet `evals/h1_review/review-sheet.md` | `156d96c87d1a6ed90157c1454b004059bad35767dd3cb3b1073598e4a8accc0c` |
| Answers template `evals/h1_review/answers-template.json` | `54133d28f588e3d45ff838c58fab5b31885a6ba8f6e66bb10a7d21fc0a22b6e5` |
| Reviewer prompt `evals/h1_review/ai-reviewer-prompt.md` | `6e9fc9c47c2dcc066f8d00764222d1b76caf12b35b489bee3704b93a39831838` |
| Owner's labels `evals/h1_review/answers-owner.json` (received 2026-10-07 06:57 UTC) | `3133d71ea59a1497e93b01d5a885053888755a25e05cb4a67ce299f09516b9f1` |
| Reviewer models requested | `gpt-5.6-sol`, `gemini-3.6-flash`, `deepseek-flash`, on each vendor's chat page |
| Settings per reviewer | the chat page's defaults, which cannot be set; web search, memory and personalisation off; a new conversation each; the model name the page shows is recorded with each reply |
| Instrument | `lab/shadow.py` `71fa73bb736387243b01a31976c9544835d6d2e64285f5ba37de670befbc3bee`, `lab/rubric.py` `164e9d6e5d6761171fb9a5e877f8b94ac26be44606490883144c305b12537133`, `lab/reviewsheet.py` `4d7f395a294534f2794b9fc2922bba2723d7cdbf0c45dfe9039da21beffd0128`; `main` commit `d2866baa430d99d98b6d0283be0ed6386a29f123` (the last commit that changed `lab/shadow.py` or `lab/rubric.py` is `2193a4aa76dfb9a6526d2d5c7f4a17f66111162b`) |
| Disagreement rule | majority of three; a three-way split is replaced by the next unused spare, in order; H1 not testable if the spares run out |
| Owner's labels | an independent check, kept apart from the AI consensus, reported as a count and a table, no threshold |

The masked sheet is made from the two case files by the command in `evals/h1_review/README.md`;
the reviewer sees the sheet, not the case files. The 14 new cases get their labels only from the
reviewers, and the final labelled file is written after the replies are in and frozen by hash then.

## Decisions

All made by the owner on 2026-10-07: chat pages for all three reviewers (an API only where one is
free); a three-way split on a main case is replaced by a pre-written, frozen spare case, and the
number of replacements and splits is reported; the owner's labels are a separate human check, not
a fourth vote, with no pass or fail threshold, reported as a count and a table; the 14 new cases
and 8 spares from this repository's history, written by Claude and unlabeled; the two unbuildable
draft cases left out; H1 is run despite the weaker claim.
