# The personal agent market in October 2026, and where home-lab fits

Written 2026-10-09 for [#364](https://github.com/roshanaryal1/home-lab/issues/364). It follows
[COMPARISON.md](COMPARISON.md), which compares home-lab with OpenClaw and Hermes Agent file by
file. This page looks wider: the cloud agents that OpenAI, Meta, xAI, Google, Microsoft and
Anthropic launched in 2026, the open-source agents, what their users complain about, and the
gaps home-lab can fill.

**How this was researched.** Seven research passes on 2026-10-09 with web search. Many vendor
pages could not be opened from the research environment, so several facts come from press and
search summaries, not from the vendor. A fact that only one secondary source gives is marked
"(unverified)". These products change weekly. Check again before quoting anything here.
Sources are listed at the end with their dates.

## The short version

- In two months, three large companies shipped always-on personal agents: xAI Grok Bot (early
  beta, 2026-08-11) [1], Meta Muse (2026-09-08) [6][7] and OpenAI Dots (2026-09-29) [10][11].
  All three run the agent on a computer in the vendor's cloud, with a browser, connectors to
  the user's accounts, memory and schedules. Google's Gemini Spark (2026-05-19) works the same
  way on Google Cloud machines [16].
- The open-source agents are large and move fast. OpenClaw and Hermes Agent ship releases most weeks
  [20][23]. Both run on the user's own machine, but OpenClaw's own security policy says its
  sandbox is off by default and plugins run as trusted code [21].
- Users and researchers report the same problems across all of them: prompt injection that
  OpenAI and the UK NCSC say cannot be fully solved [25][26], malicious skills and plugins [22][27], unclear
  use of personal data [8][9], weak undo and audit, long tasks that fail without saying so
  [14], surprise bills, and whole regions left out [10][17].
- On 2026-10-09 the owner chose a **hybrid** position for home-lab: local by default, with an
  opt-in cloud runner and hosted models for people who want them. The four things to build
  first: scheduled tasks, a safe browser, an easy install, and injection defence.
- home-lab cannot match their connector counts or polish in six months. It can be the best
  choice for people who want an agent they can verify: every risky action signed by the owner,
  the data on the owner's machine, and the safety claims measured in public.

## The products

| Product | Launched | Where the agent runs | How it asks before acting | Price | Who can get it |
|---|---|---|---|---|---|
| OpenAI Dots | 2026-09-29 [10] | Its own cloud computer and browser, GPT-6 Astra [10] | Per action type: act alone, act with pre-approval, ask first, or hand back. A separate check before account or sharing actions. Background research is read-only [11][12] | First dot included with Pro ($100, $200 or $500 a month) (unverified) [13] | Not Pro users in the EEA, Switzerland or the UK at launch [10] |
| Meta Muse | 2026-09-08 [6][7] | A cloud VM per user, Muse Spark model [6] | Sign-off before sending email or buying. A separate checker agent is reported (unverified) [7] | Free, $20 or $100 a month [9] | US adults at launch [7] |
| xAI Grok Bot | Beta 2026-08-11 [1] | A cloud computer. The docs, as summarised, say a user's bots share one computer, files and logins [2] | Allow once, always allow or deny, with a review model on commands [3] | Bundled into SuperGrok and Cursor plans [4] | Regions not stated |
| Google Gemini Spark | 2026-05-19 [16] | Google Cloud machines [16] | Not found | Google AI Ultra (prices conflict) [16] | Not the EEA, UK, Switzerland or Nigeria [16] |
| Anthropic Claude Cowork | Generally available 2026-04-09 [18] | Desktop app and cloud [18] | Auto, manual or skip per action. Deletes always ask [18] | Pro $20, Max $100 or $200 [19] | Paid plans [18] |
| OpenClaw | 2026.9.9, 2026-10-08 [20] | The user's own machine | Exec approvals. Sandbox off by default [21] | Free (MIT), plus model costs | Anywhere |
| Hermes Agent | v0.21.6, 2026-10-08 [23] | The user's machine or one of several backends [24] | "Smart" approvals, default since v0.19.0 [23] | Free (MIT), plus model costs | Anywhere |
| home-lab | No release yet | The owner's Mac, as a separate `lab` account. Model on the Mac | Approve-tier actions need the owner's Ed25519 signature over the exact action | Free (MIT). No model bill with the local model | Anywhere, once installed |

The research also looked at Microsoft's, Amazon's and Perplexity's agents, at Manus, and at the
open-source Goose, OpenHands, Open Interpreter and Letta. None of them changes the picture below.
Apple's Siri AI beta is not offered in the EU [17].

## What users and researchers complain about

Ranked by how many sources raised each problem in the research.

1. **Prompt injection.** Text in a web page, email or file can steer the agent. OpenAI says it is
   "unlikely to ever be fully solved" [25] and the UK NCSC says it may never be fully mitigated
   [26]. Browser agents were hijacked in published attacks in 2025 and 2026 [28]. OpenAI says its
   own Dots safeguards reduce the risk but do not remove it [12].
2. **Malicious skills and plugins.** In February 2026 researchers flagged 341 malicious skills
   in OpenClaw's skill registry, most from one campaign that installed a password stealer
   [22][27]. MCP servers have been cloned to spread malware [29].
3. **Unclear data use.** Muse trains on interactions by default with an opt-out (unverified)
   [9], and WIRED reports that Meta can technically reach a user's Muse VM [8]. Dots memory
   cannot be viewed or deleted item by item [11].
4. **Weak undo, audit and stop.** Grok Bot's action recording is for Enterprise only and off by
   default (unverified) [3]. An OpenClaw user's agent bulk-deleted emails after its
   confirm-first rule fell out of its context, and stop commands did not stop it [30].
5. **Long tasks that fail without saying so.** A Dots user reports a dot that keeps reporting
   progress without finishing [14]. Grok Bot users report bots down for days [5].
6. **Surprise bills.** Self-hosted OpenClaw users report monthly model bills in the hundreds of
   dollars (blog reports, unverified) [31]. Dots comes with Pro plans of $100 to $500 a month
   (unverified) [13]. Muse and Cowork have $20 and $100 tiers [9][19].
7. **Regions left out, and new rules.** Dots Pro, Gemini Spark and Siri AI are not offered in
   parts of Europe [10][16][17]. In the EU, the AI Act's duty to tell people they are dealing with
   an AI system applies from 2026-08-02 [32].
8. **Claims nobody can check.** The vendors' safety claims rest on their own descriptions. The
   research found no independent, rerunnable prompt-injection test of Dots, Muse or Grok Bot
   [12].

## Where home-lab stands against each

| Problem | What home-lab has today | What is still missing |
|---|---|---|
| Prompt injection | Read content is tainted data and can never carry a grant. Risky tools need the owner's signature. H4: 0 of 9 injection attacks succeeded with the real model ([PREREGISTRATION.md](PREREGISTRATION.md)) | A larger public injection suite over web pages, email and files, rerun on every release |
| Malicious skills | Skills become active only on the owner's signed promotion, and their scripts run in a container with no network (M6, M5) | A small curated index with pinned hashes (feature 10) |
| Data use | Memory, logs and the database stay on the Mac. Memory is shown, corrected and deleted by the owner. Nothing is sent to a model vendor with the local model | An export of memory and the action log in a plain format |
| Undo, audit, stop | A hash-chained audit log. An emergency stop that revokes the broker's authority and kills workers. Rules live in the broker, not in the model's context | A one-page view of what the agent did today |
| Silent failures | Leases, a watchdog, startup recovery, alerts | A check that flags a task that keeps reporting progress without finishing |
| Bills | The local model has no per-token bill | A cost meter and a hard daily cap for the opt-in hosted model |
| Regions | Software the owner runs, so no region list | Nothing |
| Claims nobody can check | Pre-registered claims with sealed records (M2, M5, M6, H1, H4) | No independent security review yet. That is booked for month 4 of the plan |

What home-lab lacks that the others have: connectors to mail and calendars, more than one chat
channel, a browser that acts on websites, schedules, an easy install, and polish. The plan
below puts those first, in the order the owner chose.

## The position: hybrid, local by default

The owner's decision on 2026-10-09:

- **Local by default.** Everything runs on the owner's Mac. Nothing leaves without the owner's
  say. This is what none of the cloud agents offer.
- **Opt-in cloud.** For people who want an agent that keeps running while their Mac sleeps, or a
  stronger model than their Mac can hold: an opt-in hosted model per task class (feature 12)
  and an opt-in cloud runner, a lab the owner runs on a machine they rent, under the same
  signatures and the same broker. Both off by default, both stated in the audit log.
- **The rule that does not change.** No mode, local or cloud, lets a model approve an action.
  Only the owner's signature does.

## What "on top" means for home-lab by April 2027

Measurable, so the plan can be checked:

1. The only personal agent whose safety claims are pre-registered, sealed and rerunnable by
   anyone, with the injection suite and its results published for every release.
2. A fresh Mac goes from install to the first signed task in under 30 minutes, measured on
   three people's Macs (#188).
3. Scheduled standing goals, a browser in the container and an opt-in hosted model, all under
   the same signature rule.
4. Runs with no cloud account at all, in any country.
5. A public website that says what is measured and what is not (#363).

The month-by-month work is in [PLAN-7-MONTHS.md](PLAN-7-MONTHS.md) and the features in
[FEATURE-PLAN.md](FEATURE-PLAN.md), both updated for this decision.

## Sources

Dates are publication dates where the page or search result gave one. Every source marked
"undated" was read, or seen in a search result, on 2026-10-09, so that is the date of the fact
taken from it. "Search summary" means the fact was seen in a search result, not on the page.

1. xAI, "Introducing Grok Bot", 2026-08-11 (search summary). https://x.ai/news/introducing-grok-bot
2. xAI docs, Grok Bot computer and apps, undated (search summary). https://docs.x.ai/grok-bot/computer-and-apps
3. xAI docs, Grok Bot security, undated (search summary). https://docs.x.ai/grok-bot/security
4. xAI, "Grok Bot on more plans", 2026-08-21 or 2026-08-26 (dates conflict). https://x.ai/news/grok-bot-more-plans
5. Cursor forum, "Grok Bot can't reach your computer for 48 hours", undated. https://forum.cursor.com/t/grok-bot-can-t-reach-your-computer-for-48-hours/169450
6. Axios, 2026-09-08 (search summary). https://axios.com/2026/09/08/meta-debuts-muse-personal-ai-agent
7. AP via La Nacion, 2026-09-08. https://www.lanacion.com.ar/usa/meta-launches-personal-ai-agent-muse-emphasizes-safety-and-privacy-nid08092026/
8. WIRED review, reprinted by DNYUZ, 2026-09-20. https://dnyuz.com/2026/09/20/metas-muse-is-better-at-surveilling-than-helping-me/
9. TechCrunch, 2026-09-10 (search summary), and Captain Compliance, undated. https://techcrunch.com/2026/09/10/metas-ai-agent-muse-is-now-the-no-2-app-in-the-us/ and https://captaincompliance.com/?p=13651
10. OpenAI Help Center, "Getting started with your dot", undated (search summary). https://help.openai.com/en/articles/20001530
11. OpenAI Help Center, dots privacy, security and safety, undated (search summary). https://help.openai.com/en/articles/20001529
12. OpenAI, "How we build safety, security and privacy into dots", undated (search summary). https://openai.com/index/how-we-build-safety-security-and-privacy-into-dots/
13. eesel, Dots pricing, about 2026-10-01. https://www.eesel.ai/blog/openai-dots-pricing
14. OpenAI developer forum, a dot that reports progress without finishing, about 2026-10-01. https://community.openai.com/t/dot-repeatedly-reports-progress-but-fails-to-complete-tasks-or-clearly-disclose-that-work-has-stopped/1402528
15. TechCrunch, 2026-09-29. https://techcrunch.com/2026/09/29/openai-launches-dots-its-bubbly-agentic-avatar/
16. Google Gemini Help, Gemini Spark, undated. https://support.google.com/gemini/answer/17171264
17. Engadget, June 2026, Siri AI delayed in the EU. https://engadget.com/2189932/siri-ai-for-iphones-and-ipads-will-be-delayed-indefinitely-in-the-eu
18. Claude Help, "Get started with Claude Cowork", undated. https://support.claude.com/en/articles/13345190-get-started-with-claude-cowork
19. Claude product page, Cowork, read 2026-10-09. https://claude.com/product/cowork
20. OpenClaw releases, read 2026-10-09. https://github.com/openclaw/openclaw/releases
21. OpenClaw SECURITY.md, read 2026-10-09. https://github.com/openclaw/openclaw/blob/main/SECURITY.md
22. The Hacker News, 341 malicious ClawHub skills, 2026-02. https://thehackernews.com/2026/02/researchers-find-341-malicious-clawhub.html
23. Hermes Agent releases, read 2026-10-09. https://github.com/NousResearch/hermes-agent/releases
24. Hermes Agent README, read 2026-10-09. https://github.com/NousResearch/hermes-agent
25. TechCrunch, "OpenAI says AI browsers may always be vulnerable to prompt injection attacks", 2025-12-22. https://techcrunch.com/2025/12/22/openai-says-ai-browsers-may-always-be-vulnerable-to-prompt-injection-attacks/
26. UK NCSC, on prompt injection, undated, and Security Boulevard's report of it, 2025-12. https://www.ncsc.gov.uk/news/mistaking-ai-vulnerability-could-lead-to-large-scale-breaches and https://securityboulevard.com/2025/12/prompt-injection-cant-be-fully-mitigated-ncsc-says-reduce-impact-instead/
27. CSO Online, ClawHub malware and VirusTotal scanning, 2026-02. https://csoonline.com/article/4129393/openclaw-integrates-virustotal-malware-scanning-as-security-firms-flag-enterprise-risks.html
28. Cloud Security Alliance, PleaseFix agentic browser exploits, 2026-03-03, and Aviatrix, BioShocking, 2026-06-24. https://labs.cloudsecurityalliance.org/research/csa-research-note-pleasefix-agentic-browser-exploits-2026032/ and https://aviatrix.ai/threat-research-center/new-bioshocking-attack-manipulates-ai-browser-into-data-theft-2026
29. Aviatrix, SmartLoader cloned MCP server, 2026-02. https://aviatrix.ai/threat-research-center/smartloader-oura-mcp-server-stealc-2026/
30. TechCrunch, an OpenClaw agent ran amok on a researcher's inbox, 2026-02-23. https://techcrunch.com/2026/02/23/a-meta-ai-security-researcher-said-an-openclaw-agent-ran-amok-on-her-inbox
31. ClawdHost blog, OpenClaw API costs, undated (unverified). https://clawdhost.net/blog/openclaw-api-costs-what-nobody-tells-you
32. Mondaq, "EU AI Act transparency obligations are now in force", 2026-07. https://www.mondaq.com/uk/compliance/1826042/not-delayed-not-deferred-eu-ai-act-transparency-obligations-are-now-in-force
