# ADR 0002: the Python runtime on the deployment target

**Status:** recommended, pending Roshan's approval. **Date:** 2026-09-26.

## What prompted this

The mini was checked on 2026-09-26 and reports **Python 3.9.6 as the only
interpreter on the machine**, at `/usr/bin/python3`, the macOS system
Python. No Homebrew Python, no framework install, no pyenv, no uv.
Homebrew itself is present.

3.9.6 cannot run this project. `typing.Self`, `datetime.UTC` and
`StrEnum` are all 3.11 or later, and the project targets 3.13.

So the 24/7 deployment target currently has **no interpreter capable of
running the lab at all**. That is a real finding about fresh macOS 27
rather than an oversight.

## Why this is not a trivial choice

The question is not "how do I get Python 3.13". It is **what absolute
path will a launchd job dereference at 3am in eight months**, and
**can this machine be rebuilt to the same state**.

Those two requirements eliminate the obvious answer.

## Candidates

### Homebrew, `brew install python@3.13`

**Rejected.**

Homebrew's Python exists primarily to serve other Homebrew packages. A
routine `brew upgrade`, run for some entirely unrelated formula, can
replace it without warning and take pip-installed packages and virtual
environments with it. It also cannot pin to a specific patch version such
as 3.13.4, which rules it out for reproducible builds.

For an always-on service this is the worst property available: the
failure arrives unannounced, at whatever hour the upgrade ran, triggered
by a change to something unrelated.

### python.org installer

**Viable fallback.**

Installs to `/Library/Frameworks/Python.framework/Versions/3.13/`, a
versioned path that does not move. Stable and simple. Weaknesses: manual
updates, requires admin rights, and pinning an exact patch release means
tracking installer downloads by hand.

### uv-managed Python

**Recommended.**

uv fetches prebuilt CPython from python-build-standalone into its own
directory. It does not need admin rights, does not replace the system
Python, and does not touch or conflict with Homebrew's. The version is
**pinnable to an exact patch release**, which is what makes the machine
rebuildable and the paper's environment reproducible.

Its one operational catch, worth writing down because it is not obvious:
**the list of Python versions uv can install is frozen at each uv
release**, so a stale uv installs a stale Python. Keeping uv current is
therefore part of maintaining the machine, not an optional nicety.

## Decision

**uv-managed Python, pinned to an exact patch version.**

1. Reproducibility. P3 is a paper about building and measuring this
   system. "Rebuild the machine and get the same environment" is a
   requirement, not a convenience.
2. Isolation. Nothing an unrelated `brew upgrade` does can move the
   interpreter a launchd job depends on.
3. No admin rights needed, which matters once the lab runs under a
   dedicated non-admin account.
4. `uv sync` recreates the environment exactly, including on a rebuild.

Record the exact version in the repo so the pin is auditable, and treat
keeping uv itself current as a maintenance task.

**Amendment, 2026-09-26, found on the deployment target.** `uv python
pin 3.13` writes the **minor** version, not the patch: `.python-version`
contains literally `3.13`. That pins nothing about the patch release, so
a rebuild resolves to whatever 3.13.x the uv of the day ships, which is
precisely the rebuildability property this ADR claimed and would not
have delivered. The pin must name the patch, `uv python pin 3.13.15`.
Recorded rather than quietly corrected, because the ADR asserted a
property the command did not provide.

## What would change this

- uv proving unreliable in practice, in which case the python.org
  framework install is the fallback and the reasoning above still holds
  against Homebrew.
- A requirement to share one interpreter with other Homebrew tooling,
  which does not currently exist.

## Related risk, not part of this decision

The mini reports **macOS 27.0**. The Seatbelt process isolation in
`lab/sandbox.py` (issue #17) was verified on macOS 26.5.1 on the
MacBook, not on 27. `sandbox-exec` is deprecated, so a major OS version
is exactly where it could change behaviour or disappear.

**That control is currently unverified on the deployment target.** Running
`pytest tests/test_sandbox.py` on the mini is the check, and it should
happen before anything executes code there.
