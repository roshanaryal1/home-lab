"""Benchmark and tuning measurement (items 5.2 and 8.8).

``run`` measures one model behind the bounded adapter: cold start, first
token latency, decode speed and the inference server's resident memory. The
figures that matter come from the Mac mini; this module makes sure they are
measured the same way every time and sealed with the model revision and the
lab commit.

The adapter is not streaming, so first token latency is the wall time of a
one-token request. That is a proxy and the report says so. Decode speed is the
rest of the time for a longer answer, divided into the tokens it produced.

``tuning_verdict`` compares two evaluation summaries from the same fixed task
set and recommends a setting only if it lost nothing and gained more than the
noise threshold. It is advice; a person changes the setting.
"""

from __future__ import annotations

import hashlib
import json
import statistics
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lab import evals
from lab.model import BoundedModel, ModelError

PROMPT = "Count from one to twenty in words, separated by commas."
METHOD = ("first token latency is the wall time of a one-token request (the adapter does not "
          "stream); decode speed is completion tokens over the extra time a longer answer takes")


class BenchError(ValueError):
    pass


@dataclass
class BenchReport:
    started_at: str
    model: dict[str, Any]
    repeats: int
    max_tokens: int
    method: str = METHOD
    cold_start_seconds: float = 0.0
    first_token_p50: float = 0.0
    first_token_p95: float = 0.0
    decode_tokens_per_second: float = 0.0
    end_to_end_tokens_per_second: float = 0.0
    server_rss_mb: float = 0.0
    errors: int = 0
    provenance: dict[str, Any] = field(default_factory=dict)


def rss_mb(pid: int) -> float:
    """Resident memory of one process in MB, 0 if it is gone."""
    try:
        # A fixed argv and an integer pid, no shell.
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(int(pid))], capture_output=True,
                             text=True, timeout=5, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0.0
    return int(out) / 1024 if out.isdigit() else 0.0


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))] if ordered else 0.0


def run(model: BoundedModel, *, repeats: int = 5, max_tokens: int = 128,
        server_pid: int | None = None) -> BenchReport:
    if repeats < 1:
        raise BenchError("repeats must be at least 1")
    report = BenchReport(datetime.now(UTC).isoformat(timespec="seconds"), asdict(model.spec),
                         repeats, max_tokens, provenance=evals.collect_provenance())
    messages = [{"role": "user", "content": PROMPT}]

    def timed(limit: int) -> tuple[float, int] | None:
        begin = time.monotonic()
        try:
            reply = model.generate(messages, max_tokens=limit, seed=0)
        except ModelError:
            report.errors += 1
            return None
        return reply.seconds or (time.monotonic() - begin), reply.completion_tokens

    cold = timed(1)
    report.cold_start_seconds = cold[0] if cold else 0.0
    first = [r[0] for r in (timed(1) for _ in range(repeats)) if r]
    long = [r for r in (timed(max_tokens) for _ in range(repeats)) if r]
    if first:
        report.first_token_p50 = statistics.median(first)
        report.first_token_p95 = _p95(first)
    if long:
        tokens = sum(t for _, t in long)
        total = sum(s for s, _ in long)
        extra = sum(max(s - report.first_token_p50, 0.0) for s, _ in long)
        report.end_to_end_tokens_per_second = round(tokens / total, 2) if total else 0.0
        report.decode_tokens_per_second = round(tokens / extra, 2) if extra else \
            report.end_to_end_tokens_per_second
    if server_pid is not None:
        report.server_rss_mb = round(rss_mb(server_pid), 1)
    return report


def render(report: BenchReport) -> str:
    return "\n".join([
        f"model    {report.model['name']} @ {report.model['revision'][:12]}",
        f"cold start          {report.cold_start_seconds:8.3f} s",
        f"first token p50/p95 {report.first_token_p50:8.3f} / {report.first_token_p95:.3f} s",
        f"decode              {report.decode_tokens_per_second:8.2f} tok/s",
        f"end to end          {report.end_to_end_tokens_per_second:8.2f} tok/s",
        f"server resident     {report.server_rss_mb:8.1f} MB",
        f"errors              {report.errors:8d}",
        f"note: {report.method}"])


