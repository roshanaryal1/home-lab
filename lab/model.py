"""A bounded model adapter (item 5.1, #74).

Lands against a mock: nothing here needs the Mac mini. What the adapter
must guarantee does not depend on which model sits behind it.

* **Frozen identity.** A model is pinned by name plus a full revision
  (a 40 or 64 hex-digit commit or content hash) for the weights and for
  the tokenizer. Branch names and tags ("main", "latest") are refused,
  and a reply that reports a different model name than the pinned one is
  refused, so a silently updated model cannot slip in.
* **Admission before work.** One controller decides, before the model is
  called, whether a request fits: prompt tokens, output tokens, wall
  time, and memory residency against the budget, with a single heavy
  slot. An oversized request is refused at admission, not discovered as
  swap. The token count is an estimate that errs high until the real
  tokenizer is plugged in.
* **Strict tool calls.** Model output that is meant to be a tool call is
  parsed, never repaired: one JSON object with exactly ``tool`` and
  ``arguments``, no code fence or prose around it, no duplicate keys, no
  NaN, bounded depth and size, then checked against the broker's own
  schema for that tool. A malformed call is an error to the caller, who
  may ask again; it is never fixed up, because a repaired call can carry
  an argument the model never chose.
* **Local only.** The OpenAI-compatible client talks to a loopback
  inference server and refuses any other host; anything that reaches the
  internet goes through the egress gateway.
"""

from __future__ import annotations

import fcntl
import http.client
import json
import os
import re
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from lab.broker import BrokerError, validate_params

REVISION = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
MAX_TOOL_CALL_BYTES = 16 * 1024
MAX_TOOL_CALL_DEPTH = 6
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})

# Policy from ADR 0001: 32 GB less 11.5 GB reserved for the OS and the
# lab's own services leaves 20.5 GB for weights and cache. Measured on the
# M6 (ADR 0001, "Measured on the M6"): the heavy model holds 17,180 MB with
# its weights loaded, and Metal's hard ceiling is 24.96 GiB, so this budget
# is the binding limit and leaves about 16K tokens of context.
DEFAULT_BUDGET_MB = 20_500
# The served heavy model's resident size with its weights loaded, measured on
# the M6 (ADR 0001). The loop uses it when LAB_MODEL_WEIGHTS_MB is not set, so
# its accounting matches the model the server actually holds (#211).
HEAVY_MODEL_WEIGHTS_MB = 17_180


class ModelError(RuntimeError):
    """Base for refusals the model layer makes."""


class AdmissionRefused(ModelError):
    """The request was refused before the model was called."""


class SlotBusy(AdmissionRefused):
    """The heavy slot stayed in use for the whole wait. Unlike the other refusals,
    it says nothing about the request itself, so a caller may treat it apart."""


class ModelMismatch(ModelError):
    """The server answered as a different model than the one pinned."""


class MalformedToolCall(ModelError):
    """Model output was not a valid tool call. Never repaired."""


@dataclass(frozen=True)
class ModelSpec:
    name: str
    revision: str
    tokenizer_revision: str
    context_tokens: int
    max_output_tokens: int
    weights_mb: int
    kv_bytes_per_token: int = 200_000      # measured on the M6 for the heavy model, ADR 0001
    heavy: bool = True

    def __post_init__(self) -> None:
        for what, value in (("revision", self.revision),
                            ("tokenizer_revision", self.tokenizer_revision)):
            if not REVISION.match(value):
                raise ValueError(
                    f"{what} must be a full 40 or 64 hex-digit hash, not {value!r}: "
                    "branch names and tags move")
        if not self.name or self.context_tokens <= 0 or self.max_output_tokens <= 0 \
                or self.weights_mb <= 0 or self.max_output_tokens > self.context_tokens:
            raise ValueError("model limits must be positive and output must fit the context")


def estimate_tokens(text: str) -> int:
    """A deliberately high estimate: one token per 3 UTF-8 bytes, rounded up.
    Real tokenizers average 3.5 to 4.5 bytes per token on prose."""
    return len(text.encode("utf-8")) // 3 + 1


@dataclass(frozen=True)
class Ticket:
    prompt_tokens: int
    max_tokens: int
    timeout_seconds: float
    resident_mb: int


