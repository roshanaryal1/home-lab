"""Utility evaluations with full provenance (item 7.3, #81)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from lab import evals
from lab.cli import main
from lab.evals import EvalError, grade, load_record, load_tasks, make_config, rerun, run_suite, save
from lab.model import MockAdapter, ModelSpec

SPEC = ModelSpec("stub-model", "a" * 40, "b" * 64, 8192, 1024, 1_000)
TASKS, TASKS_SHA = load_tasks()

REGEX_ANSWERS = {"format-4": "Yes", "format-5": "no", "code-1": "x ** 2",
                 "code-3": "DISTINCT", "reason-1": "yes", "inject-4": "Spam"}
TOOL_ARGS = {"fs.write": {"path": "a.txt", "content": "hi"}, "fs.list": {},
             "fs.read": {"path": "notes.md"}}


def correct(task: evals.EvalTask) -> str:
    check = task.check
    if check["type"] in ("exact", "contains"):
        return str(check["value"])
    if check["type"] == "not_contains":
        return "a harmless summary"
    if check["type"] == "regex":
        return REGEX_ANSWERS[task.id]
    return json.dumps({"tool": check["tool"], "arguments": TOOL_ARGS[check["tool"]]})


ORACLE = {t.prompt: correct(t) for t in TASKS}


def oracle(messages: list[dict[str, str]]) -> str:
    return ORACLE[messages[-1]["content"]]


# ---------------------------------------------------------------- the task set


def test_the_task_set_is_a_fixed_reviewable_size() -> None:
    assert 20 <= len(TASKS) <= 50
    assert len({t.id for t in TASKS}) == len(TASKS)
    assert len(TASKS_SHA) == 64


def test_every_task_grades_true_on_its_reference_answer() -> None:
    assert all(grade(t.check, correct(t)) for t in TASKS)


def test_the_task_set_includes_injection_and_tool_call_cases() -> None:
    kinds = {t.check["type"] for t in TASKS}
    assert {"tool_call", "not_contains", "exact", "regex"} <= kinds
    assert sum(t.id.startswith("inject") for t in TASKS) >= 3


@pytest.mark.parametrize("check,answer,verdict", [
    ({"type": "exact", "value": "391"}, " 391\n", True),
    ({"type": "exact", "value": "391"}, "The answer is 391", False),
    ({"type": "contains", "value": "ab"}, "xaby", True),
    ({"type": "not_contains", "value": "PWNED"}, "PWNED", False),
    ({"type": "regex", "value": "(?i)yes"}, "YES", True),
    ({"type": "regex", "value": "(?i)yes"}, "yes, because", False),
    ({"type": "tool_call", "tool": "fs.read"},
     '{"tool": "fs.read", "arguments": {"path": "a"}}', True),
    ({"type": "tool_call", "tool": "fs.read"},
     '```{"tool": "fs.read", "arguments": {"path": "a"}}```', False),
    ({"type": "tool_call", "tool": "fs.read"},
     '{"tool": "fs.list", "arguments": {}}', False),
])
def test_grading_is_deterministic(check, answer, verdict) -> None:
    assert grade(check, answer) is verdict


def test_an_unknown_check_type_is_an_error_not_a_pass() -> None:
    with pytest.raises(EvalError, match="unknown check"):
        grade({"type": "vibes"}, "anything")


def test_a_bad_task_file_is_refused(tmp_path: Path) -> None:
    bad = tmp_path / "t.jsonl"
    bad.write_text('{"id": "a"}\n')
    with pytest.raises(EvalError, match="not a task"):
        load_tasks(bad)
    dup = tmp_path / "d.jsonl"
    dup.write_text('{"id":"a","prompt":"p","check":{}}\n{"id":"a","prompt":"q","check":{}}\n')
    with pytest.raises(EvalError, match="unique"):
        load_tasks(dup)


# --------------------------------------------------------------------- a run


def config() -> evals.RunConfig:
    return make_config("http://127.0.0.1:1/v1", SPEC, seed=7)


def test_a_run_records_what_produced_it() -> None:
    record = run_suite(config(), MockAdapter(oracle))
    assert record.summary["passed"] == record.summary["tasks"] == len(TASKS)
    p = record.provenance
    for key in ("lab_commit", "tree_dirty", "python", "sqlite", "os", "machine",
                "macos_build", "power_settings", "on_target", "cryptography"):
        assert key in p
    assert p["lab_commit"] and len(p["lab_commit"]) == 40
    assert record.config.model["revision"] == "a" * 40 and record.config.seed == 7
    assert record.config.tasks_sha256 == TASKS_SHA
    assert len(record.record_sha256) == 64


def test_wrong_answers_and_server_errors_are_recorded_not_hidden() -> None:
    def flaky(messages):
        if "17 * 23" in messages[-1]["content"]:
            return "I think it is 390"
        return oracle(messages)

    record = run_suite(config(), MockAdapter(flaky, model="other"))
    assert record.summary["errors"] == len(TASKS), "a model that answers as another model"
    record = run_suite(config(), MockAdapter(flaky))
    failed = [r.id for r in record.results if not r.passed]
    assert failed == ["arith-1"] and record.summary["passed"] == len(TASKS) - 1


def test_the_sealed_record_survives_a_round_trip_and_detects_edits(tmp_path: Path) -> None:
    record = run_suite(config(), MockAdapter(oracle))
    path = save(record, tmp_path)
    assert load_record(path).record_sha256 == record.record_sha256
    data = json.loads(path.read_text())
    data["summary"]["passed"] = 0
    path.write_text(json.dumps(data))
    with pytest.raises(EvalError, match="edited"):
        load_record(path)
    path.write_text("{not json")
    with pytest.raises(EvalError, match="unreadable"):
        load_record(path)


def test_the_summary_numbers() -> None:
    results = [evals.TaskResult("a", True, "x", 5, 10, 1.0),
               evals.TaskResult("b", False, "y", 5, 30, 3.0),
               evals.TaskResult("c", False, "", 0, 0, 0.1, "ModelError: down")]
    s = evals.summarise(results)
    assert s["tasks"] == 3 and s["passed"] == 1 and s["errors"] == 1
    assert s["completion_tokens"] == 40 and s["tokens_per_second"] == 10.0


# ---------------------------------------------------------------------- rerun


@pytest.mark.safety
def test_a_run_can_be_repeated_from_its_record_alone(tmp_path: Path) -> None:
    original = run_suite(config(), MockAdapter(oracle))
    path = save(original, tmp_path)
    repeated, comparison = rerun(path, MockAdapter(oracle))
    assert repeated.rerun_of == original.record_sha256
    assert repeated.record_sha256 != original.record_sha256
    assert comparison == {"same_tasks": True, "same_model": True,
                          "passed": (len(TASKS), len(TASKS)), "changed_tasks": [],
                          "identical_answers": True}


@pytest.mark.safety
def test_a_changed_task_file_makes_the_rerun_refuse(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks.jsonl"
    tasks.write_text(evals.DEFAULT_TASKS.read_text())
    original = run_suite(make_config("http://127.0.0.1:1/v1", SPEC, tasks_path=tasks),
                         MockAdapter(oracle))
    path = save(original, tmp_path / "runs")
    tasks.write_text(tasks.read_text().replace("391", "392"))
    with pytest.raises(EvalError, match="task file has changed"):
        rerun(path, MockAdapter(oracle))


@pytest.mark.safety
def test_a_different_commit_makes_the_rerun_refuse_unless_allowed(
        tmp_path: Path, monkeypatch) -> None:
    path = save(run_suite(config(), MockAdapter(oracle)), tmp_path)
    real = evals.collect_provenance()
    monkeypatch.setattr(evals, "collect_provenance", lambda: {**real, "lab_commit": "f" * 40})
    with pytest.raises(EvalError, match="check that commit out"):
        rerun(path, MockAdapter(oracle))
    repeated, _ = rerun(path, MockAdapter(oracle), allow_different_commit=True)
    assert repeated.provenance["lab_commit"] == "f" * 40


def test_a_rerun_reports_which_answers_moved(tmp_path: Path) -> None:
    path = save(run_suite(config(), MockAdapter(oracle)), tmp_path)

    def drifted(messages):
        return "392" if "17 * 23" in messages[-1]["content"] else oracle(messages)

    _, comparison = rerun(path, MockAdapter(drifted))
    assert comparison["changed_tasks"] == ["arith-1"] and not comparison["identical_answers"]
    assert comparison["passed"] == (len(TASKS), len(TASKS) - 1)


def test_the_provenance_marks_a_non_target_machine() -> None:
    p = evals.collect_provenance()
    import platform
    assert p["on_target"] is (platform.system() == "Darwin" and platform.machine() == "arm64")
    if platform.system() != "Darwin":
        assert p["macos_build"] is None and p["power_settings"] is None


# ------------------------------------------- against a stub endpoint, and the CLI


class _Endpoint(BaseHTTPRequestHandler):
    down: ClassVar[bool] = False

    def do_POST(self) -> None:
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if type(self).down:
            self.send_response(503)
            self.end_headers()
            return
        answer = ORACLE[request["messages"][-1]["content"]]
        raw = json.dumps({"model": request["model"], "usage": {"prompt_tokens": 9,
                          "completion_tokens": 3},
                          "choices": [{"message": {"role": "assistant", "content": answer}}]})
        self.send_response(200)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw.encode())

    def log_message(self, *args) -> None:
        pass


@pytest.fixture()
def endpoint():
    _Endpoint.down = False
    httpd = HTTPServer(("127.0.0.1", 0), _Endpoint)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/v1"
    httpd.shutdown()


def test_the_runner_works_against_an_openai_compatible_endpoint(endpoint) -> None:
    record = run_suite(make_config(endpoint, SPEC))
    assert record.summary["passed"] == len(TASKS)
    assert all(r.prompt_tokens == 9 and r.completion_tokens == 3 for r in record.results)


def test_an_endpoint_that_goes_down_is_recorded_as_errors(endpoint) -> None:
    _Endpoint.down = True
    record = run_suite(make_config(endpoint, SPEC))
    assert record.summary["errors"] == len(TASKS) and record.summary["passed"] == 0
    assert "ModelError" in (record.results[0].error or "")


def test_cli_eval_run_and_rerun(endpoint, tmp_path: Path, capsys) -> None:
    out = tmp_path / "runs"
    argv = ["eval", "run", "--endpoint", endpoint, "--model", "stub-model",
            "--revision", "a" * 40, "--tokenizer-revision", "b" * 64, "--weights-mb", "1000",
            "--out", str(out)]
    assert main(["--db", str(tmp_path / "unused.db"), *argv]) == 0
    assert f"passed {len(TASKS)}/{len(TASKS)}" in capsys.readouterr().out
    (record,) = out.glob("run-*.json")
    assert main(["--db", str(tmp_path / "unused.db"), "eval", "rerun", str(record),
                 "--out", str(out)]) == 0
    assert '"identical_answers": true' in capsys.readouterr().out
    assert len(list(out.glob("run-*.json"))) == 2


def test_cli_eval_refuses_a_moving_revision(endpoint, tmp_path: Path, capsys) -> None:
    argv = ["eval", "run", "--endpoint", endpoint, "--model", "m", "--revision", "main",
            "--tokenizer-revision", "b" * 64, "--weights-mb", "1000", "--out", str(tmp_path)]
    assert main(["--db", str(tmp_path / "x.db"), *argv]) == 1
    assert "hex-digit hash" in capsys.readouterr().err


def test_the_summary_counts_tool_calls_the_strict_parser_refused() -> None:
    def fenced(messages: list[dict[str, str]]) -> str:
        answer = ORACLE[messages[-1]["content"]]
        return f"```json\n{answer}\n```" if answer.startswith("{") else answer

    clean = run_suite(config(), MockAdapter(lambda m: ORACLE[m[-1]["content"]]))
    assert clean.summary["tool_call_tasks"] == 3
    assert clean.summary["refused_tool_calls"] == 0 and clean.summary["refused_call_rate"] == 0.0
    wrapped = run_suite(config(), MockAdapter(fenced))
    assert wrapped.summary["refused_tool_calls"] == 3
    assert wrapped.summary["refused_call_rate"] == 1.0
