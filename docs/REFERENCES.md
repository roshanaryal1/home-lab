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
| 2601.06112 | ReliabilityBench: Evaluating LLM Agent Reliability Under Production-Like Stress Conditions (Gupta) | Preprint | none found |

"None found" means none in the sources above on that date, not that none
exists. Most of these were posted in 2026 and may be under review. To
recheck: query Semantic Scholar or DBLP with an API key, and search the
title on Google Scholar for a venue version.

Preprints are not peer reviewed. The docs use their figures as reported
claims to check, and a paper written from this lab should say "preprint"
when citing them.

## Other factual claims in the docs, checked 2026-09-29 UTC

| Claim (where) | Result | Source read |
|---|---|---|
| AutoGen is in maintenance mode (PLAN 1.3) | **Verified**: its README says it "is now in maintenance mode" and points to Microsoft Agent Framework. When it entered maintenance mode is not stated. | `microsoft/autogen` README |
| Microsoft Agent Framework 1.0, GA April 2026 (PLAN 1.3) | **Verified**: `python-1.0.0` and `dotnet-1.0.0` released 2026-04-02 as stable releases | `microsoft/agent-framework` releases |
| AG2 is the community fork of AutoGen (PLAN 1.3) | **Verified**: `ag2ai/ag2`, "AG2 (formerly AutoGen)" | GitHub record |
| CrewAI, 5.2M monthly downloads (PLAN 1.3) | **Not matched**: 2,431,720 in the last month (pypistats). Corrected in PLAN. | pypistats API |
| Laya: Convai Innovations, Apache-2.0, 421M parameters, released 2026-09-18 (PLAN 1.1) | **Verified**: `convaiinnovations/laya`, created 2026-09-18, Apache-2.0, 421,293,830 parameters | Hugging Face model record |
| Jev: closed API from TypeSafe AI, released 2026-09-15 (PLAN 1.1) | **Verified**: announced 2026-09-15, "available today in early access", no open weights | TypeSafe AI's announcement post |
| OpenClaw repository dates and size, Hermes Agent dates | See PLAN and SUBSYSTEMS: creation dates verified, launch dates and line counts marked [UNVERIFIED] | GitHub records |

| Kev-9B: LoRA on `Qwen3.5-9B-Base`, Apache-2.0; 0.822 dev and 0.852 locked test, Jev 0.857 dev (PLAN 1.1) | **Verified** in `jaredpalmer/kev-9b`'s model card. Jev's 0.857 is Kev's authors' figure, not TypeSafe's. | Hugging Face model card |
| Laya: about 33 to 39.5 ms per question (PLAN 1.1) | **Imprecise, corrected**: 39.5 ms is this English checkpoint and 32.8 ms the multilingual one, both on a T4 GPU | `convaiinnovations/laya` model card |