class AdmissionController:
    """Decides whether a request may run. One instance per machine."""

    def __init__(self, budget_mb: int = DEFAULT_BUDGET_MB, max_seconds: float = 300.0,
                 count_tokens: Callable[[str], int] = estimate_tokens, *,
                 slot_lock: str | Path | None = None,
                 slot_wait_seconds: float = 0.0) -> None:
        """``slot_lock`` names a file whose flock is the heavy slot for every
        process that passes the same path, so two processes sharing one model
        server cannot both run a heavy request (#211). Without it the slot
        holds within this process only. ``slot_wait_seconds`` is how long a
        heavy request waits for the slot before it is refused; 0 refuses at once.
        """
        self.budget_mb = budget_mb
        self.max_seconds = max_seconds
        self.slot_lock = None if slot_lock is None else Path(slot_lock)
        self.slot_wait_seconds = max(0.0, slot_wait_seconds)
        self._count = count_tokens
        self._lock = threading.Lock()
        self._heavy = threading.Lock()
        self._resident: dict[str, int] = {}     # model name -> weights MB currently loaded

    def load(self, spec: ModelSpec) -> None:
        """Record a model as resident. Refuses if the weights alone do not fit."""
        with self._lock:
            others = sum(mb for n, mb in self._resident.items() if n != spec.name)
            if others + spec.weights_mb > self.budget_mb:
                raise AdmissionRefused(
                    f"{spec.name} needs {spec.weights_mb} MB; {self.budget_mb - others} MB free")
            self._resident[spec.name] = spec.weights_mb

    def unload(self, name: str) -> None:
        with self._lock:
            self._resident.pop(name, None)

    @property
    def resident_mb(self) -> int:
        return sum(self._resident.values())

    @contextmanager
    def admit(self, spec: ModelSpec, messages: list[dict[str, str]], max_tokens: int,
              timeout_seconds: float | None = None) -> Iterator[Ticket]:
        prompt_tokens = sum(self._count(m.get("content", "")) + 4 for m in messages)
        if max_tokens <= 0 or max_tokens > spec.max_output_tokens:
            raise AdmissionRefused(
                f"max_tokens {max_tokens} is outside 1..{spec.max_output_tokens}")
        if prompt_tokens + max_tokens > spec.context_tokens:
            raise AdmissionRefused(
                f"prompt of about {prompt_tokens} tokens plus {max_tokens} output exceeds "
                f"the {spec.context_tokens} token context")
        seconds = min(timeout_seconds or self.max_seconds, self.max_seconds)
        cache_mb = spec.kv_bytes_per_token * (prompt_tokens + max_tokens) // 1_000_000 + 1
        with self._lock:
            resident_after = (sum(mb for n, mb in self._resident.items() if n != spec.name)
                              + spec.weights_mb + cache_mb)
            if resident_after > self.budget_mb:
                raise AdmissionRefused(
                    f"this request would need {resident_after} MB resident against a "
                    f"{self.budget_mb} MB budget")
            self._resident[spec.name] = spec.weights_mb
        if not spec.heavy:
            yield Ticket(prompt_tokens, max_tokens, seconds, resident_after)
            return
        with self._heavy_slot():
            yield Ticket(prompt_tokens, max_tokens, seconds, resident_after)

    @contextmanager
    def _heavy_slot(self) -> Iterator[None]:
        deadline = time.monotonic() + self.slot_wait_seconds
        acquired = (self._heavy.acquire(timeout=self.slot_wait_seconds) if self.slot_wait_seconds
                    else self._heavy.acquire(blocking=False))
        if not acquired:
            raise SlotBusy("the heavy inference slot is in use")
        fd = -1
        try:
            if self.slot_lock is not None:
                fd = os.open(self.slot_lock, os.O_RDWR | os.O_CREAT, 0o600)
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise SlotBusy(
                                f"the heavy inference slot is in use by another process "
                                f"({self.slot_lock})") from None
                        time.sleep(0.05)
            yield
        finally:
            if fd >= 0:
                os.close(fd)        # closing the descriptor releases the flock
            self._heavy.release()


@dataclass(frozen=True)
class Completion:
    text: str
    model: str                # what the server says answered
    prompt_tokens: int
    completion_tokens: int
    seconds: float


class Adapter(Protocol):
    def complete(self, spec: ModelSpec, messages: list[dict[str, str]], ticket: Ticket,
                 seed: int | None) -> Completion: ...


class MockAdapter:
    """Scripted replies, for tests and for developing everything above the model."""

    def __init__(self, replies: list[str] | Callable[[list[dict[str, str]]], str],
                 model: str | None = None, delay: float = 0.0) -> None:
        self._replies = replies
        self._model = model
        self._delay = delay
        self.calls: list[dict[str, Any]] = []

    def complete(self, spec: ModelSpec, messages: list[dict[str, str]], ticket: Ticket,
                 seed: int | None) -> Completion:
        self.calls.append({"messages": messages, "max_tokens": ticket.max_tokens, "seed": seed})
        if self._delay:
            time.sleep(self._delay)
        text = (self._replies(messages) if callable(self._replies)
                else self._replies[min(len(self.calls) - 1, len(self._replies) - 1)])
        return Completion(text, self._model or spec.name, ticket.prompt_tokens,
                          min(estimate_tokens(text), ticket.max_tokens), self._delay)


