"""Measuring reviewed handlers to size the per-task ceilings (#180).

The real numbers come from the Mac mini. These tests make sure the command
measures every handler that would run in a worker, suggests ceilings with the
stated headroom, and reports a breach rather than falling over.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lab import ceilings, handlers
from lab.cli import main
from lab.handlers import git_read, web, workspace
from lab.supervisor import Supervisor, SupervisorConfig
from lab.worker import _usage_of, own_usage


def registered_in_workers(tmp_path: Path) -> set[str]:
    """Every kind register_all puts in a worker, with a model and hosts configured."""
    spec = ceilings.load_tasks(ceilings.DEFAULT_TASKS)
    seen: set[str] = set()
    with ceilings.measurement_environment(1, spec.get("web_hosts", [])):
        sup = Supervisor(SupervisorConfig(db_path=tmp_path / "probe.db"))
        original = sup.register_reviewed

        def recording(kind: str, ref: str, *args: Any, **kw: Any) -> None:
            seen.add(kind)
            original(kind, ref, *args, **kw)

        setattr(sup, "register_reviewed", recording)  # noqa: B010
        handlers.register_all(sup)
        sup.close()
    return seen


def only_report(out: Path) -> dict[str, Any]:
    (path,) = out.glob("ceilings-*.json")
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def demo_tasks(tmp_path: Path, handlers_spec: dict[str, Any]) -> Path:
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps({"version": 1, "handlers": handlers_spec}), encoding="utf-8")
    return path


NOTE = {"tools": ["fs.write", "fs.read"],
        "samples": [{"name": "note", "payload": {"note": "hello"}}]}


def test_the_command_measures_every_registered_handler(tmp_path: Path, capsys) -> None:
    out = tmp_path / "out"
    assert main(["measure-ceilings", "--repeats", "2", "--out", str(out)]) == 0
    report = only_report(out)
    expected = registered_in_workers(tmp_path)
    assert {workspace.KIND, git_read.KIND, web.KIND} <= expected
    assert set(report["handlers"]) == expected
    spec = ceilings.load_tasks(ceilings.DEFAULT_TASKS)
    for kind, handler in report["handlers"].items():
        samples = spec["handlers"][kind]["samples"]
        assert len(handler["runs"]) == 2 * len(samples)
        assert all(r["state"] == "succeeded" for r in handler["runs"]), handler["runs"]
        assert handler["peak_rss_mb"] > 0 and handler["peak_cpu_seconds"] > 0
        assert handler["peak_rss_mb"] == max(r["peak_rss_mb"] for r in handler["runs"])
        assert handler["suggested_rss_mb"] >= handler["peak_rss_mb"] * 2
        assert handler["suggested_cpu_seconds"] >= handler["peak_cpu_seconds"] * 2
    peak_rss = max(h["peak_rss_mb"] for h in report["handlers"].values())
    peak_cpu = max(h["peak_cpu_seconds"] for h in report["handlers"].values())
    assert report["suggested"]["task_max_rss_mb"] >= peak_rss * 2
    assert report["suggested"]["task_max_cpu_seconds"] >= peak_cpu * 2
    assert report["problems"] == [] and report["headroom"] == 2.0
    for key in ("machine", "os", "python", "lab_commit"):
        assert key in report["provenance"]
    assert report["current_defaults"] == {"task_max_rss_mb": 2048, "task_max_cpu_seconds": 900.0}
    assert len(report["sha256"]) == 64
    printed = capsys.readouterr().out
    assert all(kind in printed for kind in expected) and "suggested: task_max_rss_mb=" in printed


def test_every_handler_register_all_registers_has_sample_tasks(tmp_path: Path) -> None:
    """A new reviewed handler cannot ship without samples to size its ceiling."""
    spec = ceilings.load_tasks(ceilings.DEFAULT_TASKS)
    assert registered_in_workers(tmp_path) <= set(spec["handlers"])


def test_the_suggestion_is_the_peak_times_the_stated_headroom(tmp_path: Path) -> None:
    report = ceilings.measure(demo_tasks(tmp_path, {"demo.note": NOTE}), repeats=2,
                              headroom=3.0, include_registered=False,
                              extra=[("demo.note", "lab.handlers.demo:write_note")])
    handler = report.handlers["demo.note"]
    assert handler.source == "--handler" and len(handler.runs) == 2
    assert handler.peak_rss_mb and handler.peak_cpu_seconds
    assert report.suggested["task_max_rss_mb"] >= handler.peak_rss_mb * 3
    assert report.suggested["task_max_cpu_seconds"] >= handler.peak_cpu_seconds * 3
    assert "3x" in ceilings.render(report)


def test_a_handler_over_a_tiny_ceiling_is_reported_not_crashed(tmp_path: Path) -> None:
    tasks = demo_tasks(tmp_path, {
        "demo.note": NOTE,
        "demo.hog": {"samples": [{"name": "hold", "payload": {"mb": 300, "seconds": 20}}]},
    })
    out = tmp_path / "out"
    code = main(["measure-ceilings", "--tasks", str(tasks), "--only-named", "--repeats", "1",
                 "--max-rss-mb", "64", "--out", str(out),
                 "--handler", "demo.note=lab.handlers.demo:write_note",
                 "--handler", "demo.hog=lab.handlers.demo:hold_memory"])
    assert code == 1
    report = only_report(out)
    (run,) = report["handlers"]["demo.hog"]["runs"]
    assert run["state"] == "failed" and run["exceeded"]["resource"] == "memory"
    assert run["exceeded"]["observed_mb"] > 64
    assert report["handlers"]["demo.hog"]["suggested_rss_mb"] is None
    assert any("demo.hog" in p and "memory ceiling" in p for p in report["problems"])
    assert report["suggested"]["task_max_rss_mb"] is None
    # The well-behaved handler beside it is still measured.
    assert report["handlers"]["demo.note"]["suggested_rss_mb"] is not None
    assert report["measured_under"]["task_max_rss_mb"] == 64


def test_a_handler_over_a_tiny_cpu_ceiling_is_reported(tmp_path: Path) -> None:
    tasks = demo_tasks(tmp_path, {
        "demo.spin": {"samples": [{"name": "spin", "payload": {"seconds": 20}}]}})
    report = ceilings.measure(tasks, repeats=1, include_registered=False, max_cpu_seconds=1,
                              extra=[("demo.spin", "lab.handlers.demo:spin_cpu")])
    (run,) = report.handlers["demo.spin"].runs
    assert run.exceeded is not None and run.exceeded["resource"] == "cpu"
    assert report.problems and report.suggested["task_max_cpu_seconds"] is None


def test_a_failing_sample_and_a_handler_without_samples_are_problems(tmp_path: Path,
                                                                    capsys) -> None:
    tasks = demo_tasks(tmp_path, {
        "demo.boom": {"samples": [{"name": "boom", "payload": {}}]}})
    out = tmp_path / "out"
    code = main(["measure-ceilings", "--tasks", str(tasks), "--only-named", "--repeats", "1",
                 "--out", str(out), "--handler", "demo.boom=lab.handlers.demo:explode",
                 "--handler", "demo.none=lab.handlers.demo:write_note"])
    assert code == 1
    report = only_report(out)
    (run,) = report["handlers"]["demo.boom"]["runs"]
    assert run["state"] == "failed" and "exploded" in run["error"]
    assert run["peak_rss_mb"] > 0, "a handler that raises still reports its usage"
    assert any("no sample tasks" in p for p in report["handlers"]["demo.none"]["problems"])
    assert "problem: demo.none" in capsys.readouterr().out


def test_nothing_registered_is_a_problem_not_a_suggestion(tmp_path: Path) -> None:
    report = ceilings.measure(demo_tasks(tmp_path, {"demo.note": NOTE}), repeats=1,
                              include_registered=False)
    assert report.handlers == {} and report.problems
    assert report.suggested["task_max_rss_mb"] is None


@pytest.mark.parametrize("bad", [
    {}, {"handlers": []}, {"handlers": {"k": {"samples": []}}},
    {"handlers": {"k": {"samples": [{"payload": {}}]}}},
    {"handlers": {"k": {"samples": [{"name": "a", "payload": {}}, {"name": "a", "payload": {}}]}}},
    {"handlers": {"k": {"samples": [{"name": "a", "payload": "x"}]}}},
])
def test_a_malformed_task_file_is_refused(tmp_path: Path, bad: dict[str, Any]) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ceilings.CeilingsError):
        ceilings.load_tasks(path)


def test_bad_arguments_exit_one_with_a_message(tmp_path: Path, capsys) -> None:
    assert main(["measure-ceilings", "--handler", "x=os:system", "--out", str(tmp_path)]) == 1
    assert "not a reviewed handler" in capsys.readouterr().err
    assert main(["measure-ceilings", "--handler", "nokind", "--out", str(tmp_path)]) == 1
    assert main(["measure-ceilings", "--repeats", "0", "--out", str(tmp_path)]) == 1
    assert main(["measure-ceilings", "--headroom", "0.5", "--out", str(tmp_path)]) == 1
    assert list(tmp_path.glob("ceilings-*.json")) == []


def test_suggest_rounds_up_and_keeps_a_floor() -> None:
    assert ceilings.suggest(44.1, 2.0) == 89
    assert ceilings.suggest(0.21, 2.0) == 1
    assert ceilings.suggest(10.0, 2.0) == 20


def test_the_worker_reports_its_own_usage_and_garbage_is_ignored() -> None:
    usage = own_usage()
    assert set(usage) == {"peak_rss_mb", "cpu_seconds"} and usage["peak_rss_mb"] > 1
    assert _usage_of({"usage": usage}) == usage
    assert _usage_of({}) is None
    assert _usage_of({"usage": "x"}) is None
    assert _usage_of({"usage": {"peak_rss_mb": True, "cpu_seconds": 1}}) is None
    assert _usage_of({"usage": {"peak_rss_mb": 1}}) is None


def test_the_measurement_environment_is_restored(monkeypatch) -> None:
    monkeypatch.setenv("LAB_MODEL_URL", "http://127.0.0.1:8080/v1")
    monkeypatch.delenv("LAB_WEB_FETCH_HOSTS", raising=False)
    import os
    with ceilings.measurement_environment(9, ["docs.example.org"]):
        assert os.environ["LAB_MODEL_URL"] == "http://127.0.0.1:9/v1"
        assert os.environ["LAB_WEB_FETCH_HOSTS"] == "docs.example.org"
    assert os.environ["LAB_MODEL_URL"] == "http://127.0.0.1:8080/v1"
    assert "LAB_WEB_FETCH_HOSTS" not in os.environ


@pytest.mark.parametrize("setup", [
    {"files": {"/tmp/escape.txt": "x"}},
    {"files": {"../escape.txt": "x"}},
    {"files": {"a/../../escape.txt": "x"}},
    {"files": {"": "x"}},
    {"generate": {"dir": "../out", "count": 1}},
    {"generate": {"dir": "/tmp/out", "count": 1}},
    {"git": {"dir": "../repo", "commits": 1}},
])
def test_a_sample_cannot_write_outside_its_workspace(tmp_path: Path, setup: dict[str, Any]) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    with pytest.raises(ceilings.CeilingsError):
        ceilings.seed_workspace(root, {"files": {"inside.txt": "ok"}, **setup})
    # Every path is checked before anything is written.
    assert sorted(p.name for p in tmp_path.rglob("*")) == ["ws"]
