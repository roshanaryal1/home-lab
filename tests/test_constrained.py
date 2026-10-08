"""H1c constrained decoding (#179, option 2): the grammar, the token mask and the server.

The real server runs on the Mac mini with MLX. Here a fake tokenizer and a
fake model stand in, chosen so that the model's own preference is for text the
candidate would reject ("Hello world"). The tests show the constraint alone
turns that into a reply ``lab.shadow.model_candidate`` accepts, through the
same client ``lab shadow`` uses.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from http.client import HTTPConnection
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from lab import constrained, constrained_server, shadow
from lab.constrained import (
    ALPHABET,
    ROUTES,
    ConstraintError,
    TokenConstraint,
    greedy,
    is_complete,
    is_prefix,
    reachable,
    sentences,
    spell,
)
from lab.model import (
    AdmissionController,
    BoundedModel,
    MockAdapter,
    ModelSpec,
    OpenAICompatibleAdapter,
)

EOS = 0
VOCAB = ["<|end|>", *sorted(ALPHABET),
         '{"', '"route"', ': "', '", "', '"confidence"', ": ", "post", "pa", "per", "blog",
         "0.", "85", "}", "Hello", " world", "", "é", "\n", " {"]
JUNK = ("Hello", " world", "\n", " {", "é")
NUMBERS = st.from_regex(r"0|0\.[0-9]{1,3}|1|1\.0{1,3}", fullmatch=True)
REV = "b" * 40
SPEC = ModelSpec("cand", REV, REV, context_tokens=8192, max_output_tokens=64,
                 weights_mb=100, heavy=False)
CLAIMS = [{"text": "Lease lost after restart", "kind": "finding", "evidence": [
    {"source": "issue-1", "type": "incident", "text": "Lease fencing failed after restart."}]}]


def scores(vocab: Sequence[str] = VOCAB) -> list[float]:
    """The fake model's fixed preference: junk first, then longer tokens, then the rest."""
    return [100.0 if text in JUNK else float(len(text)) for text in vocab]


class FakeBackend:
    def __init__(self, vocab: Sequence[str] = VOCAB, *, sloppy_decode: bool = False,
                 name: str = "cand") -> None:
        self.name = name
        self.vocab = list(vocab)
        self.eos_ids = frozenset({EOS})
        self.prompts: list[list[int]] = []
        self._sloppy = sloppy_decode

    def prompt_ids(self, messages: list[dict[str, str]]) -> list[int]:
        return [len(m["content"]) for m in messages]

    def generate(self, prompt: list[int], max_tokens: int, seed: int | None,
                 allowed: constrained_server.Allowed | None) -> list[int]:
        self.prompts.append(prompt)
        if allowed is None:
            return [self.vocab.index("Hello"), self.vocab.index(" world"), EOS][:max_tokens]
        prefs = scores(self.vocab)
        out: list[int] = []
        for _ in range(max_tokens):
            best = max(allowed(out), key=lambda i: (prefs[i], -i))
            out.append(best)
            if best == EOS:
                break
        return out

    def decode(self, ids: list[int]) -> str:
        text = "".join(self.vocab[i] for i in ids)
        return text.replace(", ", ",") if self._sloppy else text


# ------------------------------------------------------------------ grammar


def test_the_routes_are_the_frozen_instruments_routes() -> None:
    assert ROUTES == shadow.ROUTES


@given(st.sampled_from(ROUTES), NUMBERS)
def test_every_sentence_is_one_the_candidate_accepts(route: str, number: str) -> None:
    text = f'{{"route": "{route}", "confidence": {number}}}'
    assert is_complete(text)
    assert all(is_prefix(text[:cut]) for cut in range(len(text) + 1))
    model = BoundedModel(SPEC, MockAdapter([text], model="cand"))
    proposal = shadow.model_candidate(model)(shadow.parse_case(
        {"id": "c1", "expected": "post", "claims": CLAIMS}))
    assert proposal is not None
    assert proposal.route == route and proposal.confidence == float(number)


