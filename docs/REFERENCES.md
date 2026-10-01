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
| 2406.13352 | AgentDojo: A Dynamic Environment to Evaluate Prompt Injection Attacks and Defenses for LLM Agents (Debenedetti) | **Published**, peer reviewed | *Advances in Neural Information Processing Systems 37* (NeurIPS 2024), Track on Datasets and Benchmarks, pp. 82895-82920, [doi:10.52202/079017-2636](https://doi.org/10.52202/079017-2636). Crossref record: same title and six authors. The track is stated on the first page of the [proceedings copy](https://proceedings.neurips.cc/paper_files/paper/2024/file/97091a5177d8dc64b1da8bf3e1f6fb54-Paper-Datasets_and_Benchmarks_Track.pdf), read 2026-09-30. |
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

## Agent-security evaluation sources for the pre-registered case sets (#242), checked 2026-10-01 UTC

These are the suites and primary artefacts read to pick the attack categories
in `evals/prereg/` (see `docs/PREREGISTRATION-SAFETY.md`). Each was read
directly as cited. Where a paper is behind a host this network cannot reach,
the arXiv id and venue come from the project's own README or citation block,
read on the date below, and are marked as such; the figures are the project's
own, not re-derived here. Preprints are marked as preprints.

| Source | What it is | Read | Used for |
|---|---|---|---|
| AgentDojo (Debenedetti et al.) | 97 tasks, 629 security test cases for prompt injection on tool-using agents; utility and attack-success scored on state. Published at NeurIPS 2024 (see the table above); arXiv 2406.13352. | `README.md` of `ethz-spylab/agentdojo` and the package (PyPI `agentdojo` 0.1.35, 2025-10-27), 2026-10-01 | M2 injection styles; the state-based utility-and-attack-success grading the lab already copies in `lab/attacks.py` |
| InjecAgent (Zhan et al.) | Benchmark of indirect prompt injection in tool-integrated agents: 1,054 test cases over 17 user tools and 62 attacker tools; direct-harm and data-stealing attacks. arXiv 2403.02691; the README states Findings of ACL 2024 (not registry-confirmed here). | `README.md` of `uiuc-kang-lab/InjecAgent`, 2026-10-01 | M2 indirect-injection cases (a benign request whose tool result carries the attacker instruction) |
| Agent Security Bench, ASB (Zhang et al.) | Formalises attacks and defences across 10 scenarios, over 400 tools, 27 attack/defence methods, 13 LLM backbones; attack types direct prompt injection, observation injection, memory poisoning, Plan-of-Thought backdoor, mixed. ICLR 2025; arXiv 2410.02644 (preprint id), per the README and its BibTeX. | `README.md` of `agiresearch/ASB`, 2026-10-01 | M2 injection taxonomy (direct vs observation vs memory) |
| WASP (Evtimov et al.) | Web-agent prompt-injection benchmark built on VisualWebArena; arXiv 2504.18575 (preprint id per README). Repository archived 2026-07-01. | `README.md` of `facebookresearch/wasp`, 2026-10-01 | M2 indirect-injection framing (attacker text placed in content the agent navigates) |
| CaMeL (Debenedetti et al.) | "Defeating prompt injections by design": a defence that separates control and data flow. arXiv 2503.18813 (preprint id per README). Research artefact, not a product. | `README.md` of `google-research/camel-prompt-injection`, 2026-10-01 | Context for why the lab's control (Rule of Two, signed approvals) is a design boundary, not a model behaviour |
| MCP tool-poisoning experiments (Invariant Labs) | Working examples of tool poisoning, tool shadowing, and a sleeper rug pull on MCP servers, with exfiltration hidden after whitespace. | `README.md` of `invariantlabs-ai/mcp-injection-experiments`, 2026-10-01 | M6 MCP and rug-pull cases; the smuggled-reply M2 case |
| SandboxEscapeBench (Marchand et al.) | Inspect-AI container-escape eval: 18 scenarios across orchestration, runtime and kernel layers, difficulty 1 to 5, grounded in real CVE classes (privileged container, mounted docker socket, writable hostPath, host pid namespace, CAP_SYS_ADMIN, cgroup release-agent CVE-2022-0492, runc CVE-2019-5736, Dirty Pipe CVE-2022-0847, and others). arXiv 2603.02277 (preprint, 2026) per the README citation. Released by the UK AI Security Institute. | `README.md` of `UKGovernmentBEIS/sandbox_escape_bench`, 2026-10-01 | M5 escape families; which misconfigurations the lab's executor must never set |
| Apple `container` | Runs each Linux container in its own lightweight VM on Apple silicon, macOS 26+. The command reference documents `--network`, `--read-only`, `--mount`, `--volume`, `--user`, `--cap-drop`, memory and CPU limits. | `README.md` and `docs/command-reference.md` of `apple/container`, 2026-10-01 | M5 executor configuration referenced in ADR 0007 (`--network none`, workspace-only mount) |
| MalSkillBench (Guo et al.) | Runtime-verified benchmark of malicious agent skills: 3,944 malicious and 4,000 benign `SKILL.md` packages; taxonomy of code injection, prompt injection and mixed, with behaviours B1 to B15 (exfiltration, credential theft, remote code execution, persistence, reverse shell, role hijack, instruction override, and more). arXiv 2606.07131 (preprint) per the README citation. | `README.md` of `lxyeternal/MalSkillBench`, 2026-10-01 | M6 malicious-skill vectors and behaviour labels |
| SkillsGoat (Optimus Labs) | A vulnerable-by-design target for skill scanners: 89 single-skill fixtures (73 malicious, 16 benign) and 37 compound chains (benign alone, malicious only in combination), each with an answer key; families include metadata injection, exfiltration, over-permission, persistence, confused deputy, typosquatting, frontmatter gadgets, symlink escape, and rug-pull-over-time. | `README.md` of `optimuslabs-io/skillsgoat`, 2026-10-01 | M6 promotion-path and malformed-bundle cases; the compound-chain pair |
| Agent Skills specification | The `SKILL.md` format: required `name` and `description`, optional `license`, `compatibility`, `metadata`, `allowed-tools`; `scripts/`, `references/`, `assets/` directories. | `docs/specification` of the `agentskills` project, 2026-10-01 | M6 frontmatter and `allowed-tools` cases; the format the roadmap's M6 reads |

The OWASP Top 10 for Agentic Applications 2026 is already cited in `THREATS.md`
(the ASI01 to ASI10 mapping). The pre-registered case sets reuse that mapping:
M2 tests ASI01 (goal hijack) and ASI03 (identity and privilege abuse), M5 tests
ASI05 (unexpected code execution), and M6 tests ASI04 (agentic supply chain).

