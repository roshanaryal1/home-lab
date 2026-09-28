"""Untrusted text goes through a fixed schema (item 4.2, #69).

A warning banner or a provenance field is not protection: the text is
still there for the next component to obey. The protection is that
anything downstream receives an ``Evidence`` value, whose shape is fixed
and small, and never the raw text as a payload, a prompt fragment or a
JSON document to be parsed.

``extract_evidence`` never parses the text and never returns more than
``limit`` characters of it, with control characters and bidirectional
overrides removed so it cannot hide from or spoof a human reviewer.
``validate_evidence`` accepts exactly the fixed keys, so a child task
that was handed extra keys (a tool list, a destination) is refused.
"""

from __future__ import annotations

import unicodedata
from dataclasses import asdict, dataclass
from typing import Any

from lab.origin import SourceType, content_sha256

DEFAULT_LIMIT = 2000
MAX_LIMIT = 8000
EVIDENCE_KEYS = frozenset({"source_type", "source_id", "sha256", "length", "excerpt", "truncated"})
_KEEP = {"\n", "\t"}


@dataclass(frozen=True)
class Evidence:
    source_type: str
    source_id: str
    sha256: str          # of the full original text
    length: int          # characters in the original
    excerpt: str         # bounded and cleaned; data, never instruction
    truncated: bool

    def as_payload(self) -> dict[str, Any]:
        return asdict(self)


def clean(text: str) -> str:
    """Drop control and format characters (including bidi overrides)."""
    return "".join(ch for ch in text
                   if ch in _KEEP or unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co"))


def extract_evidence(text: str, *, source_type: str, source_id: str,
                     limit: int = DEFAULT_LIMIT) -> Evidence:
    SourceType(source_type)
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    cleaned = clean(text)
    return Evidence(
        source_type=source_type, source_id=source_id, sha256=content_sha256(text),
        length=len(text), excerpt=cleaned[:limit], truncated=len(cleaned) > limit,
    )


def validate_evidence(obj: object) -> Evidence:
    """Accept exactly the fixed schema, or raise ``ValueError``."""
    if not isinstance(obj, dict) or set(obj) != EVIDENCE_KEYS:
        raise ValueError("evidence must have exactly the fixed keys")
    if not all(isinstance(obj[k], str) for k in ("source_type", "source_id", "sha256", "excerpt")):
        raise ValueError("evidence text fields must be strings")
    if not isinstance(obj["length"], int) or isinstance(obj["length"], bool) \
            or not isinstance(obj["truncated"], bool):
        raise ValueError("evidence length/truncated have the wrong type")
    SourceType(obj["source_type"])
    if len(obj["sha256"]) != 64 or len(obj["excerpt"]) > MAX_LIMIT:
        raise ValueError("evidence hash or excerpt is malformed")
    if clean(obj["excerpt"]) != obj["excerpt"]:
        raise ValueError("evidence excerpt contains control characters")
    return Evidence(**obj)
