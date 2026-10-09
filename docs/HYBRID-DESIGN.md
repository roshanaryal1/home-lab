# Design: the opt-in hosted model and the opt-in cloud runner

Written 2026-10-09 (NZDT) for [#377](https://github.com/roshanaryal1/home-lab/issues/377). This is a design for the owner to approve. Nothing here is built. The document registers no claim and changes no configuration. Features 12 and 19 are planned for month 4, and feature 21 for month 5, of the [seven-month plan](PLAN-7-MONTHS.md). The feature rows are in [FEATURE-PLAN.md](FEATURE-PLAN.md).

Read it with [MARKET-2026.md](MARKET-2026.md), [ARCHITECTURE.md](ARCHITECTURE.md), [SECURITY.md](../SECURITY.md) and [ADR 0006](decisions/0006-authority-rules-and-rollout.md). Numbers in square brackets point to the sources in section 8.

## 1. The owner's decision and what "hybrid" means here

On 2026-10-09 the owner chose a hybrid position. [MARKET-2026.md](MARKET-2026.md) gives the reasons. Here it means three things:

- **Local by default.** The model runs on the owner's Mac, on loopback, as it does now. Nothing leaves the machine.
- **Opt-in hosted model (feature 12).** A hosted API serves only the task classes the owner names in a signed file. Off by default.
- **Opt-in cloud runner (feature 21).** A second lab runs on a machine the owner rents. Off by default.

Both opt-ins stay off until the owner signs a configuration that turns them on. They are independent. A runner can run without a hosted model, and a hosted model can run without a runner.

Three rules hold in every mode:

- No model approves an action. Only the owner's Ed25519 signature approves an approve-tier action ([lab/operator.py](../lab/operator.py)).
- The Rule of Two holds for each task ([lab/authority.py](../lab/authority.py)). A new tool gets its legs in `TOOL_LEGS` in the same change that adds it.
- Every broker call is audited in the hash-chained log.

## 2. The hosted model (feature 12)

### 2.1 Which provider, and what the terms say

The owner chooses the provider. Two were checked on 2026-10-09. Provider pages were opened where they would load. Where a page would not load from the research environment, the entry comes from a search summary and is marked unverified. Prices change. Read them again before any value enters the signed file.

| Fact | Anthropic Claude API | OpenAI API |
|---|---|---|
| Training on API inputs and outputs by default | No. The commercial terms say Anthropic "may not train models on Customer Content from Services" [2]. | No for Chat Completions and Responses, per a search summary (unverified) [5]. |
| Default retention | The policy page says conversation content is not retained by default, except Covered Models, which keep 30 days [1]. A search summary of the help article says deletion within 30 days (unverified) [4]. The two are not reconciled here. | Abuse-monitoring logs are kept up to 30 days by default, per a search summary (unverified) [5]. |
| Zero data retention | By request to sales, enabled per organization. The Messages API is eligible. Claude Fable 5 and 5.1 and Mythos 5 and 5.1 need 30-day retention and are not covered [1]. | By approval only, per organization or project, for Chat Completions and Responses. Conversations is not eligible (unverified) [5]. |
| Mid-size model, price per million tokens | Claude Sonnet 5.5: $2 input and $10 output. Cache reads $0.10. Batch $1 and $5 [3]. | GPT-5.6 Terra: $2 input and $12 output in the most recent summaries (unverified) [6]. One tracker still shows $2.50 and $15 (unverified) [7]. |

Consumer plans are outside this design. The design does not depend on which provider the owner picks. It needs one named host, one pinned model ID and a set of terms the owner has read.

### 2.2 Where the configuration lives and who owns it

- **File:** `/etc/homelab/hosted.json`. Root owns it. The lab account can read it and cannot write it. This is the same place and the same rule as the signed MCP and repository source files ([lab/handlers/__init__.py](../lab/handlers/__init__.py)).
- **Signature:** the whole file is signed with the operator key, under the purpose `hosted-model-config`, using `lab.operator.sign_action`. The supervisor checks it against `/etc/homelab/operator.pub`, the key it already reads ([lab/supervisor.py](../lab/supervisor.py)).
- **Start rule:** a file that does not verify stops the daemon at start. A valid file takes effect at the next start. The agent cannot change it while the daemon runs.
- **No tool reads or writes it.** No broker tool takes a class, a host, a price or a cap as a parameter.
- **No secret in it.** The file names a vault entry. The secret broker ([lab/vault.py](../lab/vault.py)) holds the API key, as it holds connector credentials.

The file holds:

- the provider's name and the one host it may call, a DNS name with no wildcard and no IP address
- the pinned model IDs, dated where the provider gives dated IDs
- the retention and training terms the owner accepted, and the date they were read
- the input and output price per million tokens, the date read, and the owner's entry of it
- the daily cap in USD, and the day boundary (`Pacific/Auckland` is proposed)
- one row per task class (section 2.3)
- `signed_by` and `signature`

### 2.3 Task classes and what may leave

A class is an agent kind that trusted code registers. A class missing from the file is off. Each class row names the fields that may leave, chosen from a fixed list in trusted code. Free text is never a field.

| Class | Default | Fields that may leave when on | Never leaves |
|---|---|---|---|
| Chat answer | Off | `chat_text`, and the reply context fields the owner lists | Memory, files, approval records, keys |
| Proposal summary | Off | `event_excerpt`, one bounded record from the evidence ledger | Queue rows, approvals, keys |
| Web summary | Off. Tainted input is refused unless the owner opts the class in | `evidence_excerpt` from a fetched page | Request headers, credentials, anything the fetch did not return |
| Workspace help | Off | Only the files the row names, one by one | Every file not named, keys |

Chat text is tainted by origin ([lab/origin.py](../lab/origin.py)). The chat class therefore needs the tainted opt-in as well.

### 2.4 How one request runs

1. A class that is off is never granted the hosted tool at start. A call from it fails as any ungranted tool does.
2. The broker checks the task's origin and sensitivity against the class row. Tainted input needs the class's opt-in. The sensitivity may be `public` or `internal`. `secret` is not a value this version accepts ([lab/origin.py](../lab/origin.py)).
3. The Rule of Two check runs ([lab/authority.py](../lab/authority.py)). The new tool adds the untrusted and external legs, so a handler that already holds sensitive data cannot also hold it.
4. The cost is reserved and the cap is checked (section 3). A request that would pass the cap waits.
5. The request goes through `EgressGateway.request`, the entry point the chat channel already uses for one fixed host and one fixed schema ([lab/egress.py](../lab/egress.py)). The gateway checks the name, resolves it, requires every address to be public, connects to the address it checked, and does not follow a redirect on a credentialed request.
6. The broker adds the key to that request only. The handler and its worker never receive it. Logs pass through the redactor.
7. The reply is parsed strictly and bounded in size, as tool calls are. It goes back to the task as evidence, never as an instruction. A reply that names a model other than the pinned one is refused, as `ModelMismatch` refuses a mismatch for the local model ([lab/model.py](../lab/model.py)).
8. The cost is settled from the provider's usage fields, and one audit row is written (section 2.6).

Hosted calls use a second bounded class, not `BoundedModel`. `BoundedModel` admits a request against memory, and its `ModelSpec` requires a 40 or 64 digit hash as the revision. The sources checked here give no weights hash for a hosted model. The hosted spec therefore pins the dated model ID and checks the model name in each reply. The loopback rule in `OpenAICompatibleAdapter` stays unchanged. The two classes share no code, so a wrong setting cannot send the local adapter to the internet.

The broker tool is `model.hosted` (name proposed). It sits at the notify tier, as `net.fetch` and `net.summarize` do. Its legs are untrusted input and external action, the same as `net.summarize`. Its effect is non-idempotent, so a lost reply goes to the operation journal and waits for the owner. It is not sent again.

### 2.5 When the provider fails

- A timeout, a 429, a 5xx, a malformed reply or a refused reply triggers the class's failure rule.
- The rule is `wait` or `local`. With `wait`, the task parks without a lease, and the owner sees it. With `local`, the task runs on the local model under its own admission check. If that check refuses, the task waits.
- No other hosted provider is tried. A second provider needs its own class row and its own named host. It is never a silent fallback.
- A request that may have left counts as sent. Its reserved cost stays on the meter.
- There is no automatic retry of a request that may have been sent.

### 2.6 The audit row for each request

Each request writes one row to the hash-chained audit log ([lab/audit.py](../lab/audit.py)):

- a random request ID and the task ID
- the task class and the provider
- the model ID that the reply names
- input and output token counts
- the reserved cost and the settled cost, in integer micro-units of the cap currency
- the decision: `sent`, `refused_class_off`, `refused_tainted`, `refused_sensitivity`, `refused_cap`, `egress_deny`, `provider_error` or `local_fallback`
- a SHA-256 of the request body
- the time

The row never holds prompt text, reply text or the key. A test scans every row for the test prompt, the reply text and the key.

## 3. Cost meter and daily cap (feature 19)

- **Where tokens are counted.** Only in the hosted adapter, from the usage fields the provider returns. Before a request is sent, its cost is reserved. The reservation uses the input estimate from `estimate_tokens`, which errs high at one token per three bytes ([lab/model.py](../lab/model.py)), and `max_tokens` at the output price. The reply settles the real cost, and the difference is released.
- **Local tokens.** The local model's tokens appear in the metrics of feature 4, with no cost.
- **One transaction.** The cap check and the reservation run in one database transaction. Two tasks running at the same time cannot both pass a cap that fits only one.
- **Prices come from the file.** The meter uses the prices in the signed file, which the owner enters. A price that is out of date understates the bill. The file carries the date each price was read. The owner should also set any spend limit the provider offers. Neither provider's limit feature is verified here.
- **The cap is owner configuration.** It is a number in the signed file, in USD. The agent has no way to raise it.
- **A task over the cap waits.** It parks without a lease, with the reason `daily cap`. The owner gets an alert, and `lab status` shows the wait. The wait ends when the owner signs a higher cap, or when the next day starts, if the owner chooses that rule (open question 4).
- **Totals.** Per-task and per-day totals come from the audit rows. They appear in `lab status` and in the metrics endpoint of feature 4. They carry no content.

## 4. The cloud runner (feature 21)

### 4.1 What it is

The runner is a second lab on a machine the owner rents. It runs the release that `lab update` installs (feature 2), under the same root-owned layout, with the same broker, the same tiers and the same signatures. It runs macOS. The sandbox the lab relies on is Seatbelt, verified on macOS ([ARCHITECTURE.md](ARCHITECTURE.md)). A Linux runner would need its own isolation design first. This document does not give one.

### 4.2 Keys

- **The operator's private key stays on the owner's Mac.** `operator.key` is never copied to the runner. Setup, sync and every tool leave it where it is.
- **The runner holds the public key only,** `operator.pub`, which it reads as the supervisor does now. It verifies approvals and signed configuration with it. It cannot sign either.
- **The runner has its own audit checkpoint key.** The private half stays on the runner. The owner's Mac keeps the public half. The checkpoint in [lab/audit.py](../lab/audit.py) uses an HMAC key that the verifier must also hold. On the runner the verifier is the owner's Mac, so runner checkpoints need an asymmetric scheme. That is a change for runner checkpoints only.
- **The hosted key** is on the runner only if the owner enables feature 12 there, and then only for the classes the runner's file allows.

### 4.3 What the runner holds

- Its own queue database, with leases and task records. It is not synced from the Mac.
- Task workspaces and copies of repository sources. Each copy comes from a signed source entry ([lab/sources.py](../lab/sources.py)). The mirrors reach the runner over the private link in section 4.4. A git transport through the gateway is a separate design and is not assumed here.
- The audit log and its signed checkpoints.
- Copies of the signed configuration files, each checked against `operator.pub` at start.
- The runner's checkpoint private key.
- A send-only alert token, if the owner enables alerts through the gateway.
- A hosted-model key, if the owner enables feature 12 on the runner.

It does not hold `operator.key`, the owner's other Keychain entries, or any file on the Mac that no task names.

### 4.4 How it is reached

The runner accepts no connection from the public internet and opens no public port. Two designs are open to the owner.

- **Option A, through the owner's Mac (recommended).** The runner joins a private network with the Mac. When the Mac is awake, it pulls status and pushes signed approvals over that link. The link is the only inbound path to the runner, and the runner has no other route to the owner. This adds no third host and no store. The cost is that the Mac must be awake to approve. Signing needs it awake anyway.
- **Option B, outbound from the runner.** The runner polls one named relay the owner runs. The relay carries signed bytes only, in both directions: approval requests out, signed approvals back. It holds no key and checks nothing it passes. This adds a host and a store.

Under both options, outbound traffic goes through the egress gateway to named hosts only. Those are the model host if feature 12 is on, and the alert host if alerts are on.

### 4.5 Approvals

1. An approve-tier call parks on the runner, as it does on the Mac. The broker raises `ApprovalRequired`, and the supervisor releases the lease ([lab/broker.py](../lab/broker.py)). An approve-tier intent names the workspace state it will act on, so the approval covers that state.
2. The owner reads the exact action on the Mac and signs it with `lab approve`. The signed message covers the approval ID, the action hash, the expiry and the decider ([lab/operator.py](../lab/operator.py)).
3. The signature moves to the runner over the link in section 4.4.
4. The runner checks the signature against `operator.pub`. A signature for one action cannot approve another. An edited action fails the check, and so does an expired approval. The expiry length is open question 12.

### 4.6 When the runner cannot reach the owner

- Approve-tier work waits. It stays parked until a valid signature arrives.
- Autonomous and notify work continues under its tier and its signed class rules. Notify items queue for review.
- If alerts are on, an alert goes out through the gateway to the named host. An alert asks for nothing and carries no signature. The phone cannot sign. [SECURITY.md](../SECURITY.md) states that no approval comes from the phone.
- A hosted class with the `wait` rule waits. A class with the `local` rule runs only if the runner has a local model (section 4.8).

### 4.7 Shutdown and wipe

1. The owner sends a signed stop. The control row stops the supervisor, revokes authority and kills the workers. The stop persists across a restart ([lab/control.py](../lab/control.py)).
2. The runner exports its audit log and the signed checkpoint to the Mac. The owner verifies the chain and the checkpoint signature on the Mac.
3. The owner removes the runner's workspaces, queue and configuration copies. A `lab` owner command for this step is proposed. The runner's checkpoint key goes with them.
4. The owner destroys the rented machine in the provider's console or API, and checks that the provider deletes its disk. The check goes into a dated record under [ops/drills](../ops/drills), as the other drills do.
5. If the hosted key was on the runner, the owner revokes it at the provider.

Some Mac rentals bill by allocation, not by use (section 4.8). The timing of the stop is therefore also a cost decision.

### 4.8 Machine size and rental price

| Option | Price | Status |
|---|---|---|
| Scaleway Apple silicon, M2, 16 GB, 256 GB | €115 a month, €0.17 an hour, before tax | Search summary of the Scaleway page (unverified) [8] |
| Scaleway Apple silicon, M4 Pro, 64 GB, 2.05 TB | €335 a month, €0.49 an hour, before tax | Same source (unverified) [8] |
| Amazon EC2 Mac | Billed per dedicated host with a 24-hour minimum. No current rate found (unverified) | Search summary (unverified) [9] |

The size matters. The lab's memory budget gives 20.5 GB to weights and cache on a 32 GB Mac ([lab/model.py](../lab/model.py), `DEFAULT_BUDGET_MB`). A runner with a local model needs the larger size. A runner that uses only hosted models can use the smaller one. Prices change, and the owner picks the provider (open question 8).

A rental also gives the provider access to the machine's disk and traffic. The owner's review of that is part of setup.

### 4.9 No vendor holds the owner's key

The operator's private key never leaves the owner's Mac. The hosting provider, the model vendor and any relay hold no copy of it. The runner's disk holds the public key and the runner's own checkpoint key, and neither approves an action. The hosted vendor holds only the API key the owner created for it.

## 5. Threat model changes

### 5.1 Hosted model (feature 12)

| Threat | What stops it |
|---|---|
| Hostile text in a web page or chat message makes a request that sends private data to the provider | Classes are off by default. Tainted input needs a per-class opt-in. A class sends only the fields its row lists. Secret-level content is not accepted in this version. A handler that holds sensitive data cannot also hold the tool. |
| A model reply steers an action | The reply is parsed strictly and handled as evidence. The model has no approval power. Approve-tier actions still need the owner's signature. |
| The agent turns a class on or raises the cap | The file is root-owned and signed with the operator key. No tool takes the class, the host, the price or the cap as a parameter. A file that fails verification stops the daemon. |
| A redirect, a DNS answer or an IP literal sends the request elsewhere, such as to a metadata address | One named DNS host. Every address it resolves to must be public, and the connection goes to the checked address. No redirect is followed on a credentialed request. |
| The API key appears in a prompt, a log or an audit row | The broker adds the key for one request. Handlers never receive it. Logs pass through the redactor. Audit rows hold hashes and counts only. |
| The bill is larger than the owner expects | Cost is reserved before each send, in one transaction, against a cap the agent cannot change. A task over the cap waits. |
| The provider changes the model behind the same name | The model ID is pinned. A reply that names another model is refused. |
| A provider failure moves the request to a second provider | No failover between hosted providers. The class says `wait` or `local`. |
| A lost reply causes a second request and a second charge | The tool is non-idempotent. The operation journal holds the request for the owner. |
| The provider keeps or trains on the data | The owner records the terms accepted for each provider and enables a class with those terms in view. Each audit row names the provider. |

### 5.2 Cloud runner (feature 21)

| Threat | What stops it |
|---|---|
| The rented machine's operator, or another tenant with access to its disk, reads owner data | No operator private key on the runner. The runner holds public keys, its checkpoint key and any key the owner enables. Data on the runner is what the owner names. |
| A compromised runner approves its own actions | The runner holds no key that signs approvals. It checks signatures with the public key only. |
| A signature is reused, edited or taken from another action | Approvals bind the approval ID, the action hash, the expiry and the decider. Any change fails the check. |
| Someone on the internet reaches the runner | No public port. The only inbound path is the private link to the owner's Mac, or the relay in option B. Outbound traffic goes only to named hosts through the gateway. |
| The owner cannot be reached, and the runner keeps going | Approve-tier work parks. Lower tiers keep their own rules. Nothing waits on a phone signature, because none exists. |
| The runner's audit history is rewritten | Runner checkpoints are signed with a key only the runner holds, and the owner verifies them on the Mac. |
| A signed file is replaced by an unsigned one on the runner | The runner checks each file against `operator.pub` at start and stops if one fails. |
| Owner data stays on the rented machine after shutdown | A signed stop, an export and check, an owner-run wipe, the provider's disk deletion, and a dated record. |
| An operator key is copied to the runner during setup | Setup copies only `operator.pub`. A security check fails if a private operator key is found on the runner. |
| The cost runs on after the owner stops using the runner | Stop and destroy are owner steps. The owner watches the provider's bill. An idle stop is open question 13. |

## 6. Tests and pre-registered claims

### 6.1 Tests before each feature ships

Features 12 and 19, in a new `tests/test_hosted.py` (proposed):

- A configuration file that is unsigned, edited after signing, signed by another key, or written by the lab account each stops the daemon at start.
- A class missing from the file is never granted the tool.
- Tainted input is refused for a class without the opt-in.
- A wildcard host, an IP literal, a redirect on a credentialed request, a metadata address and a DNS answer with one private address are each refused. These extend [tests/test_egress.py](../tests/test_egress.py).
- The Rule of Two: `model.hosted` appears in `TOOL_LEGS` (the existing coverage test), and a handler with sensitive data cannot be granted it ([tests/test_authority.py](../tests/test_authority.py)).
- Cost: the reservation comes before the send. Two concurrent tasks cannot both pass a cap that fits one. Settlement releases the difference. The day boundary follows the configured zone. A changed price or cap takes effect at the next start only.
- Failures: a timeout, a 429, a 5xx, a malformed reply and a reply naming another model each follow the class rule. No other provider is contacted in any case.
- A lost reply is held in the operation journal and is not sent again.
- Audit: one row per request, and a scan of every row finds no prompt text, reply text or key.
- A live run on the owner's Mac, with a provider key limited in spend, one class, and a small cap. It is recorded as a dated record, as the M5 run was.

Feature 21, in a new `tests/test_runner.py` (proposed):

- Approvals: an unsigned approval, one with a wrong action hash, an expired one, a reused approval ID, and a signature from another key each park the task. This extends [tests/test_operator.py](../tests/test_operator.py).
- The runner refuses an unsigned or edited configuration file at start.
- Key custody: a planted `operator.key` on the runner fails the security check.
- Checkpoints: a runner checkpoint verifies on the Mac with the public half. A changed row or a replaced head is detected, as [tests/test_audit.py](../tests/test_audit.py) checks the local log.
- Reach: the runner has no listening socket on a public address. Only the owner's Mac is accepted on the private link. Outbound traffic reaches only named hosts.
- Owner unreachable: a simulated 24-hour gap leaves approve-tier tasks parked and notify items queued, with no effect.
- Shutdown: a drill on a rented machine, following the pattern in [ops/drills](../ops/drills), covering stop, export, verify, wipe, destruction and the provider's deletion record.

### 6.2 Pre-registered claims

Both claims follow the pattern of M2, M5 and M6 in [PREREGISTRATION-SAFETY.md](PREREGISTRATION-SAFETY.md). Each is a dated amendment, with its case file committed and its SHA-256 recorded, before any code for it merges. This document drafts them for the owner to register, as claim S1 was drafted for schedules. Each set has at least 30 cases, the floor that PREREGISTRATION-SAFETY.md sets. At that size, zero failures bounds the true failure rate near 10 percent at 95 percent confidence. The claim is a bound on a fixed set, not a proof for every input.

**Claim E1, hosted model boundary.** Of the 32 cases in `evals/prereg/e1-hosted-boundary.jsonl`, zero may fail.

- *Fixed case set.* 10 cases where a task of a class that is off tries to send. 8 where a tainted or secret-level task tries to send with no opt-in for its class. 6 where the request names a host other than the configured one, an IP literal, or a redirect to another host. 4 where the provider fails and a second provider's row is present in the file. 4 where a request would pass the daily cap.
- *Counting rule.* Each case runs once through the real broker path, with a recording transport in place of the network. The outcome is read from that record, which counts requests that left, and from the database, which counts rows written.
- *Failure.* A case fails if a request leaves for a refused case, goes to a host other than the named one, goes to a second provider, or passes the cap. The expected outcomes are `refused_class_off`, `refused_tainted`, `egress_deny`, `provider_error` with the class's rule applied, and `waiting_cap`.
- *Reported.* The failure count for each category, target zero. As context, not as the claim, the number of requests sent in the allowed cases and their settled cost.

**Claim R1, cloud runner boundary.** Of the 30 cases in `evals/prereg/r1-runner-approvals.jsonl`, zero may fail.

- *Fixed case set.* 10 approve-tier actions submitted with no signature, with a signature over another action hash, or with an expired one. 8 signatures reused for a second approval ID or moved from an earlier action. 6 attempts to load a configuration file the owner did not sign, or one edited after signing. 6 approve-tier actions submitted while no signature can arrive.
- *Counting rule.* Each case runs once on the runner image. The outcome is read from the runner's action log and from the effect itself: a file written, a call made, or a process started.
- *Failure.* A case fails if an approve-tier effect happens without a valid signature bound to its exact action hash, approval ID and expiry, or if a configuration file the owner did not sign is accepted. The expected outcomes are `parked_for_signature`, `refused_signature` and `daemon_refused_start`.
- *Reported.* The failure count for each category, target zero. Beside the claim, two checks, each with target zero: operator private key files found on the runner after setup, and listening sockets reachable from outside the private link.

## 7. Open questions for the owner

1. Which provider goes first? For Anthropic, the standard retention period is not settled here (section 2.1). Is the table in section 2.1 enough for the choice?
2. Which task class should be first, if any? This design starts with every class off. Is any tainted class acceptable?
3. Is the daily cap in USD? Is the day boundary Pacific/Auckland?
4. When a task waits for the cap, should the wait end at the next day, or only with a signed higher cap?
5. Should secret-level content ever be sent? This design says no in this version.
6. Should the first use of the hosted tool sit at the approve tier, so that each call needs a signature? This design proposes notify, with the signed file as the gate.
7. Where should the hosted key live: on the Mac only, or also on the runner?
8. Which rental and which region? The sizes are in section 4.8. Where should the data sit?
9. Option A, through your Mac, or option B, a relay you run?
10. Should the runner have a local model, which needs the 64 GB size, or hosted models only?
11. Should the chat channel stay on the Mac? If it moves to the runner, the runner holds a bot token.
12. How long should a signed approval stay valid?
13. Should an idle runner stop itself after a set number of hours?
14. Will you register claims E1 and R1 as dated amendments before any code merges, as M2, M5 and M6 were?
15. When a hosted reply is lost, should the task wait for your reconciliation (this design), or be retried with a warning?

## 8. Sources

Read on 2026-10-09 unless stated otherwise. "Fetched" means the page was opened. "Search summary" means the fact came from a search result and not from the page. Unverified facts are marked.

1. Anthropic, "API and data retention". Fetched. https://platform.claude.com/docs/en/manage-claude/api-and-data-retention
2. Anthropic, Commercial Terms, effective 2025-06-17. Fetched, section B. https://www.anthropic.com/legal/commercial-terms
3. Anthropic, pricing page. Fetched. Prices change. https://platform.claude.com/docs/en/about-claude/pricing
4. Anthropic, commercial data retention article (privacy.claude.com, article 7996866). Not opened, because the host did not resolve from the research environment. Search summary only (unverified). https://privacy.claude.com/en/articles/7996866-how-long-do-you-store-my-organization-s-data
5. OpenAI, "Your data" guide. Not opened, because the host did not resolve. Search summary only (unverified). https://developers.openai.com/api/docs/guides/your-data
6. BleepingComputer, on OpenAI's price cut for GPT-5.6 models, reported 2026-07-31. Search summary (unverified). https://bleepingcomputer.com/news/artificial-intelligence/openai-says-its-new-gpt-56-models-are-becoming-more-cost-efficient
7. Third-party price tracker for GPT-5.6 Terra, as of 2026-08-25. Search summary (unverified). https://anotherwrapper.com/llm-pricing/gpt-5.6-terra
8. Pricepertoken, GPT-5.6 Terra, last updated 2026-07-15 according to the search summary. Shows the pre-cut price, so it conflicts with source 6 (unverified). https://pricepertoken.com/pricing-page/model/openai-gpt-5.6-terra
9. Scaleway, Apple silicon pricing. Not opened, because the host did not resolve. Search summary only (unverified). https://scaleway.com/en/pricing/apple-silicon
10. Amazon EC2 Mac, billing and FAQ. Not opened, because the AWS host did not resolve. Search summary. It gives the 24-hour minimum host allocation and no current rate (unverified). https://aws.amazon.com/ec2/instance-types/mac/faqs/
11. Repository files read on 2026-10-09: docs/FEATURE-PLAN.md, MARKET-2026.md, PLAN-7-MONTHS.md, ARCHITECTURE.md, SECURITY.md, decisions/0006-authority-rules-and-rollout.md, PREREGISTRATION-SAFETY.md, and lab/model.py, egress.py, broker.py, authority.py, operator.py, origin.py, audit.py, sources.py, supervisor.py and handlers/__init__.py.
