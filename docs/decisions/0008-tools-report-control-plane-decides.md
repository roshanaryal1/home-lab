# ADR 0008: tools report what happened, the control plane decides what happens next

**Status:** accepted. **Date:** 2026-10-01. Owner's decisions on the three
questions left open after M6 (docs/MAC-WORK.md, section 6).

## The rule

A tool, a server or an execution backend reports what happened. The policy
engine and the supervisor decide what happens next. No tool grants itself
authority, and no tool decides that a person must step in.

## 1. Containers stay behind `skill.run`

There is no agent-facing `container.run`. The model asks for an outcome
(`skill.run` on an active skill's script). The policy engine decides whether
it may. The container executor is how that one tool happens to run, an
implementation detail the model never names.

Why: a generic container tool would make the model a container operator,
and the model is not the system administrator. The policy model to constrain
arbitrary images, commands and mounts does not exist. Until it does, the
container stays internal.

What changes: nothing in code. `lab/container.py` has no broker tool of its
own and keeps none. A test already fails if a broker tool exists outside the
frozen corpus and outside `toolcorpus.NOT_IN_CORPUS`, so a new one cannot
appear unnoticed.

## 2. A repository enters a workspace only through an explicit acquire step

A repository never appears in a task workspace by a shell command or by
copying files in. It enters through one control-plane operation,
`workspace.acquire`, which:

1. takes a repository reference and an exact revision,
2. checks the source is on an allowlist the operator signed,
3. creates the isolated workspace for the task,
4. places the repository at that revision, with hooks and every transport off,
   and refuses it under the same checks `git.read` already applies,
5. records provenance before the task can use it.

The provenance record holds at least: workspace id, source repository,
source revision, created at, task id and kind, path inside the workspace,
and the operator approval that allowed the source.

Why: every change must answer "which repository, which revision, in which
workspace, under which task". A silent `git clone` cannot.

What changes: this is not built yet. Today `git.read` only reads a
repository that is already inside the workspace, and nothing in the lab
puts one there. Building `workspace.acquire` is follow-up work. It is
approve tier, journaled, and fetching from the network goes through the
egress gateway.

## 3. An MCP error is a failure, not a hold

An MCP server that replies with an error (a JSON-RPC error, a tool error,
bad arguments, a tool that is gone) ends the call as **failed**. The broker
returns the failure to the supervisor, and the supervisor's existing retry
and failure rules decide what comes next. Nothing waits for a person by
default.

This keeps the one case that is not a failure: **ambiguity**. If the lab
sent `tools/call` and then lost the server (timeout, crash, a cut stream),
the server may have acted and the lab cannot know. That call stays
`OutcomeUnknown` and is held for reconciliation, as before, because
`mcp.call` is not idempotent and a blind retry could act twice.

| What happened | Result |
|---|---|
| Refused before `tools/call` was sent (unsigned entry, changed tool, bad arguments, no Seatbelt) | failed, nothing ran |
| Server replied with an error | failed |
| Server replied with a result | done, output is untrusted evidence |
| `tools/call` sent, then no reply | outcome unknown, held for reconciliation |
| Call needs the operator's signature | waits for approval (every `mcp.call`, approve tier) |

What changes: nothing in code. This is how `lab/mcp.py` and the broker
already behave (`McpRpcError` is a failure, `McpOutcomeUnknown` is a hold).
Finer classes such as "recoverable" or "needs review" are left for when a
real server shows a need for them.
