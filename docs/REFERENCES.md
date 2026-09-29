# References: publication status

Every paper the docs cite, with whether a peer-reviewed version exists.
Checked 2026-09-29 UTC. A preprint is cited as a preprint; a venue, volume or
page range is written only when a registry record for it was read.

How each was checked: arXiv's own record (`journal_ref` and `doi` fields,
which authors fill in when a paper is published), OpenAlex (a title search
and the record for the arXiv DOI), and, where a DOI turned up, Crossref's
record for that DOI. Semantic Scholar and DBLP were tried but refused the
requests (rate limits), so they are not part of this check.

| arXiv | Title (first author) | Status | Published version |
|---|---|---|---|
| 2406.13352 | AgentDojo: A Dynamic Environment to Evaluate Prompt Injection Attacks and Defenses for LLM Agents (Debenedetti) | **Published**, peer reviewed | *Advances in Neural Information Processing Systems 37* (NeurIPS 2024), pp. 82895-82920, [doi:10.52202/079017-2636](https://doi.org/10.52202/079017-2636). Crossref record: same title and six authors. |
| 2608.27886 | Resource Constraints and Performance in Agentic AI Systems (Salman) | Preprint | none found |
| 2608.12851 | Practice Makes Unsafe: Skill Misevolution in Self-Improving LLM Agents (Mao) | Preprint | none found |
| 2608.13867 | Engineering Reliable Coding Agents: Evaluating and Operating the System Around the Model (Jarmak) | Preprint (the arXiv comment calls it a technical review and monograph) | none found |
| 2609.03192 | Where Reliability Lives: Experimental Localisation of Behavioural Properties in an Agent System (T. Marsden) | Preprint (its arXiv comment says so) | none found |
| 2605.18401 | SkillsVote: Lifecycle Governance of Agent Skills from Collection, Recommendation to Evolution (Liu) | Preprint | none found |
| 2602.13855 | From Fluent to Verifiable: Claim-Level Auditability for Deep Research Agents (Rasheed) | Preprint | none found |

"None found" means none in the sources above on that date, not that none
exists. Most of these were posted in 2026 and may be under review. To
recheck: query Semantic Scholar or DBLP with an API key, and search the
title on Google Scholar for a venue version.

Preprints are not peer reviewed. The docs use their figures as reported
claims to check, and a paper written from this lab should say "preprint"
when citing them.
