"""Observation source: turn real GitHub activity into proposals.

Issue #39, the first vertical slice ADR 0005 commits to before #32 and
#33 are built as general services. This is deliberately narrow: one real
signal source (this repo's own closed issues and merged PRs), one
dedup rule, one output shape (an ordinary queued task). Generalizing to
other signal sources is #32's job, once this slice proves the path is
real.

A proposal is not a special table. It is an ordinary task, agent_kind
"proposal", capability_tier "notify" (drafts are logged, never
autonomous and never requiring approval to *create*, only to publish).
That is the point: autonomy comes from generating work, not from a new
authority surface.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from typing import Any

from lab.origin import Origin, SourceType, content_sha256
from lab.queue import TaskQueue

log = logging.getLogger("lab.observe")


class ObservationError(RuntimeError):
    """Raised when the signal source cannot be reached or parsed."""


@dataclass(frozen=True)
class Signal:
    """One real, external thing worth possibly writing about.

    `source_url` is the dedup key: a signal is proposed at most once,
    checked against every existing proposal task's payload before a new
    one is queued.
    """

    source_type: str  # "issue" or "pull_request"
    source_url: str
    title: str
    body: str
    number: int


def _run_gh(*args: str) -> str:
    """Run `gh` and return stdout, raising ObservationError on failure.

    Subprocess rather than a GitHub API client library: `gh` is already
    the project's dependency for everything else that talks to GitHub,
    and it carries the user's existing auth, so there is no new
    credential for this to hold or leak.
    """
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    except FileNotFoundError as exc:
        raise ObservationError("gh CLI not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise ObservationError(f"gh {' '.join(args)} timed out") from exc
    except subprocess.CalledProcessError as exc:
        raise ObservationError(
            f"gh {' '.join(args)} failed: {exc.stderr.strip()}"
        ) from exc
    return result.stdout


def fetch_signals(repo: str, limit: int = 20) -> list[Signal]:
    """Real closed issues and merged PRs from `repo`, most recent first.

    Only closed issues and only merged (not just closed) PRs: an issue
    closed as not-planned or a PR closed without merging is not evidence
    of anything worth writing about.
    """
    signals: list[Signal] = []

    issues_json = _run_gh(
        "issue", "list", "--repo", repo, "--state", "closed",
        "--limit", str(limit),
        "--json", "number,title,body,url,stateReason",
    )
    for item in json.loads(issues_json):
        if item.get("stateReason") == "not_planned":
            continue
        signals.append(Signal(
            source_type="issue",
            source_url=item["url"],
            title=item["title"],
            body=item.get("body") or "",
            number=item["number"],
        ))

    prs_json = _run_gh(
        "pr", "list", "--repo", repo, "--state", "merged",
        "--limit", str(limit),
        "--json", "number,title,body,url",
    )
    for item in json.loads(prs_json):
        signals.append(Signal(
            source_type="pull_request",
            source_url=item["url"],
            title=item["title"],
            body=item.get("body") or "",
            number=item["number"],
        ))

    return signals


def _already_proposed(queue: TaskQueue, source_url: str) -> bool:
    """True if a proposal task already exists for this exact source.

    Scans by agent_kind rather than a dedicated index: proposal volume
    is small (one repo's issue/PR history), and a dedicated index is
    the kind of thing to add once #32 generalizes this past one source.
    """
    cursor = queue._conn.execute(
        "SELECT payload FROM tasks WHERE agent_kind = 'proposal'"
    )
    for (payload_json,) in cursor.fetchall():
        payload: dict[str, Any] = json.loads(payload_json)
        if payload.get("source_url") == source_url:
            return True
    return False


def observe_and_propose(queue: TaskQueue, repo: str,
                        signals: list[Signal] | None = None) -> list[str]:
    """Fetch real signals, propose the ones not already proposed.

    Returns the task_ids created. A proposal never runs autonomously:
    capability_tier "notify" means it is executed and recorded, not
    gated on approval to *create*, matching the routing/drafting steps
    that follow it. Publishing is a separate, later, approve-tier step
    this slice deliberately never reaches.
    """
    created: list[str] = []
    for signal in (signals if signals is not None else fetch_signals(repo)):
        if _already_proposed(queue, signal.source_url):
            continue
        task_id = queue.add_task(
            title=f"proposal: {signal.title}",
            payload={
                "source_type": signal.source_type,
                "source_url": signal.source_url,
                "source_title": signal.title,
                "source_body": signal.body,
                "source_number": signal.number,
            },
            agent_kind="proposal",
            capability_tier="notify",
            idempotent=True,
            origin=Origin(SourceType.EVENT, signal.source_url,
                          content_sha256(f"{signal.title}\n{signal.body}")),
        )
        log.info("proposed %s from %s", task_id, signal.source_url)
        created.append(task_id)
    return created
