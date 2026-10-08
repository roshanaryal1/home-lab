"""A loopback chat-completions server that constrains the reply for H1c (#179, option 2).

``mlx_lm.server`` cannot constrain a reply (it never reads ``response_format``,
#179). This server loads the same MLX weights through ``mlx_lm`` and applies
``lab.constrained.TokenConstraint`` as a logits processor, so the model may
only emit ``{"route": ..., "confidence": ...}`` in the shape
``lab.shadow.model_candidate`` accepts. ``lab shadow --endpoint`` talks to it
exactly as it talks to ``mlx_lm.server``: the client and the frozen H1
instrument (``lab/shadow.py``, ``lab/rubric.py``) are unchanged.

The constraint is fixed when the server starts, not chosen per request: a
request's ``response_format`` is ignored, and every reply names the grammar
it ran under in ``system_fingerprint`` (``h1c-route-v1``, or
``unconstrained`` with ``--unconstrained``). ``--unconstrained`` exists for the
equivalence check: with the constraint off, this server must give the same
reply as ``mlx_lm.server`` on the same probe, or the comparison with H1b would
measure the server, not the constraint.

On the Mac mini it runs with the Python of the ``mlx-lm`` tool environment,
which has ``mlx`` and ``mlx_lm``; this module imports them only there. Nothing
here is run in CI except through a fake backend.

    PYTHONPATH="$REPO" ~/.local/share/uv/tools/mlx-lm/bin/python -m lab.constrained_server \
        --model <snapshot path> --port 8081 [--unconstrained] [--self-check]
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections.abc import Callable, Sequence
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Protocol

from lab.constrained import (
    ALPHABET,
    GRAMMAR_VERSION,
    ConstraintError,
    TokenConstraint,
    is_complete,
    sentences,
    spell,
)

LOOPBACK = {"127.0.0.1", "::1", "localhost"}
MAX_BODY_BYTES = 1 << 20
MAX_TOKENS_LIMIT = 4096
DEFAULT_MAX_TOKENS = 512
UNCONSTRAINED = "unconstrained"

Allowed = Callable[[list[int]], list[int]]


class Backend(Protocol):
    """What the server needs from a model. ``MLXBackend`` on the Mac, a fake in tests."""

    @property
    def name(self) -> str: ...

    @property
    def vocab(self) -> Sequence[str]: ...

    @property
    def eos_ids(self) -> frozenset[int]: ...

    def prompt_ids(self, messages: list[dict[str, str]]) -> list[int]: ...

    def generate(self, prompt: list[int], max_tokens: int, seed: int | None,
                 allowed: Allowed | None) -> list[int]: ...

    def decode(self, ids: list[int]) -> str: ...


class RequestError(ValueError):
    """The request is not one this server answers (HTTP 400)."""


def parse_request(body: bytes) -> tuple[list[dict[str, str]], int, int | None]:
    """The messages, max_tokens and seed of a chat-completions request, or ``RequestError``."""
    try:
        data = json.loads(body)
    except ValueError:
        raise RequestError("the body is not JSON") from None
    if not isinstance(data, dict):
        raise RequestError("the body is not a JSON object")
    if data.get("stream"):
        raise RequestError("streaming is not supported")
    temperature = data.get("temperature", 0)
    if isinstance(temperature, bool) or temperature != 0:
        raise RequestError("only temperature 0 is served: the run is greedy")
    messages = data.get("messages")
    if not isinstance(messages, list) or not messages or not all(
            isinstance(m, dict) and isinstance(m.get("role"), str)
            and isinstance(m.get("content"), str) for m in messages):
        raise RequestError("messages must be a non-empty list of {role, content} strings")
    max_tokens = data.get("max_tokens", DEFAULT_MAX_TOKENS)
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) \
            or not 1 <= max_tokens <= MAX_TOKENS_LIMIT:
        raise RequestError(f"max_tokens must be an integer from 1 to {MAX_TOKENS_LIMIT}")
    seed = data.get("seed")
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
        raise RequestError("seed must be an integer")
    return ([{"role": m["role"], "content": m["content"]} for m in messages],
            max_tokens, seed)


class Service:
    """Answers chat completions one at a time, with or without the constraint."""

    def __init__(self, backend: Backend, *, constrained: bool = True) -> None:
        self.backend = backend
        self.constraint = (TokenConstraint(backend.vocab, backend.eos_ids)
                           if constrained else None)
        self.fingerprint = GRAMMAR_VERSION if constrained else UNCONSTRAINED
        self._lock = threading.Lock()

    def complete(self, messages: list[dict[str, str]], max_tokens: int,
                 seed: int | None) -> dict[str, Any]:
        with self._lock:
            prompt = self.backend.prompt_ids(messages)
            allowed = self.constraint.allowed if self.constraint is not None else None
            ids = self.backend.generate(prompt, max_tokens, seed, allowed)
        eos = self.backend.eos_ids
        finish = "stop" if ids and ids[-1] in eos else "length"
        text = self.backend.decode([i for i in ids if i not in eos])
        if self.constraint is not None and finish == "stop" and not is_complete(text):
            # The mask allows end-of-sequence only after a whole sentence. If the
            # decoded text is not one, the per-token texts and the tokenizer's
            # decode disagree, and the run must stop rather than report it.
            raise ConstraintError(f"decoded reply is not a sentence of the language: {text!r}")
        completion_tokens = sum(1 for i in ids if i not in eos)
        return {
            "id": f"chatcmpl-{time.time_ns()}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": self.backend.name,
            "system_fingerprint": self.fingerprint,
            "choices": [{"index": 0, "finish_reason": finish,
                         "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": len(prompt), "completion_tokens": completion_tokens,
                      "total_tokens": len(prompt) + completion_tokens},
        }


def make_handler(service: Service) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "lab-constrained"

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            if self.path.rstrip("/") not in ("/v1/chat/completions", "/chat/completions"):
                self._send(404, {"error": "only /v1/chat/completions is served"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if not 0 < length <= MAX_BODY_BYTES:
                self._send(400, {"error": "a JSON body up to 1 MiB is required"})
                return
            try:
                messages, max_tokens, seed = parse_request(self.rfile.read(length))
                self._send(200, service.complete(messages, max_tokens, seed))
            except RequestError as exc:
                self._send(400, {"error": str(exc)})
            except ConstraintError as exc:
                self._send(500, {"error": f"constraint: {exc}"})
            except Exception as exc:
                # A backend failure (MLX, the tokenizer) is answered, not dropped,
                # so the run record says why and the server log keeps the trace.
                self.log_error("backend failed: %r", exc)
                self._send(500, {"error": f"backend: {type(exc).__name__}: {exc}"[:500]})

        def log_message(self, format: str, *args: Any) -> None:
            sys.stderr.write(f"{self.log_date_time_string()} {format % args}\n")

    return Handler


def serve(service: Service, host: str, port: int) -> HTTPServer:
    """A server bound to loopback only. The caller runs ``serve_forever``."""
    if host not in LOOPBACK:
        raise ValueError("the server binds to loopback only")
    return HTTPServer((host, port), make_handler(service))


def self_check(backend: Backend) -> list[str]:
    """Problems that would make an H1c run unsound, before any case is sent (empty is good)."""
    problems: list[str] = []
    try:
        constraint = TokenConstraint(backend.vocab, backend.eos_ids)
    except ConstraintError as exc:
        return [str(exc)]
    # With a one-character token for every character of the language, any prefix
    # the mask allows can be finished one character at a time, so the mask can
    # never steer the model into a dead end.
    singles = {text for i, text in enumerate(backend.vocab)
               if len(text) == 1 and i not in backend.eos_ids}
    missing = sorted(ALPHABET - singles)
    if missing:
        problems.append(f"no one-character token for {''.join(missing)!r}: the mask could "
                        "reach a dead end")
    for sentence in sentences():
        path = spell(constraint, sentence)
        if path is None:
            problems.append(f"the vocabulary cannot spell {sentence!r}")
            continue
        # The mask works on each token's text alone; the reply is the tokenizer's
        # decode of the whole sequence. They must agree, or a reply could pass the
        # mask and still not be the sentence the mask allowed.
        decoded = backend.decode([i for i in path if i not in backend.eos_ids])
        if decoded != sentence:
            problems.append(f"decoding the tokens of {sentence!r} gives {decoded!r}")
    return problems


class MLXBackend:  # pragma: no cover - needs Apple silicon, mlx and mlx_lm
    """The H1 and H1b weights through ``mlx_lm``, on the Mac mini only."""

    def __init__(self, model_path: str) -> None:
        import mlx.core as mx  # type: ignore[import-not-found]
        import numpy as np  # type: ignore[import-not-found]
        from mlx_lm import load  # type: ignore[import-not-found]
        from mlx_lm.generate import stream_generate  # type: ignore[import-not-found]
        from mlx_lm.sample_utils import make_sampler  # type: ignore[import-not-found]

        self._mx, self._np = mx, np
        self._stream_generate, self._make_sampler = stream_generate, make_sampler
        self.name: str = model_path
        self.model, self.tokenizer = load(model_path)
        size = len(self.tokenizer.get_vocab())
        self.vocab: list[str] = [str(self.tokenizer.decode([i])) for i in range(size)]
        ids = getattr(self.tokenizer, "eos_token_ids", None) or {self.tokenizer.eos_token_id}
        self.eos_ids: frozenset[int] = frozenset(int(i) for i in ids)

    def prompt_ids(self, messages: list[dict[str, str]]) -> list[int]:
        return list(self.tokenizer.apply_chat_template(messages, add_generation_prompt=True))

    def decode(self, ids: list[int]) -> str:
        return str(self.tokenizer.decode(ids))

    def generate(self, prompt: list[int], max_tokens: int, seed: int | None,
                 allowed: Allowed | None) -> list[int]:
        mx, np = self._mx, self._np
        if seed is not None:
            mx.random.seed(seed)
        processors: list[Callable[[Any, Any], Any]] = []
        if allowed is not None:
            start: list[int] = []

            def mask(tokens: Any, logits: Any) -> Any:
                seen = [] if tokens is None else [int(t) for t in tokens.tolist()]
                if not start:
                    start.append(len(seen))
                bias = np.full(logits.shape[-1], -np.inf, dtype=np.float32)
                bias[allowed(seen[start[0]:])] = 0.0
                return logits + mx.array(bias)

            processors.append(mask)
        out: list[int] = []
        for step in self._stream_generate(self.model, self.tokenizer, prompt,
                                          max_tokens=max_tokens,
                                          sampler=self._make_sampler(temp=0.0),
                                          logits_processors=processors):
            out.append(int(step.token))
            if out[-1] in self.eos_ids:
                break
        return out


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - runs on the Mac mini
    parser = argparse.ArgumentParser(prog="python -m lab.constrained_server",
                                     description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--model", required=True, help="the MLX snapshot path H1 and H1b used")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--unconstrained", action="store_true",
                        help="serve without the constraint, for the equivalence check")
    parser.add_argument("--self-check", action="store_true",
                        help="check the vocabulary can spell every route, then exit")
    args = parser.parse_args(argv)
    backend = MLXBackend(args.model)
    if args.self_check:
        problems = self_check(backend)
        constraint = TokenConstraint(backend.vocab, backend.eos_ids)
        print(f"grammar {GRAMMAR_VERSION}; {len(backend.vocab)} tokens, "
              f"{constraint.candidate_count} candidates, eos {sorted(backend.eos_ids)}")
        for problem in problems:
            print(f"FAIL: {problem}")
        print("PASS" if not problems else "FAIL")
        return 0 if not problems else 1
    service = Service(backend, constrained=not args.unconstrained)
    httpd = serve(service, args.host, args.port)
    print(f"serving {service.fingerprint} on http://{args.host}:{args.port}/v1", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
