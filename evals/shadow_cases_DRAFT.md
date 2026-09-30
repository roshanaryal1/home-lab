# H1 shadow cases: DRAFTS, not for use

**Status: drafts for the owner to review. Not frozen, not registered, not
used by any run.** `lab shadow` has no default case file (`--cases` is
required), so nothing reads this file unless someone names it; do not pass it
to `lab shadow` before the review below.

H1 (`docs/PREREGISTRATION.md`) needs at least 30 labeled cases; 12 exist.
These 18 would make 30. They were drafted on 2026-09-29 UTC by the same
assistant that has read `lab/rubric.py`, which is exactly the bias the plan
warns about ("cases written by the same person who wrote the rubric favor
it"). To limit that:

- The proposed labels follow the policy in `docs/PIPELINE.md` (one incident
  is a post, a pattern with a mechanism is a blog, a measurement that
  survives a control is a paper), argued in the table below. Neither the
  rubric nor any model was run on these cases.
- Six are marked contestable: cases where a reasonable reader, or the
  rubric, could route differently. They are the most useful ones for H1.

**Update, 2026-09-30:** the rubric (no model) was afterwards run on these
cases by the same assistant, to see whether they load. Two cannot be built in
a ledger: `d-paper-one-source` and `d-paper-control-contradicts` describe
states the ledger refuses to reach. On the other 16 the rubric agrees with the
drafted label on 15; `d-blog-two-sources` differs (its two claims together
cite three distinct incident sources, which the rubric counts as a blog). So
the drafter has seen the rubric's output, and the drafted labels largely
reproduce it, which is why an independent relabel is required. Nothing was
frozen or registered. See `evals/h1_review/README.md` and #84.

Before any H1 run:

1. Someone other than the drafter, ideally not looking at `lab/rubric.py`,
   confirms or changes every label, especially the contestable ones.
2. The reviewed cases are appended to a new, dated case file, and a dated
   pre-registration amendment freezes it by SHA-256.
3. Only then is H1 run.

| id | proposed label | why | contestable |
|---|---|---|---|
| `d-blog-two-sources` | post | Pattern with a mechanism but only two distinct incident sources; the policy wants a pattern (three). | no |
| `d-blog-same-source` | post | Three incidents, all from one source; not three distinct incidents. | yes |
| `d-blog-mech-contradicted` | post | Pattern is solid, but the only mechanism claim is contradicted, so no usable mechanism. | no |
| `d-blog-mech-no-evidence` | post | Pattern, and a mechanism claim with no evidence at all. | no |
| `d-blog-four-sources` | blog | Four distinct incidents and a supported mechanism: a clear blog. | no |
| `d-blog-mech-same-source` | blog | Pattern of three distinct incidents; the mechanism rests on one of those same incidents. Arguably still a pattern with a mechanism. | yes |
| `d-paper-no-baseline` | post | Verified measurement with a control but no baseline; not a measurement that survives a control against a baseline. | no |
| `d-paper-one-source` | post | Verified measurement whose measurement, baseline and control all come from one run. | yes |
| `d-paper-control-contradicts` | insufficient_evidence | The control contradicts the measurement; the only claim is contested and nothing usable remains. | yes |
| `d-paper-two-measurements` | paper | Two verified measurements, each with measurement, baseline and control from different sources. | no |
| `d-paper-and-pattern` | paper | A verified controlled measurement plus a blog-level pattern; the highest route applies. | no |
| `d-post-single-commit` | post | One lesson from one commit. | no |
| `d-insufficient-only-contradicted` | insufficient_evidence | The only claim's only evidence contradicts it. | no |
| `d-mechanism-alone` | post | A supported mechanism with no pattern: one supported claim is a post. | yes |
| `d-blog-with-unverified-measure` | blog | Pattern with mechanism, plus a controlled measurement that is not verified; blog, not paper. | no |
| `d-contested-pattern` | post | The pattern claim is contradicted and drops out; the supported mechanism alone makes a post. | yes |
| `d-verified-without-measurement-type` | post | Verified measurement claim backed by baseline, control and an incident, but no measurement evidence. | no |
| `d-three-bare-claims` | insufficient_evidence | Three claims, none with any evidence. | no |