def save(report: BenchReport, out_dir: Path) -> Path:
    body = asdict(report)
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    body["sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = report.started_at.replace(":", "").replace("-", "")
    path = out_dir / f"bench-{stamp}-{body['sha256'][:8]}.json"
    path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


# ------------------------------------------------------------- tuning gate


@dataclass(frozen=True)
class Verdict:
    recommend: bool
    reasons: list[str]


def tuning_verdict(base: dict[str, Any], candidate: dict[str, Any], *, min_gain: float = 0.10,
                   min_tasks: int = 20) -> Verdict:
    reasons: list[str] = []
    if base["tasks"] != candidate["tasks"]:
        reasons.append("not the same task set; compare runs of one fixed set")
    elif base["tasks"] < min_tasks:
        reasons.append(f"only {base['tasks']} tasks, at least {min_tasks} needed")
    if candidate["passed"] < base["passed"]:
        reasons.append(f"passed {candidate['passed']} against {base['passed']}: quality was lost")
    if candidate["errors"] > base["errors"]:
        reasons.append(f"{candidate['errors']} errors against {base['errors']}")
    latency_gain = 0.0
    if base.get("latency_p95") and candidate.get("latency_p95"):
        latency_gain = (base["latency_p95"] - candidate["latency_p95"]) / base["latency_p95"]
    speed_gain = 0.0
    if base.get("tokens_per_second") and candidate.get("tokens_per_second"):
        speed_gain = (candidate["tokens_per_second"] - base["tokens_per_second"]) \
            / base["tokens_per_second"]
    best = max(latency_gain, speed_gain)
    if not reasons and best < min_gain:
        reasons.append(f"best gain {best:+.1%} is below the required {min_gain:.0%}")
    recommend = not reasons
    if recommend:
        reasons.append(f"gain: p95 latency {latency_gain:+.1%}, tokens/s {speed_gain:+.1%}, "
                       "no task lost")
    reasons.append("this is advice only: a person changes the setting")
    return Verdict(recommend, reasons)


def _summary_of(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    summary = data.get("summary")
    if not isinstance(summary, dict):
        raise BenchError(f"{path.name} is not an evaluation record")
    return summary


def main(argv: list[str]) -> int:
    import argparse
    import sys
    parser = argparse.ArgumentParser(prog="lab bench")
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="cold start, first token, decode speed and server memory")
    r.add_argument("--endpoint", required=True)
    r.add_argument("--model", required=True)
    r.add_argument("--revision", required=True)
    r.add_argument("--tokenizer-revision", required=True)
    r.add_argument("--weights-mb", type=int, required=True)
    r.add_argument("--repeats", type=int, default=5)
    r.add_argument("--max-tokens", type=int, default=128)
    r.add_argument("--server-pid", type=int, default=None)
    r.add_argument("--out", type=Path, default=Path("evals/bench"))
    t = sub.add_parser("tune", help="compare two evaluation records from the same task set")
    t.add_argument("baseline", type=Path)
    t.add_argument("candidate", type=Path)
    t.add_argument("--min-gain", type=float, default=0.10)
    args = parser.parse_args(argv)
    try:
        if args.cmd == "run":
            from lab.model import ModelSpec, OpenAICompatibleAdapter
            spec = ModelSpec(args.model, args.revision, args.tokenizer_revision, 8192,
                             max(args.max_tokens, 1), args.weights_mb)
            bounded = BoundedModel(spec, OpenAICompatibleAdapter(args.endpoint))
            report = run(bounded, repeats=args.repeats, max_tokens=args.max_tokens,
                         server_pid=args.server_pid)
            print(render(report))
            print(f"wrote {save(report, args.out)}")
        else:
            verdict = tuning_verdict(_summary_of(args.baseline), _summary_of(args.candidate),
                                     min_gain=args.min_gain)
            print("recommend" if verdict.recommend else "do not recommend")
            for reason in verdict.reasons:
                print(f"  - {reason}")
    except (BenchError, ValueError, OSError, KeyError) as exc:
        print(f"bench: {exc}", file=sys.stderr)
        return 1
    return 0
