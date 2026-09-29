"""Constrained decoding for tool calls (item 5.3, #76).

The schema handed to the inference server is generated from the broker's own
tool table, so it cannot drift from it. It can only narrow what a model
produces: ``parse_tool_call`` and the broker stay the authority.
"""

from __future__ import annotations

import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, ClassVar

import pytest

from lab import grammar
from lab.broker import TOOL_SCHEMAS, InvalidParams, validate_params
from lab.model import (
    AdmissionController,
    BoundedModel,
    MalformedToolCall,
    ModelSpec,
    OpenAICompatibleAdapter,
    parse_tool_call,
)


def test_every_validator_in_the_broker_table_has_a_schema_fragment() -> None:
    """Adding a tool or a validator without teaching this module fails here."""
    schema = grammar.tool_call_schema()
    tools = {b["properties"]["tool"]["const"] for b in schema["oneOf"]}
    assert tools == set(TOOL_SCHEMAS)


def test_an_unknown_validator_fails_closed() -> None:
    def odd(v: object) -> bool:
        return True

    with pytest.raises(grammar.GrammarError):
        grammar.fragment_for(odd)


def test_the_schema_is_a_closed_discriminated_union() -> None:
    schema = grammar.tool_call_schema()
    assert schema["type"] == "object" and schema["additionalProperties"] is False
    for branch in schema["oneOf"]:
        assert branch["additionalProperties"] is False
        assert set(branch["required"]) == {"tool", "arguments"}
        assert branch["properties"]["arguments"]["additionalProperties"] is False


def test_required_and_optional_parameters_match_the_broker() -> None:
    schema = grammar.tool_call_schema()
    by_tool = {b["properties"]["tool"]["const"]: b["properties"]["arguments"]
               for b in schema["oneOf"]}
    for tool, params in TOOL_SCHEMAS.items():
        args = by_tool[tool]
        assert set(args["properties"]) == set(params)
        assert set(args.get("required", [])) == {n for n, (_, req) in params.items() if req}


def test_only_the_tools_a_task_holds_can_be_produced() -> None:
    schema = grammar.tool_call_schema(allowed={"fs.read", "fs.list"})
    assert {b["properties"]["tool"]["const"] for b in schema["oneOf"]} == {"fs.read", "fs.list"}
    with pytest.raises(grammar.GrammarError):
        grammar.tool_call_schema(allowed={"fs.read", "rm.rf"})
    with pytest.raises(grammar.GrammarError):
        grammar.tool_call_schema(allowed=set())


@pytest.mark.parametrize("tool", sorted(TOOL_SCHEMAS))
def test_whatever_the_schema_admits_the_strict_parser_accepts(tool: str) -> None:
    schema = grammar.tool_call_schema()
    branch = next(b for b in schema["oneOf"] if b["properties"]["tool"]["const"] == tool)
    for include_optional in (False, True):
        sample = grammar.sample(branch, include_optional=include_optional)
        assert grammar.conforms(schema, sample)
        parsed = parse_tool_call(json.dumps(sample))
        validate_params(parsed.tool, parsed.arguments)


@pytest.mark.parametrize("bad", [
    {"tool": "fs.read", "arguments": {}},
    {"tool": "fs.read", "arguments": {"path": 5}},
    {"tool": "fs.read", "arguments": {"path": "a", "extra": 1}},
    {"tool": "shell.run", "arguments": {"argv": []}},
    {"tool": "shell.run", "arguments": {"argv": ["ls"], "timeout": 0}},
    {"tool": "nope", "arguments": {}},
    {"tool": "fs.read", "arguments": {"path": "a"}, "extra": 1},
])
def test_what_the_schema_rejects_the_parser_also_rejects(bad: dict[str, Any]) -> None:
    assert not grammar.conforms(grammar.tool_call_schema(), bad)
    with pytest.raises((MalformedToolCall, InvalidParams)):
        parse_tool_call(json.dumps(bad))


@pytest.mark.safety
def test_the_grammar_can_narrow_but_the_parser_stays_the_authority() -> None:
    """A NUL byte in argv is something a schema cannot say. It passes the
    schema and the parser still refuses it."""
    bad = {"tool": "shell.run", "arguments": {"argv": ["ls\u0000"]}}
    assert grammar.conforms(grammar.tool_call_schema(), bad)
    with pytest.raises(MalformedToolCall):
        parse_tool_call(json.dumps(bad))


# --------------------------------------------------- the adapter carries it


class _Capture(BaseHTTPRequestHandler):
    bodies: ClassVar[list[dict[str, Any]]] = []

    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        type(self).bodies.append(json.loads(self.rfile.read(length)))
        body = json.dumps({"model": "m", "choices": [{"message": {"content": "{}"}}],
                           "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a: object) -> None:
        pass


def test_the_adapter_sends_the_response_format_when_asked_and_not_otherwise() -> None:
    _Capture.bodies = []
    httpd = HTTPServer(("127.0.0.1", 0), _Capture)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{httpd.server_port}/v1"
        spec = ModelSpec("m", "a" * 40, "a" * 40, 4096, 128, 100, heavy=False)
        plain = BoundedModel(spec, OpenAICompatibleAdapter(url), AdmissionController())
        plain.generate([{"role": "user", "content": "hi"}])
        fmt = grammar.response_format()
        constrained = BoundedModel(
            spec, OpenAICompatibleAdapter(url, response_format=fmt), AdmissionController())
        constrained.generate([{"role": "user", "content": "hi"}])
    finally:
        httpd.shutdown()
    assert "response_format" not in _Capture.bodies[0]
    sent = _Capture.bodies[1]["response_format"]
    assert sent["type"] == "json_schema" and sent["json_schema"]["strict"] is True
    assert sent["json_schema"]["schema"] == grammar.tool_call_schema()


def test_response_format_must_be_a_json_object() -> None:
    with pytest.raises(ValueError):
        OpenAICompatibleAdapter("http://127.0.0.1:1/v1", response_format="strict")  # type: ignore[arg-type]
    assert http.client is not None
