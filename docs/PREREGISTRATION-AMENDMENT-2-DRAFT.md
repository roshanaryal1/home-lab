# Amendment 2 to the pre-registration: AI-assigned labels for H1 (DRAFT)

**Status: draft, not registered, not frozen.** Drafted 2026-10-07 for
[#84](https://github.com/roshanaryal1/home-lab/issues/84), at the owner's direction that an AI
will label the H1 cases. Every bracketed item is an open decision for the owner. No H1 run
has happened, and none may happen until this amendment is dated, filled in, registered on OSF
and its hashes are recorded.

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
- A case where all three differ is labeled by [OWNER TO CHOOSE: the owner, after a stated gap
  and without seeing the rubric's output, or the case is dropped and the file topped up from
  the spare cases]. The drafter's label is never used to break a tie, and is not shown.
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

Agreement between the owner and the final labels is reported next to the H1 result. If the owner
disagrees with the final label on more than [threshold, for example 6 of 30] cases, the result is
reported as inconclusive about the labels, whatever H1's rule says.

The final label of a case is the label at least two of the three AI reviewers chose. The owner's
labels are a check on that, not a fourth vote; [OWNER TO CHOOSE: or the owner is a fourth voter and
a two to two split goes to a stated rule].

## The case file

- 30 buildable cases: the 16 draft cases that build in a ledger, and 14 new cases.
  `evals/h1_review/extra-cases-UNLABELED.jsonl` holds the 14. The two draft cases that cannot
  be built are left out.
- **The 14 new cases were written by Claude from real events in this repository's history**
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
reviewers; the disagreements and their resolution; the owner's agreement with the final labels across all 30 cases; the rubric's
accuracy on the final file next to the candidate's; and the AI-labels caveat above, in the
abstract, not only in the text.

## Timing

Dated and registered on OSF before the sheet is generated or any model sees any case. The first
reviewer reply's provenance record must come after the registration timestamp.

## Fields to fill in before registration

| Field | Value |
|---|---|
| Date and time (UTC) | |
| Case file and SHA-256 | |
| Number of cases | |
| Reviewer models and version strings | |
| Settings per reviewer | |
| Prompt file SHA-256 | |
| Lab commit (40 characters) | |
| Disagreement rule | |
| Owner labels (all cases) and agreement threshold | |

## Open decisions for the owner

1. API or chat for each reviewer: chat, with an API only where one is free (decided).
2. What happens to a case where all three reviewers disagree.
3. Whether the owner is a check on the AI labels or a fourth voter, and the disagreement
   threshold.
4. Whether any of the 14 new cases should be changed or dropped after you read the sheet.
5. Whether H1 is worth running given the weaker claim.
