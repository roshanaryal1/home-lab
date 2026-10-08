"""Constrained decoding for the H1c routing run (#179, option 2).

H1c repeats H1b with one change: the candidate's reply is constrained, token
by token, to the one shape ``lab.shadow.model_candidate`` accepts, so the model
can only choose which route and which confidence, never the format. The served
``mlx_lm.server`` cannot do this (it never reads ``response_format``, #179), so
the constraint runs inside our own loopback server
(``lab.constrained_server``), on the same MLX weights as H1 and H1b.

The language is fixed and small::

    {"route": "<route>", "confidence": <number>}

with exactly that spacing, ``<route>`` one of ``ROUTES`` (the five routes of
``lab.shadow``, checked by a test) and ``<number>`` one of ``0``, ``1``,
``0.d``, ``0.dd``, ``0.ddd``, ``1.0``, ``1.00`` or ``1.000``, so every reply
that completes parses and passes the candidate's own checks.

How a token is allowed: every token id has a fixed text (``vocab[i]``). A
token is allowed when the text generated so far plus that token's text is
still a prefix of some sentence of the language. The end-of-sequence token is
allowed only when the text is a complete sentence, and then nothing else is.
Tokens whose text is empty, or uses a character the language never uses, are
never allowed.

This module is pure Python with no MLX import, so the grammar and the token
mask are tested in CI. Applying the mask to MLX logits is the server's job.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

ROUTES: tuple[str, ...] = ("no_artifact", "insufficient_evidence", "post", "blog", "paper")
GRAMMAR_VERSION = "h1c-route-v1"

_PREFIX = '{"route": "'
_MIDDLE = '", "confidence": '
_SUFFIX = "}"
_NUMBER = re.compile(r"0|0\.[0-9]{1,3}|1|1\.0{1,3}")
_NUMBER_PREFIX = re.compile(r"|0|0\.|0\.[0-9]{1,3}|1|1\.|1\.0{1,3}")
ALPHABET = frozenset(_PREFIX + _MIDDLE + _SUFFIX + "".join(ROUTES) + "0123456789.")


class ConstraintError(RuntimeError):
    """The constraint cannot continue: no token is allowed, or the text left the language."""


def _number_then_suffix(rest: str, *, complete: bool) -> bool:
    """Is ``rest`` a number followed by the closing brace (or a prefix of that)?"""
    for cut in range(len(rest) + 1):
        number, tail = rest[:cut], rest[cut:]
        if tail:
            if _NUMBER.fullmatch(number) and (tail == _SUFFIX or (not complete
                                                                 and _SUFFIX.startswith(tail))):
                return True
        elif not complete and _NUMBER_PREFIX.fullmatch(number):
            return True
    return False


def _matches(text: str, *, complete: bool) -> bool:
    if not text.startswith(_PREFIX):
        return not complete and _PREFIX.startswith(text)
    rest = text[len(_PREFIX):]
    for route in ROUTES:
        if rest.startswith(route):
            after = rest[len(route):]
            if after.startswith(_MIDDLE):
                return _number_then_suffix(after[len(_MIDDLE):], complete=complete)
            if not complete and _MIDDLE.startswith(after):
                return True
        elif not complete and route.startswith(rest):
            return True
    return False


def is_prefix(text: str) -> bool:
    """Can ``text`` still grow into a sentence of the language? (A whole sentence counts.)"""
    return _matches(text, complete=False)


def is_complete(text: str) -> bool:
    """Is ``text`` exactly one sentence of the language?"""
    return _matches(text, complete=True)


def sentences() -> list[str]:
    """One sentence per route, for self-checks: the route with confidence ``1``."""
    return [f"{_PREFIX}{route}{_MIDDLE}1{_SUFFIX}" for route in ROUTES]


@dataclass
class TokenConstraint:
    """Which token ids may come next, given the token ids generated so far.

    ``vocab[i]`` is the text of token ``i`` decoded on its own. ``eos_ids`` are
    the end-of-sequence ids. Only tokens whose text is non-empty and made of
    ``ALPHABET`` characters are ever candidates, which keeps each step to a few
    thousand checks instead of the whole vocabulary.
    """

    vocab: Sequence[str]
    eos_ids: frozenset[int]
    _candidates: list[int] = field(init=False, repr=False)
    _cache: dict[str, list[int]] = field(init=False, repr=False, default_factory=dict)

    def __post_init__(self) -> None:
        if not self.eos_ids:
            raise ConstraintError("the tokenizer names no end-of-sequence token")
        self._candidates = [i for i, text in enumerate(self.vocab)
                            if text and i not in self.eos_ids and set(text) <= ALPHABET]

    @property
    def candidate_count(self) -> int:
        return len(self._candidates)

    def text(self, generated: Iterable[int]) -> str:
        """The text of ``generated``, without the end-of-sequence tokens."""
        return "".join(self.vocab[i] for i in generated if i not in self.eos_ids)

    def allowed_for_text(self, text: str) -> list[int]:
        if text in self._cache:
            return self._cache[text]
        if is_complete(text):
            allowed = sorted(self.eos_ids)
        elif not is_prefix(text):
            raise ConstraintError(f"generated text left the language: {text!r}")
        else:
            allowed = [i for i in self._candidates if is_prefix(text + self.vocab[i])]
        if not allowed:
            raise ConstraintError(f"no token can continue {text!r}")
        self._cache[text] = allowed
        return allowed

    def allowed(self, generated: Iterable[int]) -> list[int]:
        """The token ids allowed after ``generated`` (ids only, the prompt excluded)."""
        return self.allowed_for_text(self.text(generated))


def greedy(constraint: TokenConstraint, scores: Sequence[float], max_tokens: int) -> list[int]:
    """Greedy constrained decoding against fixed per-token ``scores``, for tests and self-checks.

    A real model's scores change at every step. Here they do not, which is
    enough to show that the constraint alone, whatever the scores prefer,
    yields a complete sentence or stops at ``max_tokens``.
    """
    generated: list[int] = []
    for _ in range(max_tokens):
        allowed = constraint.allowed(generated)
        best = max(allowed, key=lambda i: (scores[i], -i))
        generated.append(best)
        if best in constraint.eos_ids:
            break
    return generated


def spell(constraint: TokenConstraint, sentence: str) -> list[int] | None:
    """Token ids that spell ``sentence`` one allowed step at a time, or ``None``. (A self-check.)

    Explores every way to cut ``sentence`` into allowed tokens, so a vocabulary
    that cannot spell a route at all is found before any case is sent. The ids
    returned are one such way, ending with an end-of-sequence id.
    """
    paths: dict[str, list[int]] = {"": []}
    frontier = [""]
    while frontier:
        text = frontier.pop()
        if text == sentence:
            eos = constraint.allowed_for_text(text)
            return [*paths[text], eos[0]] if eos == sorted(constraint.eos_ids) else None
        try:
            allowed = constraint.allowed_for_text(text)
        except ConstraintError:
            continue                      # a dead end: this cut cannot finish the sentence
        for i in allowed:
            if i in constraint.eos_ids:
                continue
            grown = text + constraint.vocab[i]
            if sentence.startswith(grown) and grown not in paths:
                paths[grown] = [*paths[text], i]
                frontier.append(grown)
    return None


def reachable(constraint: TokenConstraint, sentence: str) -> bool:
    """Can ``sentence`` be spelled with allowed tokens, step by step?"""
    return spell(constraint, sentence) is not None
