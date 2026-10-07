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
- **Web search, memory and personalisation are off** in every interface. Where an API is
  available it is preferred, so that the settings below can be set and recorded.
- **Blind.** The reviewer sees only the generated review sheet (seeded shuffle, neutral case
  ids, the five route definitions, no rubric counts, no drafted labels), in a new conversation
  with no memory, no tools, no web and no file access, using the prompt in
  `evals/h1_review/ai-reviewer-prompt.md`. It is never told the hypothesis, the candidate, or
  the word "rubric".
- **Settings:** [temperature 0 or the provider's lowest; record every setting the interface
  exposes]. One reply per model, saved byte for byte. A reply is not regenerated because the
  labels look odd; a malformed reply (not valid JSON, a missing case, a route outside the five)
  is the only reason to ask again, once, and both replies are kept.
- **Recorded for each reviewer:** model name and version string, UTC time, settings, SHA-256 of
  the prompt file, the sheet and the reply.

## Disagreement rule (written before the sheet goes out)

- Three reviewers label every case. A case takes the label that at least two of them chose.
- A case where all three differ is labeled by [OWNER TO CHOOSE: the owner, after a stated gap
  and without seeing the rubric's output, or the case is dropped and the file topped up from
  the spare cases]. The drafter's label is never used to break a tie, and is not shown.
- Reported: how many cases were unanimous, how many were two to one, how many split three ways,
  and how each split was resolved. A reviewer that disagrees with the other two on a large
  share of the cases is reported by name.

## A person checks a sample

[OWNER TO CHOOSE: a person other than the drafter labels a seeded random sample of 10 cases
from the same sheet.] The agreement between that person and the AI labels is reported next to
the H1 result. If agreement is below [threshold, for example 8 of 10], the result is reported
as inconclusive about the labels, whatever H1's rule says.

## The case file

- At least 30 buildable cases. 16 of the 18 drafts build in a ledger today; the other 2 are
  excluded or rewritten as states the ledger can reach.
- [OWNER TO CHOOSE: where the extra 14 cases come from. Proposed: real incidents and
  measurements from this repository's own history, not invented ones, chosen before any
  reviewer sees them. Cases written by the same AI that then labels them would label its own
  work and are not allowed.]
- The case file is a new dated file, frozen by its SHA-256 before the sheet is generated.
- The instrument (`lab shadow`) is frozen by the lab commit (40 characters), as registered.

## Reported with the result

Reviewer agreement with the drafts (from `lab reviewsheet compare`); agreement between the
reviewers; the disagreements and their resolution; the person's sample agreement; the rubric's
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
| Sample reviewer (role) and sample size | |
| Agreement threshold for the sample | |

## Open decisions for the owner

1. API or chat interface for each (the models are chosen above; Perplexity is explained above),
   and whether `gpt-5.6-sol` lets you set the temperature.
2. What happens to a case where all three reviewers disagree.
3. Whether a person checks a sample, who, and the agreement threshold.
4. Where the extra 14 cases come from.
5. Whether to rewrite or drop the two unbuildable cases.
6. Whether H1 is worth running at all if the sample check is skipped, given the weaker claim.
