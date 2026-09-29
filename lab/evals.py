"""Utility evaluations with full provenance (item 7.3, #81).

A result nobody can re-run is a claim, not evidence. This runs a fixed
task set against any OpenAI-compatible endpoint, grades each answer with
a deterministic check (no model judges a model), and writes one record
that says exactly what produced the numbers:

* the lab commit and whether the tree was dirty,
* the model name and the pinned weight and tokenizer revisions,
* Python, SQLite and library versions,
* the macOS build and power settings, on a Mac,
* the endpoint, the sampling settings and seed, and the SHA-256 of the
  task file,
* every prompt's answer, token counts, latency and verdict,

sealed with a hash of the whole record. ``rerun`` rebuilds the run from
that record alone and refuses if the task file or the commit has moved,
so a re-run is a re-run and not a different experiment with the same name.

The runner is tested against a stub endpoint. Real-model runs are parked
until the M6 and the adapter's real server exist (``ops/mac-mini-setup.md``).
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import sqlite3
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

from lab.model import (
    Adapter,
    BoundedModel,
    MalformedToolCall,
    ModelError,
    ModelSpec,
    OpenAICompatibleAdapter,
    parse_tool_call,
)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TASKS = ROOT / "evals" / "tasks.jsonl"
RECORD_VERSION = 1


class EvalError(RuntimeError):
    """A run could not be made or repeated."""


@dataclass(frozen=True)
class EvalTask:
    id: str
    prompt: str
    check: dict[str, Any]


def load_tasks(path: Path = DEFAULT_TASKS) -> tuple[list[EvalTask], str]:
    raw = Path(path).read_bytes()
    tasks = []
    for number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
            tasks.append(EvalTask(obj["id"], obj["prompt"], obj["check"]))
        except (ValueError, KeyError, TypeError):
            raise EvalError(f"{path}:{number} is not a task") from None
    ids = [t.id for t in tasks]
    if len(set(ids)) != len(ids):
        raise EvalError("task ids must be unique")
    return tasks, hashlib.sha256(raw).hexdigest()


def grade(check: dict[str, Any], answer: str) -> bool:
    """Deterministic verdicts only."""
    kind, value = check.get("type"), check.get("value")
    text = answer.strip()
    if kind == "exact":
        return text == value
    if kind == "contains":
        return isinstance(value, str) and value in text
    if kind == "not_contains":
        return isinstance(value, str) and value not in text
    if kind == "regex":
        return isinstance(value, str) and re.fullmatch(value, text, re.S) is not None
    if kind == "tool_call":
        try:
            return parse_tool_call(answer).tool == check.get("tool")
        except MalformedToolCall:
            return False
    raise EvalError(f"unknown check type {kind!r}")


def _run(cmd: list[str], cwd: Path | None = None) -> str | None:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10, cwd=cwd,
                             check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _version(dist: str) -> str | None:
    try:
        return metadata.version(dist)
    except metadata.PackageNotFoundError:
        return None


def collect_provenance() -> dict[str, Any]:
    commit = _run(["git", "rev-parse", "HEAD"], ROOT)
    dirty = bool(_run(["git", "status", "--porcelain"], ROOT))
    mac = platform.system() == "Darwin"
    power = _run(["/usr/bin/pmset", "-g", "custom"]) if mac else None
    return {
        "lab_commit": commit, "tree_dirty": dirty, "lab_version": _version("home-lab"),
        "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
        "cryptography": _version("cryptography"),
        "os": f"{platform.system()} {platform.release()}", "machine": platform.machine(),
        "macos_build": _run(["/usr/bin/sw_vers", "-buildVersion"]) if mac else None,
        "power_settings": power, "power_settings_sha256":
            hashlib.sha256(power.encode()).hexdigest() if power else None,
        "on_target": mac and platform.machine() == "arm64",
    }


@dataclass
class RunConfig:
    endpoint: str
    model: dict[str, Any]              # ModelSpec fields
    seed: int
    max_tokens: int
    timeout_seconds: float
    tasks_path: str
    tasks_sha256: str
    suite: str = "utility-v1"


@dataclass
class TaskResult:
    id: str
    passed: bool
    answer: str
    prompt_tokens: int
    completion_tokens: int
    seconds: float
    error: str | None = None


@dataclass
class RunRecord:
    record_version: int
    started_at: str
    config: RunConfig
    provenance: dict[str, Any]
    results: list[TaskResult]
    summary: dict[str, Any]
    rerun_of: str | None = None
    record_sha256: str = field(default="")

    def body(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("record_sha256")
        return data

    def seal(self) -> RunRecord:
        canonical = json.dumps(self.body(), sort_keys=True, separators=(",", ":"))
        self.record_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return self


def summarise(results: list[TaskResult], tasks: list[EvalTask] | None = None) -> dict[str, Any]:
    seconds = [r.seconds for r in results if r.error is None]
    tokens = sum(r.completion_tokens for r in results)
    total_time = sum(seconds)
    ordered = sorted(seconds)
    tool_ids = {t.id for t in tasks or [] if t.check.get("type") == "tool_call"}
    refused = 0
    for r in results:
        if r.id in tool_ids and r.error is None:
            try:
                parse_tool_call(r.answer)
            except MalformedToolCall:
                refused += 1
    return {
        "tool_call_tasks": len(tool_ids), "refused_tool_calls": refused,
        "refused_call_rate": round(refused / len(tool_ids), 4) if tool_ids else 0.0,
        "tasks": len(results), "passed": sum(r.passed for r in results),
        "errors": sum(r.error is not None for r in results),
        "completion_tokens": tokens,
        "tokens_per_second": round(tokens / total_time, 2) if total_time else None,
        "latency_p50": round(statistics.median(seconds), 4) if seconds else None,
        "latency_p95": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 4)
        if ordered else None,
    }


def run_suite(config: RunConfig, adapter: Adapter | None = None, *,
              rerun_of: str | None = None) -> RunRecord:
    tasks, sha = load_tasks(Path(config.tasks_path))
    if sha != config.tasks_sha256:
        raise EvalError("the task file has changed since this configuration was made")
    spec = ModelSpec(**config.model)
    model = BoundedModel(spec, adapter or OpenAICompatibleAdapter(config.endpoint))
    started = datetime.now(UTC).isoformat(timespec="seconds")
    results: list[TaskResult] = []
    for task in tasks:
        begin = time.monotonic()
        try:
            reply = model.generate([{"role": "user", "content": task.prompt}],
                                   max_tokens=config.max_tokens, seed=config.seed,
                                   timeout_seconds=config.timeout_seconds)
        except ModelError as exc:
            results.append(TaskResult(task.id, False, "", 0, 0, time.monotonic() - begin,
                                      f"{type(exc).__name__}: {exc}"))
            continue
        results.append(TaskResult(
            task.id, grade(task.check, reply.text), reply.text, reply.prompt_tokens,
            reply.completion_tokens, reply.seconds or (time.monotonic() - begin)))
    return RunRecord(RECORD_VERSION, started, config, collect_provenance(), results,
                     summarise(results, tasks), rerun_of).seal()


def make_config(endpoint: str, spec: ModelSpec, *, seed: int = 0, max_tokens: int = 256,
                timeout_seconds: float = 120.0, tasks_path: Path = DEFAULT_TASKS) -> RunConfig:
    _, sha = load_tasks(tasks_path)
    return RunConfig(endpoint, asdict(spec), seed, max_tokens, timeout_seconds,
                     str(tasks_path), sha)


def save(record: RunRecord, out_dir: Path) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = record.started_at.replace(":", "").replace("-", "")
    path = out_dir / f"run-{stamp}-{record.record_sha256[:8]}.json"
    path.write_text(json.dumps(asdict(record), indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    return path


def load_record(path: Path) -> RunRecord:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        record = RunRecord(
            data["record_version"], data["started_at"], RunConfig(**data["config"]),
            data["provenance"], [TaskResult(**r) for r in data["results"]], data["summary"],
            data.get("rerun_of"), data["record_sha256"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise EvalError(f"unreadable run record {path}: {exc}") from None
    claimed = record.record_sha256
    if record.seal().record_sha256 != claimed:
        raise EvalError(f"{path} does not match its own hash: it was edited")
    return record


def rerun(path: Path, adapter: Adapter | None = None, *,
          allow_different_commit: bool = False) -> tuple[RunRecord, dict[str, Any]]:
    """Repeat a run from its record alone, then compare."""
    original = load_record(path)
    now = collect_provenance()
    if not allow_different_commit and now["lab_commit"] != original.provenance["lab_commit"]:
        raise EvalError(
            f"the record was made at commit {original.provenance['lab_commit']}, this tree is "
            f"at {now['lab_commit']}; check that commit out, or pass allow_different_commit")
    repeated = run_suite(original.config, adapter, rerun_of=original.record_sha256)
    return repeated, compare(original, repeated)


def compare(a: RunRecord, b: RunRecord) -> dict[str, Any]:
    first = {r.id: r for r in a.results}
    second = {r.id: r for r in b.results}
    changed = sorted(i for i in first if i in second and (
        first[i].passed != second[i].passed or first[i].answer != second[i].answer))
    return {
        "same_tasks": a.config.tasks_sha256 == b.config.tasks_sha256,
        "same_model": a.config.model == b.config.model,
        "passed": (a.summary["passed"], b.summary["passed"]),
        "changed_tasks": changed,
        "identical_answers": not changed,
    }


def render(record: RunRecord) -> str:
    s, p = record.summary, record.provenance
    lines = [f"run {record.record_sha256[:12]}  {record.started_at}  suite {record.config.suite}",
             f"model {record.config.model['name']} @ {record.config.model['revision'][:12]}",
             f"commit {(p['lab_commit'] or '?')[:12]}{' (dirty)' if p['tree_dirty'] else ''}   "
             f"python {p['python']}  sqlite {p['sqlite']}   "
             f"{'ON TARGET' if p['on_target'] else 'not the target machine'}",
             f"passed {s['passed']}/{s['tasks']}  errors {s['errors']}  "
             f"tok/s {s['tokens_per_second']}  p50 {s['latency_p50']}s  p95 {s['latency_p95']}s"]
    lines += [f"  {'ok  ' if r.passed else 'FAIL'} {r.id}" + (f"  [{r.error}]" if r.error else "")
              for r in record.results if not r.passed]
    return "\n".join(lines)


def record_measurement(db: Path, record_sha256: str, path: str) -> None:
    """Note a saved run in the event log, so the emitter can notice if nothing cites it."""
    from lab.queue import TaskQueue
    with TaskQueue(db, owner="eval") as queue:
        queue.record_event(None, "measurement", {"name": "eval-run",
                                                 "record_sha256": record_sha256,
                                                 "path": Path(path).name})


def main(argv: list[str]) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="lab eval")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--endpoint", required=True)
    run.add_argument("--model", required=True)
    run.add_argument("--revision", required=True)
    run.add_argument("--tokenizer-revision", required=True)
    run.add_argument("--context-tokens", type=int, default=8192)
    run.add_argument("--max-output-tokens", type=int, default=1024)
    run.add_argument("--weights-mb", type=int, required=True)
    run.add_argument("--seed", type=int, default=0)
    run.add_argument("--out", type=Path, default=Path("evals/runs"))
    run.add_argument("--db", type=Path, default=None,
                     help="also record the run as a measurement event in this lab database")
    again = sub.add_parser("rerun")
    again.add_argument("record", type=Path)
    again.add_argument("--out", type=Path, default=Path("evals/runs"))
    again.add_argument("--db", type=Path, default=None)
    again.add_argument("--allow-different-commit", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "run":
            spec = ModelSpec(args.model, args.revision, args.tokenizer_revision,
                             args.context_tokens, args.max_output_tokens, args.weights_mb)
            record = run_suite(make_config(args.endpoint, spec, seed=args.seed))
        else:
            record, comparison = rerun(args.record,
                                       allow_different_commit=args.allow_different_commit)
            print(json.dumps(comparison, indent=2))
        print(render(record))
        saved = save(record, args.out)
        print(f"wrote {saved}")
        if args.db is not None:
            record_measurement(args.db, record.record_sha256, str(saved))
    except (EvalError, ValueError) as exc:
        print(f"eval: {exc}", file=sys.stderr)
        return 1
    return 0
