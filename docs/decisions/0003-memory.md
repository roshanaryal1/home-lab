# ADR 0003: memory across sessions

**Status:** recommended. **Date:** 2026-09-26.

Prompted by asking whether graphify, or something like it, should be the
memory layer for the home lab.

## What the field settled on in 2026

Production agent memory has converged on **hybrid**: dense vectors for
similarity, a graph for relationships, and plain relational storage for
operational state. Mem0 runs a three-tier hierarchy across user, session
and agent scope. Zep, built on Graphiti, is a temporally aware knowledge
graph that explicitly maintains **the temporal validity of facts**.

The local-first stacks pair SQLite for relational state, a vector store
for similarity and a graph store for relationships, with no external
services.

That is worth knowing, and mostly worth not copying yet.

## Four stores, not one

Conflating these is the common failure. They have different write rules
and different consequences when wrong.

| Store | Holds | Who may write |
|---|---|---|
| **Operational state** | tasks, leases, retries, approvals | system only, transactional |
| **Episodic log** | what happened, tool calls, decisions | append-only, never edited |
| **Curated memory** | stable preferences, projects, constraints | human review or a trusted promotion path |
| **Evidence base** | source-backed research notes | requires provenance and a retrieval date |

The first two exist already, in `lab/schema.sql`. The last two do not.

The rule that matters more than the storage choice: **a web page, an
email or another agent must never silently write curated memory.** Memory
poisoning is an agent-security problem, not a quality problem, and it is
the same boundary as the tool broker. Retrieved content is data.

## Where graphify fits, and where it does not

graphify turns a folder of files into a navigable knowledge graph with
community detection, an audit trail, typed exports and an MCP server
mode. It is a genuinely good corpus-understanding tool.

**It is not a runtime agent memory system, and should not be used as
one.** It builds a graph *from a corpus, in a batch*. Agent memory needs
incremental writes during a task, transactional consistency with the
queue, and retrieval in the middle of a loop. Those are different jobs,
and forcing one tool to do both would produce something that does
neither well.

**Where it is actually useful, and should be used:**

1. **On the research corpus.** P2 has 195 dated entities with sources;
   P3 will produce weeks of logs and incidents. Understanding those as a
   graph is exactly what graphify is for, and it runs offline, after the
   fact, without touching the runtime.
2. **On the home-lab repo itself**, for architecture questions.
3. **Possibly for the evidence base** described in `SUBSYSTEMS.md` §1,
   which wants typed edges: supports, contradicts, refines. graphify has
   typed edges and an audit trail, so this is worth evaluating rather
   than assuming. It is not a day-one dependency either way.

## Decision

**Keep SQLite plus `sqlite-vec` plus FTS for runtime memory. Do not adopt
a graph database as a runtime dependency. Use graphify as an offline
tool on corpora.**

Reasons, in order:

1. **Section 15 of the reference architecture says so**, explicitly:
   no Neo4j or other heavy graph database, and no standalone vector-DB
   cluster, before SQLite is measured as insufficient. The study's
   plurality for the vector store was `sqlite-vec`, 6 of 12.
2. **One heavy inference slot.** Memory competing with the model for
   32 GB is the wrong trade. SQLite plus `sqlite-vec` costs almost
   nothing resident.
3. **It is already there.** The queue, events and approvals are in one
   SQLite file with WAL. Memory in the same database means memory writes
   are transactional with task state, which a separate service cannot
   give without distributed-transaction problems nobody wants at 3am.
4. **Fewer things to break unattended.** Every service is a thing that
   can be down when nobody is watching.

## The one idea worth taking now

**Temporal validity.** Zep and Graphiti track when a fact was true, not
only that it was recorded. That is not a graph feature, it is a schema
decision, and it can be added to SQLite for the cost of two columns.

It matters here more than most projects: P2, *The Rater Is Stale*, is a
paper about exactly this, that a judgement made at one time decays. A
memory system for this lab that stores facts without recording when they
were true would contradict its own research programme.

So every memory row carries: source, acquisition method, **valid-from**,
**observed-at**, confidence, and a review date. That is the part of the
2026 consensus worth adopting immediately; the graph database is not.

## What would change this

- Measured evidence that SQLite plus `sqlite-vec` is insufficient for
  retrieval quality or latency. That is the bar section 15 sets, and it
  has to be measured rather than assumed.
- Multi-hop relationship queries turning out to be central rather than
  occasional, which would make a real graph store earn its keep.
- graphify proving out for the evidence base specifically, which would
  make it a component rather than a tool.