@pytest.mark.parametrize("text", [
    '{"route":"post","confidence":1}',            # other spacing
    '{"route": "post", "confidence": 1.5}',       # above 1
    '{"route": "post", "confidence": 2}',
    '{"route": "post", "confidence": 0.1234}',    # four decimals
    '{"route": "post", "confidence": 01}',
    '{"route": "post", "confidence": .5}',
    '{"route": "thread", "confidence": 1}',       # not a route
    '{"route": "Post", "confidence": 1}',
    '{"route": "post", "confidence": 1} ',        # anything after the brace
    '{"route": "post", "confidence": 1',          # no brace
    '{"route": "post", "confidence": 1, "x": 1}',
    '{"confidence": 1, "route": "post"}',         # other key order
    "Hello",
    "",
])
def test_near_misses_are_not_sentences(text: str) -> None:
    assert not is_complete(text)


@given(st.text(alphabet=sorted(ALPHABET), max_size=60))
def test_anything_complete_parses_as_the_candidate_expects(text: str) -> None:
    if is_complete(text):
        obj = json.loads(text)
        assert set(obj) == {"route", "confidence"} and obj["route"] in ROUTES
        assert 0 <= obj["confidence"] <= 1


def test_prefixes_and_dead_ends() -> None:
    assert is_prefix("") and is_prefix('{"ro') and is_prefix('{"route": "p')
    assert is_prefix('{"route": "paper", "confidence": 0.')
    assert not is_prefix(" ") and not is_prefix('{"route": "x')
    assert not is_prefix('{"route": "post", "confidence": 1.1')


# ------------------------------------------------------------- token mask


def test_only_tokens_made_of_the_languages_characters_are_candidates() -> None:
    constraint = TokenConstraint(VOCAB, frozenset({EOS}))
    allowed_texts = {VOCAB[i] for i in constraint.allowed([])}
    assert allowed_texts == {"{", '{"'}
    for junk in ("Hello", " world", "\n", "é", ""):
        assert VOCAB.index(junk) not in constraint._candidates


def test_end_of_sequence_only_after_a_whole_sentence_and_then_nothing_else() -> None:
    constraint = TokenConstraint(VOCAB, frozenset({EOS}))
    whole = sentences()[0]
    assert constraint.allowed_for_text(whole) == [EOS]
    assert EOS not in constraint.allowed_for_text(whole[:-1])


def test_leaving_the_language_or_having_no_eos_is_an_error() -> None:
    constraint = TokenConstraint(VOCAB, frozenset({EOS}))
    with pytest.raises(ConstraintError):
        constraint.allowed([VOCAB.index("Hello")])
    with pytest.raises(ConstraintError):
        TokenConstraint(VOCAB, frozenset())


def test_greedy_decoding_under_the_constraint_ends_in_a_sentence_whatever_the_scores() -> None:
    constraint = TokenConstraint(VOCAB, frozenset({EOS}))
    out = greedy(constraint, scores(), max_tokens=64)
    assert out[-1] == EOS and is_complete(constraint.text(out))


def test_every_route_can_be_spelled_and_a_missing_letter_is_found() -> None:
    constraint = TokenConstraint(VOCAB, frozenset({EOS}))
    for sentence in sentences():
        path = spell(constraint, sentence)
        assert path is not None and path[-1] == EOS
        assert constraint.text(path) == sentence
    no_g = [t for t in VOCAB if "g" not in t]
    blog = next(s for s in sentences() if '"blog"' in s)
    assert not reachable(TokenConstraint(no_g, frozenset({EOS})), blog)


# ------------------------------------------------------------------ server


def test_the_service_answers_with_a_sentence_and_names_its_grammar() -> None:
    service = constrained_server.Service(FakeBackend())
    reply = service.complete([{"role": "user", "content": "route this"}], 64, 0)
    text = reply["choices"][0]["message"]["content"]
    assert is_complete(text) and reply["choices"][0]["finish_reason"] == "stop"
    assert reply["system_fingerprint"] == constrained.GRAMMAR_VERSION
    usage = reply["usage"]
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]


def test_unconstrained_mode_passes_the_model_through_for_the_equivalence_check() -> None:
    service = constrained_server.Service(FakeBackend(), constrained=False)
    reply = service.complete([{"role": "user", "content": "x"}], 64, None)
    assert reply["choices"][0]["message"]["content"] == "Hello world"
    assert reply["system_fingerprint"] == "unconstrained"


def test_a_decode_that_disagrees_with_the_mask_stops_the_reply() -> None:
    service = constrained_server.Service(FakeBackend(sloppy_decode=True))
    with pytest.raises(ConstraintError):
        service.complete([{"role": "user", "content": "x"}], 64, 0)


