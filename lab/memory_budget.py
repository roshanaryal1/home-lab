"""Predict the heavy model's resident memory and check it against measurements (#321).

P3's definition of done (docs/PLAN.md section 3.2) asks for memory budget
predictions checked against measured memory. ``lab memory-budget`` prints the
predictions for context lengths. With ``--measurements FILE`` it also compares
them with a record of readings taken on the Mac mini.

The prediction is the weights plus the KV cache for the context length::

    predicted MB = weights_mb + kv_bytes_per_token * tokens / 1_000_000

ADR 0001 states no fixed overhead beyond the weights, so none is added. The
cache term is ``lab.model.kv_cache_mb``, the figure admission uses before its
one MB margin. It is rounded down to whole MB.

Units. MB here is 10**6 bytes, the unit ``lab.model`` uses. ``footprint``
prints GiB (2**30 bytes), and one GiB is 1073.741824 MB. Its output is whole
GiB, so each reading is good to plus or minus 0.5 GiB, about 537 MB.

What is compared. The KV cache lives in Metal allocations, which RSS did not
show (ADR 0001). So the prediction is meant to match ``footprint``. ``rss`` is
accepted so the gap can be shown. An RSS error is expected to grow with context.

A measurement record is JSON with one key, "measurements", holding a non-empty
list. Every point has exactly these four keys::

    {"measurements": [
      {"context_tokens": 8192, "measured_mb": 19327.0,
       "what": "footprint", "source": "Mac mini M6, footprint, pid 4242, 2026-10-09"}
    ]}

Bad input is refused with the location of the fault. ``write_record`` writes a
new file only, through ``lab.sealed.write_new``, and never overwrites one.

How to measure on the Mac mini

The method follows ADR 0001. Start
``mlx_lm.server`` on loopback with ``--prompt-cache-size 1``. For each context
length, start a fresh server, let the weights load, and read the footprint with
no request. Then send one request with that many prompt tokens, wait for it to
finish, and read the footprint again. Read the footprint of the server process
with ``footprint -p <pid>`` (UNVERIFIED: the repo's docs say only "footprint",
not this flag). Convert GiB to MB by multiplying by 1073.741824, and write the
points into a record.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from lab.model import HEAVY_MODEL_WEIGHTS_MB, ModelSpec, kv_cache_mb
from lab.sealed import write_new

DEFAULT_CONTEXTS: tuple[int, ...] = (8192, 16384, 37000)
KINDS = ("footprint", "rss")
POINT_KEYS = frozenset({"context_tokens", "measured_mb", "what", "source"})
MAX_SOURCE_CHARS = 500
# Far above any context this lab runs. It keeps every prediction a finite float.
MAX_CONTEXT_TOKENS = 1_048_576

# The heavy model as ADR 0001 measured it: the plain 4-bit build, whose footprint
# table is in the ADR. The DWQ build the server runs since 2026-09-30 reported the
# same 16 GiB footprint (ADR 0001), so the same prediction applies to it.
# The public Hugging Face commit of that build, as in ADR 0001 and PREREGISTRATION.md.
HEAVY_REVISION = "6e302ea604ad9ab206367e2c501d1571023e7b6d"
HEAVY_SPEC = ModelSpec(
    name="mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit",
    revision=HEAVY_REVISION,
    tokenizer_revision=HEAVY_REVISION,
    context_tokens=16_384,
    max_output_tokens=1024,
    weights_mb=HEAVY_MODEL_WEIGHTS_MB,
)


class MemoryBudgetError(ValueError):
    """A context length, a measurement or a record this module refuses."""


@dataclass(frozen=True)
class Measurement:
    """One reading: the resident MB measured at a context length."""

    context_tokens: int
    measured_mb: float
    what: str                  # "footprint" or "rss"
    source: str                # where the reading came from: machine, command, date

    def __post_init__(self) -> None:
        _context_tokens(self.context_tokens)
        measured = self.measured_mb
        if isinstance(measured, bool) or not isinstance(measured, (int, float)) \
                or not _finite(measured) or measured <= 0:
            raise MemoryBudgetError(
                f"measured_mb must be a positive number of MB, not {measured!r}")
        if self.what not in KINDS:
            raise MemoryBudgetError(f"what must be 'footprint' or 'rss', not {self.what!r}")
        if not isinstance(self.source, str) or not self.source.strip():
            raise MemoryBudgetError("source must say where the reading came from, and not be empty")
        if len(self.source) > MAX_SOURCE_CHARS:
            raise MemoryBudgetError(f"source is longer than {MAX_SOURCE_CHARS} characters")


@dataclass(frozen=True)
class Comparison:
    measurement: Measurement
    predicted_mb: int
    error_mb: float            # predicted minus measured. Positive means the prediction is high
    error_percent: float       # error_mb as a percent of the measured value


def _finite(value: float) -> bool:
    """math.isfinite, except an int too large for a float is not finite either."""
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise MemoryBudgetError(f"{name} must be a positive whole number, not {value!r}")
    return value


def _context_tokens(value: object) -> int:
    """A context length: a positive whole number no larger than MAX_CONTEXT_TOKENS."""
    tokens = _positive_int(value, "context_tokens")
    if tokens > MAX_CONTEXT_TOKENS:
        raise MemoryBudgetError(
            f"context_tokens must be a positive whole number no larger than {MAX_CONTEXT_TOKENS}")
    return tokens


def predict_mb(spec: ModelSpec, context_tokens: int) -> int:
    """Predicted resident MB at ``context_tokens``: the weights plus the KV cache for that
    many tokens, rounded down to whole MB. No fixed overhead, since ADR 0001 states none."""
    tokens = _context_tokens(context_tokens)
    return spec.weights_mb + kv_cache_mb(spec, tokens)


def compare(spec: ModelSpec, measurements: Sequence[Measurement]) -> list[Comparison]:
    """Each measurement next to the prediction at its own context length."""
    out: list[Comparison] = []
    for measurement in measurements:
        predicted = predict_mb(spec, measurement.context_tokens)
        error = predicted - measurement.measured_mb
        out.append(Comparison(measurement, predicted, error,
                              100.0 * error / measurement.measured_mb))
    return out


def _refuse_constant(name: str) -> Any:
    raise MemoryBudgetError(f"{name} is not a finite number")


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise MemoryBudgetError(f"duplicate key {key!r}")
        out[key] = value
    return out


def parse_record(text: str, origin: str = "record") -> list[Measurement]:
    """The measurements in a record's text. Anything else is refused, with where it went wrong."""
    try:
        data = json.loads(text, object_pairs_hook=_no_duplicates,
                          parse_constant=_refuse_constant)
    except MemoryBudgetError:
        raise
    except (ValueError, RecursionError) as exc:
        raise MemoryBudgetError(f"{origin} is not valid JSON: {exc}") from None
    if not isinstance(data, dict) or set(data) != {"measurements"}:
        raise MemoryBudgetError(
            f'{origin}: the top level must be an object with one key, "measurements"')
    points = data["measurements"]
    if not isinstance(points, list) or not points:
        raise MemoryBudgetError(f'{origin}: "measurements" must be a non-empty list')
    out: list[Measurement] = []
    for index, raw in enumerate(points):
        where = f"{origin}: measurements[{index}]"
        if not isinstance(raw, dict):
            raise MemoryBudgetError(f"{where} must be an object")
        missing = sorted(POINT_KEYS - set(raw))
        unknown = sorted(set(raw) - POINT_KEYS)
        if missing or unknown:
            raise MemoryBudgetError(
                f"{where} needs exactly {sorted(POINT_KEYS)}. Missing {missing}, "
                f"unknown {unknown}")
        try:
            out.append(Measurement(raw["context_tokens"], raw["measured_mb"], raw["what"],
                                   raw["source"]))
        except MemoryBudgetError as exc:
            raise MemoryBudgetError(f"{where}: {exc}") from None
    return out


def read_record(path: Path) -> list[Measurement]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise MemoryBudgetError(f"cannot read {path}: {exc}") from None
    return parse_record(text, str(path))


def record_text(measurements: Sequence[Measurement]) -> str:
    if not measurements:
        raise MemoryBudgetError("a record needs at least one measurement")
    body = {"measurements": [asdict(m) for m in measurements]}
    return json.dumps(body, indent=2) + "\n"


def write_record(path: Path, measurements: Sequence[Measurement]) -> None:
    """Write a new record at ``path``. Never overwrites: ``write_new`` links the file into
    place, and a link fails when the name already exists."""
    text = record_text(measurements)
    try:
        write_new(Path(path), text)
    except FileExistsError:
        raise MemoryBudgetError(f"{path} already exists. A record is never overwritten") from None
    except OSError as exc:
        raise MemoryBudgetError(f"cannot write {path}: {exc}") from None