class OpenAICompatibleAdapter:
    """Chat completions against a loopback server (mlx-lm, llama.cpp, vLLM)."""

    def __init__(self, base_url: str, *, response_format: dict[str, Any] | None = None) -> None:
        """``response_format`` is sent with every request when set: the
        constrained-decoding option (``lab.grammar.response_format()``). Whether
        a server honours it is server-specific and measured, not assumed."""
        if response_format is not None and not isinstance(response_format, dict):
            raise ValueError("response_format must be a JSON object")
        self._response_format = response_format
        parts = urllib.parse.urlsplit(base_url)
        if parts.scheme != "http" or (parts.hostname or "") not in LOOPBACK:
            raise ValueError("the inference server must be http on loopback; "
                             "anything else goes through the egress gateway")
        self._host = parts.hostname or "127.0.0.1"
        self._port = parts.port or 80
        self._path = parts.path.rstrip("/") + "/chat/completions"
        self.base_url = base_url

    def complete(self, spec: ModelSpec, messages: list[dict[str, str]], ticket: Ticket,
                 seed: int | None) -> Completion:
        payload: dict[str, Any] = {"model": spec.name, "messages": messages,
                                   "max_tokens": ticket.max_tokens, "temperature": 0,
                                   "stream": False}
        if seed is not None:
            payload["seed"] = seed
        if self._response_format is not None:
            payload["response_format"] = self._response_format
        started = time.monotonic()
        conn = http.client.HTTPConnection(self._host, self._port,
                                          timeout=ticket.timeout_seconds)
        try:
            conn.request("POST", self._path, body=json.dumps(payload),
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
        except (OSError, http.client.HTTPException) as exc:
            raise ModelError(f"inference server unavailable: {exc}") from None
        finally:
            conn.close()
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ModelError("inference response larger than the cap")
        if resp.status != 200:
            raise ModelError(f"inference server returned {resp.status}")
        try:
            data = json.loads(raw)
            text = data["choices"][0]["message"]["content"]
            usage = data.get("usage") or {}
            model = str(data["model"])
        except (ValueError, KeyError, IndexError, TypeError):
            raise ModelError("inference response was not a chat completion") from None
        if not isinstance(text, str):
            raise ModelError("inference response content was not text")
        return Completion(text, model, int(usage.get("prompt_tokens", ticket.prompt_tokens)),
                          int(usage.get("completion_tokens", 0)), time.monotonic() - started)


class BoundedModel:
    """The only way the lab calls a model."""

    def __init__(self, spec: ModelSpec, adapter: Adapter,
                 controller: AdmissionController | None = None) -> None:
        self.spec = spec
        self.adapter = adapter
        self.controller = controller or AdmissionController()

    def generate(self, messages: list[dict[str, str]], *, max_tokens: int | None = None,
                 seed: int | None = None, timeout_seconds: float | None = None) -> Completion:
        limit = self.spec.max_output_tokens if max_tokens is None else max_tokens
        with self.controller.admit(self.spec, messages, limit, timeout_seconds) as ticket:
            completion = self.adapter.complete(self.spec, messages, ticket, seed)
        if completion.model != self.spec.name:
            raise ModelMismatch(
                f"pinned {self.spec.name!r} but the server answered as {completion.model!r}")
        if completion.completion_tokens > limit:
            raise ModelError("the server exceeded the output limit it was given")
        return completion

    def tool_call(self, messages: list[dict[str, str]], **kw: Any) -> ToolCall:
        return parse_tool_call(self.generate(messages, **kw).text)


@dataclass(frozen=True)
class ToolCall:
    tool: str
    arguments: dict[str, Any]


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise MalformedToolCall(f"duplicate key {key!r}")
        out[key] = value
    return out


def _refuse_constant(name: str) -> Any:
    raise MalformedToolCall(f"{name} is not valid JSON")


def _depth(value: Any, level: int = 1) -> int:
    if isinstance(value, dict):
        return max([level] + [_depth(v, level + 1) for v in value.values()])
    if isinstance(value, list):
        return max([level] + [_depth(v, level + 1) for v in value])
    return level


def parse_tool_call(text: str) -> ToolCall:
    """Strict parse. Raises ``MalformedToolCall``; never repairs."""
    if len(text.encode("utf-8")) > MAX_TOOL_CALL_BYTES:
        raise MalformedToolCall("tool call larger than the cap")
    if not (text.startswith("{") and text.endswith("}")):
        raise MalformedToolCall("output must be exactly one JSON object, with nothing around it")
    try:
        obj = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_refuse_constant)
    except MalformedToolCall:
        raise
    except (ValueError, RecursionError) as exc:
        raise MalformedToolCall(f"not valid JSON: {exc}") from None
    if not isinstance(obj, dict) or set(obj) != {"tool", "arguments"}:
        raise MalformedToolCall('keys must be exactly "tool" and "arguments"')
    tool, arguments = obj["tool"], obj["arguments"]
    if not isinstance(tool, str) or not isinstance(arguments, dict):
        raise MalformedToolCall('"tool" must be a string and "arguments" an object')
    if _depth(arguments) > MAX_TOOL_CALL_DEPTH:
        raise MalformedToolCall("arguments nested too deeply")
    try:
        validate_params(tool, arguments)
    except BrokerError as exc:
        raise MalformedToolCall(f"{type(exc).__name__}: {exc}") from None
    return ToolCall(tool, arguments)
