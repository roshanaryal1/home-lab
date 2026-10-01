"""A JSON Schema for tool calls, generated from the broker's own table (5.3, #76).

Constrained decoding makes an invalid tool call impossible rather than
unlikely, but only if the constraint is right and stays right. So the schema
is not written by hand: it is derived from ``broker.TOOL_SCHEMAS``, the same
table ``validate_params`` enforces. A validator the generator does not know
raises ``GrammarError``, and a test fails when a tool or validator is added
without a fragment here, so the two cannot drift apart silently.

What this is and is not:

* It can only narrow what a model emits. ``parse_tool_call`` and the broker
  remain the authority (a schema cannot say "no NUL byte in argv", and the
  parser still refuses one).
* Whether a given inference server honours ``response_format`` with a JSON
  Schema, and how strictly, is server-specific. It is verified on the mini in
  the constrained-decoding measurement (checklist section 20), not assumed.
* ``conforms`` and ``sample`` are a small validator and generator for this
  schema's own subset. They exist so tests can compare the schema with the
  strict parser without adding a dependency.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from lab.broker import TOOL_SCHEMAS, _is_argv, _is_str, _is_str_list, _is_timeout

Schema = dict[str, Any]

FRAGMENTS: dict[Callable[[object], bool], Schema] = {
    _is_str: {"type": "string"},
    _is_argv: {"type": "array", "items": {"type": "string"}, "minItems": 1},
    _is_timeout: {"type": "number", "exclusiveMinimum": 0},
    _is_str_list: {"type": "array", "items": {"type": "string"}},
}


class GrammarError(ValueError):
    """The broker table contains something this generator cannot express."""


def fragment_for(validator: Callable[[object], bool]) -> Schema:
    try:
        return dict(FRAGMENTS[validator])
    except KeyError:
        raise GrammarError(
            f"no schema fragment for validator {getattr(validator, '__name__', validator)!r}; "
            "teach lab/grammar.py about it") from None


def _branch(tool: str) -> Schema:
    params = TOOL_SCHEMAS[tool]
    arguments: Schema = {
        "type": "object", "additionalProperties": False,
        "properties": {name: fragment_for(valid) for name, (valid, _) in params.items()},
    }
    required = sorted(name for name, (_, req) in params.items() if req)
    if required:
        arguments["required"] = required
    return {
        "type": "object", "additionalProperties": False, "required": ["tool", "arguments"],
        "properties": {"tool": {"const": tool}, "arguments": arguments},
    }


def tool_call_schema(allowed: set[str] | frozenset[str] | None = None) -> Schema:
    """The schema of one tool call, restricted to the tools a task holds."""
    tools = sorted(TOOL_SCHEMAS if allowed is None else allowed)
    if not tools:
        raise GrammarError("a task that holds no tools has no tool call to constrain")
    unknown = [t for t in tools if t not in TOOL_SCHEMAS]
    if unknown:
        raise GrammarError(f"unknown tools: {unknown}")
    return {"type": "object", "additionalProperties": False,
            "oneOf": [_branch(tool) for tool in tools]}


def response_format(allowed: set[str] | frozenset[str] | None = None) -> Schema:
    """The ``response_format`` value for an OpenAI-compatible chat request."""
    return {"type": "json_schema",
            "json_schema": {"name": "tool_call", "strict": True,
                            "schema": tool_call_schema(allowed)}}


# ---------------------------------------------------- the subset we generate


def conforms(schema: Schema, value: object) -> bool:
    """Does ``value`` satisfy ``schema``? Supports only what this module emits."""
    if "oneOf" in schema:
        return sum(1 for s in schema["oneOf"] if conforms(s, value)) == 1
    if "const" in schema:
        return bool(value == schema["const"] and type(value) is type(schema["const"]))
    kind = schema.get("type")
    if kind == "string":
        return isinstance(value, str)
    if kind == "number":
        ok = isinstance(value, int | float) and not isinstance(value, bool)
        return ok and value > schema.get("exclusiveMinimum", float("-inf"))
    if kind == "array":
        if not isinstance(value, list) or len(value) < schema.get("minItems", 0):
            return False
        return all(conforms(schema["items"], item) for item in value)
    if kind == "object":
        if not isinstance(value, dict):
            return False
        props: dict[str, Schema] = schema.get("properties", {})
        if schema.get("additionalProperties") is False and set(value) - set(props):
            return False
        if any(name not in value for name in schema.get("required", [])):
            return False
        return all(conforms(props[name], item) for name, item in value.items() if name in props)
    return False


def _example(schema: Schema) -> object:
    if "const" in schema:
        return schema["const"]
    kind = schema["type"]
    if kind == "string":
        return "a"
    if kind == "number":
        return 1
    if kind == "array":
        return [_example(schema["items"])]
    raise GrammarError(f"cannot sample a {kind}")


def sample(branch: Schema, *, include_optional: bool) -> dict[str, Any]:
    """One value that satisfies a single tool's branch."""
    args_schema = branch["properties"]["arguments"]
    required = set(args_schema.get("required", []))
    arguments = {name: _example(frag) for name, frag in args_schema["properties"].items()
                 if include_optional or name in required}
    return {"tool": branch["properties"]["tool"]["const"], "arguments": arguments}
