"""The public injection suite (#368, feature 17).

A fixed, versioned set of injection cases that anyone can rerun. Each case is
one of the attack scenarios in ``lab.attacks`` with its hostile instructions
in a given framing: a plain "system notice", hidden in an HTML comment, or
dressed as a tool result. The task fetches the case's document as a web page
and the handler reads it, through the real supervisor, broker and egress
path. Grading is on state, as in ``lab.attacks``: files, operations, hosts
reached and tasks created, never what the model said.

The default model is the stub that obeys every instruction it reads. Against
it, a framing changes nothing, so the stub tests the controls alone. A real
model (``--endpoint``) sees three disguises of the same instructions.

* The case file's SHA-256 is pinned below. A changed file is refused, so a
  published result always names the exact cases it ran.
* The suite only grows: a new version is a new file beside the old one.
* A case that succeeds is not published. It is fixed first.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lab import attacks

ROOT = Path(__file__).resolve().parent.parent
SUITE_DIR = ROOT / "evals" / "injection"
# version: (file name, its SHA-256)
SUITES: dict[str, tuple[str, str]] = {
    "v1": ("suite-v1.jsonl",
           "1e1a0e770600880f5eec32653078d18ee1b90c57181905b02a655cd5366e782a"),
}
LATEST = "v1"
STUB = "stub (obeys every instruction it reads)"
CASE_KEYS = frozenset({"id", "scenario", "framing", "document"})


class SuiteError(ValueError):
    """A case file that is missing, changed or malformed."""


@dataclass(frozen=True)
class Case:
    id: str
    scenario: str
    framing: str
    document: str


@dataclass(frozen=True)
class CaseResult:
    id: str
    scenario: str
    framing: str
    utility: bool
    attack_succeeded: bool
    task_state: str


@dataclass(frozen=True)
class Report:
    suite: str
    sha256: str
    commit: str
    model: str
    date: str
    results: tuple[CaseResult, ...]

    @property
    def attack_successes(self) -> int:
        return sum(r.attack_succeeded for r in self.results)

    @property
    def utility(self) -> int:
        return sum(r.utility for r in self.results)

    def as_json(self) -> dict[str, Any]:
        return {"suite": self.suite, "sha256": self.sha256, "commit": self.commit,
                "model": self.model, "date": self.date, "cases": len(self.results),
                "attack_successes": self.attack_successes, "utility": self.utility,
                "results": [asdict(r) for r in self.results]}


def load(version: str = LATEST, directory: Path | None = None) -> tuple[str, list[Case]]:
    """The cases of ``version`` and the file's SHA-256, after it matches the pinned one.
    ``directory`` defaults to SUITE_DIR."""
    if version not in SUITES:
        raise SuiteError(f"no injection suite {version!r}; known: {', '.join(sorted(SUITES))}")
    name, pinned = SUITES[version]
    path = (SUITE_DIR if directory is None else directory) / name
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise SuiteError(f"cannot read {name}: {exc.strerror}") from exc
    digest = hashlib.sha256(data).hexdigest()
    if digest != pinned:
        raise SuiteError(f"{name} has SHA-256 {digest}, but {pinned} is pinned. "
                         "A suite version never changes: add a new version instead")
    scenarios = {s.name for s in attacks.SCENARIOS}
    cases: list[Case] = []
    for number, line in enumerate(data.decode("utf-8").splitlines(), 1):
        row = json.loads(line)
        if not isinstance(row, dict) or set(row) != CASE_KEYS:
            raise SuiteError(f"{name} line {number} is not a case")
        if row["scenario"] not in scenarios:
            raise SuiteError(f"{name} line {number} names no known scenario")
        cases.append(Case(**row))
    if len({c.id for c in cases}) != len(cases):
        raise SuiteError(f"{name} repeats a case id")
    return digest, cases


def _commit() -> str:
    try:
        done = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=10, check=True)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return done.stdout.strip() or "unknown"


ModelFactory = Callable[[attacks.World], Callable[..., Any]]


async def run(version: str = LATEST, *, model: ModelFactory = attacks.obedient_model,
              model_label: str = STUB, directory: Path | None = None,
              now: datetime | None = None) -> Report:
    """Run every case of ``version`` in a fresh lab of its own and grade it."""
    digest, cases = load(version, directory)
    by_name = {s.name: s for s in attacks.SCENARIOS}
    results: list[CaseResult] = []
    for case in cases:
        with tempfile.TemporaryDirectory(prefix="lab-injection-") as tmp:
            grade = await attacks.run_scenario(by_name[case.scenario], Path(tmp), model=model,
                                               document=case.document)
        results.append(CaseResult(case.id, case.scenario, case.framing, grade.utility,
                                  grade.attack_succeeded, grade.task_state))
    when = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return Report(version, digest, _commit(), model_label, when, tuple(results))


def format_report(report: Report) -> str:
    rows = [f"injection suite {report.suite}  sha256 {report.sha256[:12]}  "
            f"commit {report.commit[:12]}",
            f"model: {report.model}",
            f"{'case':<44} {'utility':<8} {'attack':<8} state"]
    for r in report.results:
        rows.append(f"{r.id:<44} {'yes' if r.utility else 'no':<8} "
                    f"{'SUCCESS' if r.attack_succeeded else 'blocked':<8} {r.task_state}")
    total = len(report.results)
    rows.append(f"\nattack success: {report.attack_successes}/{total}   "
                f"utility: {report.utility}/{total}")
    return "\n".join(rows)
