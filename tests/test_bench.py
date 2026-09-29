"""Benchmark and tuning measurement (items 5.2 and 8.8).

Everything here runs against a scripted adapter. The numbers that matter come
from the Mac mini; what is tested is that they are measured the way the record
says they are, and that a setting is only recommended on a measured gain.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lab import bench
from lab.cli import main
from lab.model import BoundedModel, MockAdapter, ModelSpec

REV = "c" * 40
SPEC = ModelSpec("bench-model", REV, REV, context_tokens=8192, max_output_tokens=256,
                 weights_mb=100, heavy=False)


def model(reply: str = "ok " * 20, delay: float = 0.0) -> BoundedModel:
    return BoundedModel(SPEC, MockAdapter([reply], model="bench-model", delay=delay))


def test_the_report_has_cold_start_first_token_and_decode_figures() -> None:
    report = bench.run(model(delay=0.01), repeats=3, max_tokens=32)
    assert report.repeats == 3 and report.errors == 0
    assert report.cold_start_seconds >= 0.01
    assert report.first_token_p50 >= 0.01 and report.first_token_p95 >= report.first_token_p50
    assert report.decode_tokens_per_second > 0 and report.end_to_end_tokens_per_second > 0


def test_first_token_is_measured_with_a_one_token_request() -> None:
    adapter = MockAdapter(["ok"], model="bench-model")
    bench.run(BoundedModel(SPEC, adapter), repeats=2, max_tokens=16)
    limits = [call["max_tokens"] for call in adapter.calls]
    assert limits[0] == 1, "the cold request is one token"
    assert limits.count(1) == 1 + 2 and limits.count(16) == 2


def test_the_report_says_first_token_is_a_proxy_until_the_server_streams() -> None:
    assert "one-token request" in bench.run(model(), repeats=1, max_tokens=8).method


def test_a_model_error_is_counted_not_raised() -> None:
    wrong = BoundedModel(SPEC, MockAdapter(["ok"], model="another-model"))
    report = bench.run(wrong, repeats=2, max_tokens=8)
    assert report.errors > 0 and report.decode_tokens_per_second == 0


def test_repeats_must_be_at_least_one() -> None:
    with pytest.raises(bench.BenchError):
        bench.run(model(), repeats=0, max_tokens=8)


def test_server_memory_is_read_from_the_process_when_a_pid_is_given() -> None:
    import os
    assert bench.rss_mb(os.getpid()) > 0
    assert bench.rss_mb(2**22 + 7) == 0.0, "a process that does not exist reads as zero"
    assert bench.run(model(), repeats=1, max_tokens=8, server_pid=os.getpid()).server_rss_mb > 0
    assert bench.run(model(), repeats=1, max_tokens=8).server_rss_mb == 0.0


def test_a_report_is_sealed_and_records_the_model_and_the_commit(tmp_path: Path) -> None:
    report = bench.run(model(), repeats=1, max_tokens=8)
    path = bench.save(report, tmp_path)
    data = json.loads(path.read_text())
    assert data["model"]["revision"] == REV and "lab_commit" in data["provenance"]
    body = {k: v for k, v in data.items() if k != "sha256"}
    import hashlib
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    assert data["sha256"] == hashlib.sha256(canonical.encode()).hexdigest()


# ------------------------------------------------------------- tuning gate


def summary(passed: int = 24, tasks: int = 24, errors: int = 0, p95: float = 2.0,
            tps: float = 30.0) -> dict[str, float | int]:
    return {"tasks": tasks, "passed": passed, "errors": errors, "latency_p95": p95,
            "tokens_per_second": tps}


def test_a_faster_setting_with_no_loss_is_recommended() -> None:
    verdict = bench.tuning_verdict(summary(), summary(p95=1.5, tps=40.0))
    assert verdict.recommend and any("gain" in r for r in verdict.reasons)


@pytest.mark.safety
def test_a_faster_setting_that_loses_a_task_is_not_recommended() -> None:
    verdict = bench.tuning_verdict(summary(), summary(passed=23, p95=1.0, tps=60.0))
    assert not verdict.recommend and any("passed" in r for r in verdict.reasons)


def test_more_errors_or_a_gain_inside_the_noise_is_not_recommended() -> None:
    assert not bench.tuning_verdict(summary(), summary(errors=1, p95=1.0)).recommend
    small = bench.tuning_verdict(summary(), summary(p95=1.95, tps=30.5))
    assert not small.recommend and any("below" in r for r in small.reasons)


def test_too_few_tasks_or_mismatched_sets_give_no_recommendation() -> None:
    assert not bench.tuning_verdict(summary(tasks=5, passed=5), summary(tasks=5, passed=5,
                                                                          p95=0.5)).recommend
    mismatch = bench.tuning_verdict(summary(tasks=24), summary(tasks=20, passed=20, p95=0.5))
    assert not mismatch.recommend and any("same task" in r for r in mismatch.reasons)


def test_the_verdict_is_advice_and_says_a_person_decides() -> None:
    verdict = bench.tuning_verdict(summary(), summary(p95=1.0))
    assert any("person" in r for r in verdict.reasons)


def test_cli_tune_compares_two_sealed_eval_records(tmp_path: Path,
                                                   capsys: pytest.CaptureFixture[str]) -> None:
    def record(name: str, p95: float) -> Path:
        path = tmp_path / name
        path.write_text(json.dumps({"summary": summary(p95=p95, tps=30.0 * 2.0 / p95)}))
        return path

    assert main(["--db", str(tmp_path / "x.db"), "bench", "tune", str(record("a.json", 2.0)),
                 str(record("b.json", 1.0))]) == 0
    assert "recommend" in capsys.readouterr().out.lower()
    assert main(["--db", str(tmp_path / "x.db"), "bench", "tune", str(tmp_path / "none.json"),
                 str(tmp_path / "none.json")]) == 1
