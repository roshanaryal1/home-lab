"""Tests for the observation source.

`gh` is mocked throughout: these tests must not depend on network
access or the real home-lab repo's live issue/PR history, or they would
be flaky by construction and would break every time someone opens or
closes a real issue.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from lab.observe import (
    ObservationError,
    fetch_signals,
    observe_and_propose,
)
from lab.queue import TaskQueue


@pytest.fixture()
def q(tmp_path: Path) -> TaskQueue:
    with TaskQueue(tmp_path / "lab.db", owner="test-observer") as queue:
        yield queue


ONE_ISSUE = json.dumps([{
    "number": 39, "title": "First vertical slice",
    "body": "observe, propose, route, draft, stop",
    "url": "https://github.com/roshanaryal1/home-lab/issues/39",
    "stateReason": "completed",
}])
NOT_PLANNED_ISSUE = json.dumps([{
    "number": 99, "title": "wontfix",
    "body": "", "url": "https://github.com/roshanaryal1/home-lab/issues/99",
    "stateReason": "not_planned",
}])
ONE_PR = json.dumps([{
    "number": 40, "title": "ADR 0005",
    "body": "x" * 200,
    "url": "https://github.com/roshanaryal1/home-lab/pull/40",
}])
EMPTY = json.dumps([])


def _run(stdout_by_call: list[str]):
    """Mock subprocess.run returning each stdout in sequence."""
    calls = iter(stdout_by_call)

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout=next(calls), stderr="",
        )
    return fake_run


def test_fetch_signals_skips_not_planned_issues() -> None:
    with patch("lab.observe.subprocess.run",
               side_effect=_run([NOT_PLANNED_ISSUE, EMPTY])):
        signals = fetch_signals("roshanaryal1/home-lab")
    assert signals == []


def test_fetch_signals_returns_issues_and_prs() -> None:
    with patch("lab.observe.subprocess.run",
               side_effect=_run([ONE_ISSUE, ONE_PR])):
        signals = fetch_signals("roshanaryal1/home-lab")
    assert len(signals) == 2
    assert {s.source_type for s in signals} == {"issue", "pull_request"}


def test_gh_not_found_raises_observation_error() -> None:
    with (
        patch("lab.observe.subprocess.run", side_effect=FileNotFoundError),
        pytest.raises(ObservationError),
    ):
        fetch_signals("roshanaryal1/home-lab")


def test_gh_failure_raises_observation_error() -> None:
    def fake_run(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args, stderr="rate limited")
    with (
        patch("lab.observe.subprocess.run", side_effect=fake_run),
        pytest.raises(ObservationError),
    ):
        fetch_signals("roshanaryal1/home-lab")


def test_observe_and_propose_creates_tasks(q: TaskQueue) -> None:
    with patch("lab.observe.subprocess.run",
               side_effect=_run([ONE_ISSUE, ONE_PR])):
        created = observe_and_propose(q, "roshanaryal1/home-lab")
    assert len(created) == 2
    for task_id in created:
        task = q.get(task_id)
        assert task.agent_kind == "proposal"
        assert task.capability_tier == "notify"
        assert task.state == "queued"


@pytest.mark.safety
def test_observe_and_propose_is_idempotent(q: TaskQueue) -> None:
    """The same signal observed twice produces one proposal, not two.
    Without this, a poller running every N minutes would flood the
    queue with duplicate proposals for the same closed issue."""
    with patch("lab.observe.subprocess.run",
               side_effect=_run([ONE_ISSUE, EMPTY])):
        first = observe_and_propose(q, "roshanaryal1/home-lab")
    with patch("lab.observe.subprocess.run",
               side_effect=_run([ONE_ISSUE, EMPTY])):
        second = observe_and_propose(q, "roshanaryal1/home-lab")

    assert len(first) == 1
    assert len(second) == 0


def test_proposal_payload_carries_the_source(q: TaskQueue) -> None:
    with patch("lab.observe.subprocess.run",
               side_effect=_run([ONE_ISSUE, EMPTY])):
        created = observe_and_propose(q, "roshanaryal1/home-lab")
    task = q.get(created[0])
    assert task.payload["source_url"] == (
        "https://github.com/roshanaryal1/home-lab/issues/39"
    )
    assert task.payload["source_type"] == "issue"
