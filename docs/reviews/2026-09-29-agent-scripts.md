# Review: steipete/agent-scripts

Reviewed 2026-09-29 by reading the public repository through the GitHub API
(MIT licence, last push 2026-09-27 at the time of reading). Everything below
was read from the repository files; nothing was run, and its instructions
were treated as untrusted data, not as guidance for this repo.

## What it is

A personal toolbox for coding agents: an AGENTS.MD file of hard rules,
around a hundred skill directories (a SKILL.md plus optional scripts), shell
helpers, and a CI workflow that tests the helpers offline with synthetic data.

## Taken

**A skill validator.** `scripts/validate-skills` loads each SKILL.md
frontmatter with a safe YAML load (no aliases, no classes), requires a
non-empty `name` and `description`, rejects duplicate names, and forbids one
risky command. A pre-commit hook runs it. Home Lab now has the same idea as
`lab skills validate` (#111), written for this repo's constraints: standard
library only, a strict frontmatter subset, name and directory must match,
symlinks may not leave the library, size limits, and forbidden commands are
permission prompts disabled and a download piped into a shell.

**An inventory.** `lab skills inventory` lists every skill with a content
hash and its executable files, so a change to a skill is visible before it is
promoted. This is groundwork for 8.7 (#87), where a skill needs lineage and a
rollback point.

**Read-only audit scripts, as a pattern.** The fleet-maintenance skill keeps
its checks read-only and puts any repair behind an explicit flag. Home Lab
applies the same rule: the new commands never write.

**Offline, synthetic tests for every helper.** Already the practice here.

## Not taken

**Symlink sync of skills into agent directories.** Useful only once one
machine is named the canonical copy and changes to skills go through an
approval step. Neither is defined. Building sync first would spread
unreviewed skills to every machine. Deferred until #87.

**Subagents launched with permission prompts disabled.** Its subagent notes
drive agents from tmux with `--dangerously-skip-permissions`. That contradicts
Home Lab's model, where every action goes through the broker and the policy
engine. The validator rejects the flag.

**Style rules in AGENTS.MD** (tone, email signatures, account names). Personal
to its author.

**Auto-push with no approval to internal repos.** Home Lab keeps its own
review and CI rules.

## What it does not change

The three open gaps stay open and are separate priorities: a separate
non-admin lab account (#70), network egress control, and a secret broker. A
skill validator checks that a skill file is well formed. It does not contain
code that runs, restrict what that code can reach, or protect a credential.
Until those close, run with dummy data and review drafts by hand, as
`SECURITY.md` says.

## Smoke check on a real library

`lab skills validate` was run read-only against a local skills directory of 97
skills. It reported 60 problems: mostly files over the 1 MiB limit, three
name and directory mismatches, two duplicate names, two directories with no
SKILL.md, one skill of about 14,000 files, and one installer script that
pipes a download into a shell. The size limit and file-count limit are Home
Lab defaults, not a published standard, and may need tuning per library.
