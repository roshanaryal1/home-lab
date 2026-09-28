"""The bounded model adapter (item 5.1, #74), against a mock and a stub server."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest

from lab.model import (
    AdmissionController,
    AdmissionRefused,
    BoundedModel,
    MalformedToolCall,
    MockAdapter,
    ModelError,
    ModelMismatch,
    ModelSpec,
    OpenAICompatibleAdapter,
    estimate_tokens,
    parse_tool_call,
)

REV = "a" * 40
TOK = "b" * 64


def spec(**kw) -> ModelSpec:
    base = {"name": "qwen-test", "revision": REV, "tokenizer_revision": TOK,
            "context_tokens": 8192, "max_output_tokens": 1024, "weights_mb": 16_700,
            "kv_bytes_per_token": 100_000}
    return ModelSpec(**{**base, **kw})


def msgs(text: str = "hello") -> list[dict[str, str]]:
    return [{"role": "user", "content": text}]


# ----------------------------------------------------------- frozen identity


@pytest.mark.parametrize("bad", ["main", "latest", "v1.0", "abc123", "A" * 40, "g" * 40,
                                 "a" * 39, "a" * 41, ""])
def test_a_moving_or_malformed_revision_is_refused(bad: str) -> None:
    with pytest.raises(ValueError, match="hex-digit hash"):
        spec(revision=bad)
    with pytest.raises(ValueError, match="hex-digit hash"):
        spec(tokenizer_revision=bad)


def test_limits_must_make_sense() -> None:
    for kw in ({"context_tokens": 0}, {"max_output_tokens": 0}, {"weights_mb": 0},
               {"max_output_tokens": 9000}, {"name": ""}):
        with pytest.raises(ValueError):
            spec(**kw)


@pytest.mark.safety
def test_a_server_answering_as_a_different_model_is_refused() -> None:
    model = BoundedModel(spec(), MockAdapter(["hi"], model="qwen-test-updated"))
    with pytest.raises(ModelMismatch):
        model.generate(msgs())


def test_the_pinned_model_answers() -> None:
    reply = BoundedModel(spec(), MockAdapter(["hi there"])).generate(msgs())
    assert reply.text == "hi there" and reply.model == "qwen-test"


# ---------------------------------------------------------------- admission


@pytest.mark.safety
def test_an_oversized_prompt_is_refused_before_the_model_is_called() -> None:
    adapter = MockAdapter(["never"])
    model = BoundedModel(spec(context_tokens=2048, max_output_tokens=512), adapter)
    with pytest.raises(AdmissionRefused, match="exceeds"):
        model.generate(msgs("x" * 20_000))
    assert adapter.calls == []


@pytest.mark.parametrize("tokens", [0, -1, 2000])
def test_output_limits_are_enforced(tokens: int) -> None:
    adapter = MockAdapter(["x"])
    with pytest.raises(AdmissionRefused, match="max_tokens"):
        BoundedModel(spec(), adapter).generate(msgs(), max_tokens=tokens)
    assert adapter.calls == []


@pytest.mark.safety
def test_a_request_that_would_swap_is_refused_at_admission() -> None:
    """Weights fit, but weights plus the cache for this request do not."""
    tight = AdmissionController(budget_mb=17_000)
    model = BoundedModel(spec(kv_bytes_per_token=1_000_000), MockAdapter(["x"]), tight)
    model.generate(msgs("short"), max_tokens=16)                       # small cache: fine
    with pytest.raises(AdmissionRefused, match="resident"):
        model.generate(msgs("word " * 800), max_tokens=1024)           # big cache: refused


def test_weights_larger_than_the_budget_never_load() -> None:
    ctrl = AdmissionController(budget_mb=10_000)
    with pytest.raises(AdmissionRefused, match="needs 16700 MB"):
        ctrl.load(spec())
    with pytest.raises(AdmissionRefused):
        BoundedModel(spec(), MockAdapter(["x"]), ctrl).generate(msgs())


def test_two_heavy_models_do_not_fit_together() -> None:
    ctrl = AdmissionController()
    ctrl.load(spec(name="a"))
    with pytest.raises(AdmissionRefused):
        ctrl.load(spec(name="b"))
    ctrl.unload("a")
    ctrl.load(spec(name="b"))
    assert ctrl.resident_mb == 16_700


@pytest.mark.safety
def test_only_one_heavy_request_runs_at_a_time_and_the_slot_is_always_released() -> None:
    ctrl = AdmissionController()
    inside = threading.Event()
    release = threading.Event()
    results: list[str] = []

    def slow(_messages) -> str:
        inside.set()
        release.wait(5)
        return "done"

    model = BoundedModel(spec(), MockAdapter(slow), ctrl)
    worker = threading.Thread(target=lambda: results.append(model.generate(msgs()).text))
    worker.start()
    assert inside.wait(5)
    with pytest.raises(AdmissionRefused, match="heavy inference slot"):
        model.generate(msgs())
    release.set()
    worker.join(5)
    assert results == ["done"]
    assert model.generate(msgs()).text == "done", "the slot was released"

    def boom(_messages) -> str:
        raise RuntimeError("adapter crashed")

    crashing = BoundedModel(spec(), MockAdapter(boom), ctrl)
    with pytest.raises(RuntimeError):
        crashing.generate(msgs())
    assert model.generate(msgs()).text == "done", "released after an exception too"


def test_a_light_model_does_not_take_the_heavy_slot() -> None:
    ctrl = AdmissionController()
    light = spec(name="light", weights_mb=1_000, heavy=False)
    with ctrl.admit(light, msgs(), 16), ctrl.admit(light, msgs(), 16):
        pass


def test_the_time_limit_is_clamped_to_the_controller_maximum() -> None:
    ctrl = AdmissionController(max_seconds=30)
    with ctrl.admit(spec(), msgs(), 16, timeout_seconds=9999) as ticket:
        assert ticket.timeout_seconds == 30


def test_the_token_estimate_errs_high() -> None:
    assert estimate_tokens("a" * 300) >= 100 and estimate_tokens("") == 1
    assert estimate_tokens("é" * 100) >= 66        # multibyte counts by bytes


# --------------------------------------------------------------- tool calls

GOOD = '{"tool": "fs.write", "arguments": {"path": "a.txt", "content": "hi"}}'


def test_a_valid_tool_call_parses() -> None:
    call = parse_tool_call(GOOD)
    assert call.tool == "fs.write" and call.arguments == {"path": "a.txt", "content": "hi"}
    assert parse_tool_call('{"tool":"fs.list","arguments":{}}').tool == "fs.list"


@pytest.mark.safety
@pytest.mark.parametrize("text,why", [
    ("```json\n" + GOOD + "\n```", "fence"),
    ("Sure! " + GOOD, "prose before"),
    (GOOD + " hope that helps", "prose after"),
    (" " + GOOD, "leading space"),
    (GOOD + "\n", "trailing newline"),
    ('{"tool": "fs.write", "arguments": {"path": "a", "content": "b",}}', "trailing comma"),
    ("{'tool': 'fs.write', 'arguments': {}}", "single quotes"),
    ('{"tool": "fs.write", "arguments": {"path": "a", "content": "b"}, "note": 1}', "extra key"),
    ('{"tool": "fs.write"}', "missing arguments"),
    ('{"arguments": {}}', "missing tool"),
    ('{"tool": "fs.write", "tool": "fs.delete", "arguments": {"path": "a", "content": "b"}}',
     "duplicate key"),
    ('{"tool": "fs.write", "arguments": {"path": "a", "path": "../x", "content": "b"}}',
     "duplicate inner key"),
    ('{"tool": "fs.list", "arguments": {"path": NaN}}', "NaN"),
    ('{"tool": "fs.list", "arguments": {"path": Infinity}}', "Infinity"),
    ('{"tool": ["fs.list"], "arguments": {}}', "tool not a string"),
    ('{"tool": "fs.list", "arguments": []}', "arguments not an object"),
    ('{"tool": "fs.nope", "arguments": {}}', "unknown tool"),
    ('{"tool": "fs.write", "arguments": {"path": "a"}}', "missing required argument"),
    ('{"tool": "fs.write", "arguments": {"path": 5, "content": "b"}}', "wrong type"),
    ('{"tool": "fs.write", "arguments": {"path": "a", "content": "b", "mode": "777"}}',
     "unreviewed argument"),
    ('{"tool": "shell.run", "arguments": {"argv": "rm -rf /"}}', "string instead of argv"),
    ('{"tool": "shell.run", "arguments": {"argv": ["ls"], "timeout": true}}', "bool timeout"),
    ('{"tool": "fs.list", "arguments": {"a": {"b": {"c": {"d": {"e": {"f": {"g": 1}}}}}}}}',
     "too deep"),
    ("", "empty"), ("{}", "empty object"), ("[]", "list"), ("null", "null"),
    ("{" + '"a":' * 5000 + "1}", "not json"),
    ('{"tool": "fs.write", "arguments": {"path": "a", "content": "' + "x" * 20000 + '"}}',
     "too large"),
])
def test_anything_but_one_valid_object_is_refused_not_repaired(text: str, why: str) -> None:
    with pytest.raises(MalformedToolCall):
        parse_tool_call(text)


def test_a_deep_bomb_does_not_crash_the_parser() -> None:
    with pytest.raises(MalformedToolCall):
        parse_tool_call("{" * 100_000 + "}" * 100_000)


def test_a_dangerous_but_well_formed_call_parses_and_is_left_to_policy() -> None:
    """The parser checks shape, not intent: policy and the approval gate decide
    whether fs.delete may run. It must not be silently altered here."""
    call = parse_tool_call('{"tool": "fs.delete", "arguments": {"path": "../../etc"}}')
    assert call.arguments == {"path": "../../etc"}


def test_bounded_model_tool_call_goes_through_admission_and_strict_parsing() -> None:
    good = BoundedModel(spec(), MockAdapter([GOOD]))
    assert good.tool_call(msgs()).tool == "fs.write"
    bad = BoundedModel(spec(), MockAdapter(["Sure, here you go: " + GOOD]))
    with pytest.raises(MalformedToolCall):
        bad.tool_call(msgs())


# ------------------------------------------------- the OpenAI-compatible client


class _Server(BaseHTTPRequestHandler):
    mode = "ok"
    seen: ClassVar[list[dict]] = []

    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        request = json.loads(self.rfile.read(length))
        type(self).seen.append(request)
        mode = type(self).mode
        if mode == "error":
            self.send_response(500)
            self.end_headers()
            return
        body = {"model": "other-model" if mode == "swap" else request["model"],
                "choices": [{"message": {"role": "assistant", "content": "pong"}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 2}}
        raw = b"not json" if mode == "garbage" else json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args) -> None:
        pass


@pytest.fixture()
def server():
    _Server.mode, _Server.seen = "ok", []
    httpd = HTTPServer(("127.0.0.1", 0), _Server)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/v1"
    httpd.shutdown()


def test_the_client_sends_a_deterministic_bounded_request(server) -> None:
    model = BoundedModel(spec(), OpenAICompatibleAdapter(server))
    reply = model.generate(msgs("ping"), max_tokens=64, seed=7)
    assert reply.text == "pong" and reply.completion_tokens == 2
    (sent,) = _Server.seen
    assert sent["model"] == "qwen-test" and sent["temperature"] == 0
    assert sent["max_tokens"] == 64 and sent["seed"] == 7 and sent["stream"] is False


@pytest.mark.parametrize("mode,exc", [("swap", ModelMismatch), ("garbage", ModelError),
                                      ("error", ModelError)])
def test_a_wrong_or_broken_server_is_an_error(server, mode, exc) -> None:
    _Server.mode = mode
    with pytest.raises(exc):
        BoundedModel(spec(), OpenAICompatibleAdapter(server)).generate(msgs())


def test_a_dead_server_is_a_clean_error() -> None:
    with pytest.raises(ModelError, match="unavailable"):
        BoundedModel(spec(), OpenAICompatibleAdapter("http://127.0.0.1:9/v1")).generate(msgs())


@pytest.mark.safety
@pytest.mark.parametrize("url", ["http://example.com/v1", "https://127.0.0.1/v1",
                                 "http://10.0.0.5:8080/v1", "http://169.254.169.254/v1",
                                 "ftp://localhost/v1", "http://localhost.evil.com/v1"])
def test_the_client_refuses_anything_but_loopback_http(url: str) -> None:
    with pytest.raises(ValueError, match="loopback"):
        OpenAICompatibleAdapter(url)