def test_lab_shadow_gets_a_route_through_the_unchanged_client() -> None:
    backend = FakeBackend()
    httpd = constrained_server.serve(constrained_server.Service(backend), "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{httpd.server_port}/v1"
        model = BoundedModel(SPEC, OpenAICompatibleAdapter(url), AdmissionController())
        proposal = shadow.model_candidate(model)(shadow.parse_case(
            {"id": "c1", "expected": "post", "claims": CLAIMS}))
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert proposal is not None and proposal.route in ROUTES
    assert backend.prompts, "the request reached the model"


def test_the_clients_model_pin_still_refuses_a_server_serving_other_weights() -> None:
    backend = FakeBackend(name="other-weights")
    httpd = constrained_server.serve(constrained_server.Service(backend), "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{httpd.server_port}/v1"
        model = BoundedModel(SPEC, OpenAICompatibleAdapter(url), AdmissionController())
        proposal = shadow.model_candidate(model)(shadow.parse_case(
            {"id": "c1", "expected": "post", "claims": CLAIMS}))
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert proposal is None


@pytest.mark.parametrize(("body", "path", "status"), [
    ({"messages": [{"role": "user", "content": "x"}], "temperature": 0.7}, "/v1/chat/completions",
     400),
    ({"messages": [{"role": "user", "content": "x"}], "stream": True}, "/v1/chat/completions",
     400),
    ({"messages": []}, "/v1/chat/completions", 400),
    ({"messages": [{"role": "user", "content": "x"}], "max_tokens": 0}, "/v1/chat/completions",
     400),
    ({"messages": [{"role": "user", "content": "x"}], "seed": "0"}, "/v1/chat/completions", 400),
    ({"messages": [{"role": "user", "content": "x"}]}, "/v1/completions", 404),
    ({"messages": [{"role": "user", "content": "x"}],
      "response_format": {"type": "text"}}, "/v1/chat/completions", 200),
])
def test_requests_outside_the_fixed_design_are_refused(body: dict[str, object], path: str,
                                                      status: int) -> None:
    httpd = constrained_server.serve(constrained_server.Service(FakeBackend()), "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        conn = HTTPConnection("127.0.0.1", httpd.server_port, timeout=10)
        conn.request("POST", path, body=json.dumps(body),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert resp.status == status


def test_a_backend_failure_is_answered_with_a_500_not_a_dropped_connection() -> None:
    class Broken(FakeBackend):
        def generate(self, prompt: list[int], max_tokens: int, seed: int | None,
                     allowed: constrained_server.Allowed | None) -> list[int]:
            raise RuntimeError("metal out of memory")

    httpd = constrained_server.serve(constrained_server.Service(Broken()), "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        conn = HTTPConnection("127.0.0.1", httpd.server_port, timeout=10)
        conn.request("POST", "/v1/chat/completions", headers={"Content-Type": "application/json"},
                     body=json.dumps({"messages": [{"role": "user", "content": "x"}]}))
        resp = conn.getresponse()
        body = json.loads(resp.read())
        conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert resp.status == 500
    assert body["error"] == "backend: RuntimeError: metal out of memory"


def test_a_body_that_is_not_json_is_refused() -> None:
    with pytest.raises(constrained_server.RequestError):
        constrained_server.parse_request(b"not json")
    with pytest.raises(constrained_server.RequestError):
        constrained_server.parse_request(b"[1]")


def test_the_server_binds_to_loopback_only() -> None:
    with pytest.raises(ValueError):
        constrained_server.serve(constrained_server.Service(FakeBackend()), "0.0.0.0", 0)


def test_the_self_check_passes_a_sound_tokenizer_and_names_each_problem() -> None:
    assert constrained_server.self_check(FakeBackend()) == []
    sloppy = constrained_server.self_check(FakeBackend(sloppy_decode=True))
    assert len(sloppy) == len(ROUTES) and all("decoding" in p for p in sloppy)
    no_g = constrained_server.self_check(FakeBackend([t for t in VOCAB if "g" not in t]))
    blog = next(s for s in sentences() if "blog" in s)
    assert no_g == ["no one-character token for 'g': the mask could reach a dead end",
                    f"the vocabulary cannot spell {blog!r}"]


def test_the_module_does_not_import_mlx() -> None:
    source = Path(constrained_server.__file__).read_text(encoding="utf-8")
    top = source.split("class MLXBackend", 1)[0]
    assert "import mlx" not in top and "from mlx_lm" not in top

