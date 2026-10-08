"""Tests for the memory budget check (#321).

The heavy-model figures are ADR 0001's: 17,180 MB of weights and about 200,000
bytes of KV cache per token. The comparison numbers are arithmetic checks on
made-up readings, not measurements from the Mac mini.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lab import memory_budget as mb
from lab.cli import main
from lab.model import ModelSpec, kv_cache_mb

REV = "a" * 40


def test_the_heavy_spec_carries_the_adr_figures() -> None:
    assert mb.HEAVY_SPEC.weights_mb == 17_180
    assert mb.HEAVY_SPEC.kv_bytes_per_token == 200_000


def test_heavy_prediction_at_8192_tokens_matches_the_hand_calculation() -> None:
    # 200,000 bytes per token times 8,192 tokens is 1,638,400,000 bytes, which is
    # 1,638.4 MB. Adding 17,180 MB of weights gives 18,818.4 MB, rounded down to 18,818.
    assert 200_000 * 8_192 == 1_638_400_000
    assert mb.predict_mb(mb.HEAVY_SPEC, 8_192) == 18_818


def test_cache_term_is_the_admission_figure_before_its_margin() -> None:
    assert kv_cache_mb(mb.HEAVY_SPEC, 8_192) == 1_638
    assert kv_cache_mb(mb.HEAVY_SPEC, 37_000) == 7_400


def test_prediction_adds_no_fixed_overhead() -> None:
    spec = ModelSpec("m", REV, REV, 8_192, 16, 1_000, kv_bytes_per_token=0)
    assert mb.predict_mb(spec, 1) == mb.predict_mb(spec, 8_192) == 1_000


def test_the_largest_allowed_context_predicts_a_finite_mb() -> None:
    # 200,000 bytes times 1,048,576 tokens is 209,715.2 MB, so 209,715 MB of cache,
    # plus 17,180 MB of weights, is 226,895.
    assert mb.predict_mb(mb.HEAVY_SPEC, mb.MAX_CONTEXT_TOKENS) == 226_895


@pytest.mark.parametrize("tokens", [0, -1, 8192.5, True, 1_048_577])
def test_prediction_refuses_a_context_that_is_not_a_positive_whole_number(tokens) -> None:
    with pytest.raises(mb.MemoryBudgetError, match="context_tokens"):
        mb.predict_mb(mb.HEAVY_SPEC, tokens)  # type: ignore[arg-type]


def test_comparison_error_is_predicted_minus_measured_in_mb_and_percent() -> None:
    readings = [mb.Measurement(8_192, 20_000.0, "footprint", "made-up reading"),
                mb.Measurement(37_000, 17_000.0, "rss", "made-up reading")]
    first, second = mb.compare(mb.HEAVY_SPEC, readings)
    assert first.predicted_mb == 18_818
    assert first.error_mb == pytest.approx(18_818 - 20_000)
    assert first.error_percent == pytest.approx(-1_182 / 20_000 * 100)
    assert second.predicted_mb == 24_580
    assert second.error_mb == pytest.approx(24_580 - 17_000)     # positive: prediction high
    assert second.error_percent == pytest.approx(7_580 / 17_000 * 100)


def _point(**changes: object) -> dict[str, object]:
    point: dict[str, object] = {"context_tokens": 8192, "measured_mb": 19_000.0,
                                "what": "footprint", "source": "M6, footprint, 2026-10-09"}
    point.update(changes)
    return point


def _record(point: dict[str, object]) -> str:
    return json.dumps({"measurements": [point]})


@pytest.mark.parametrize(("text", "match"), [
    ("not json", "not valid JSON"),
    ("[]", "top level"),
    ('{"measurements": []}', "non-empty list"),
    ('{"measurements": [], "extra": 1}', "top level"),
    ('{"measurements": [], "measurements": []}', "duplicate key"),
    ('{"measurements": [{"context_tokens": 8192, "measured_mb": NaN, '
     '"what": "footprint", "source": "x"}]}', "not a finite number"),
    (json.dumps({"measurements": [{k: v for k, v in _point().items() if k != "source"}]}),
     r"Missing \['source'\]"),
    (_record({**_point(), "note": "x"}), r"unknown \['note'\]"),
    (_record(_point(context_tokens=True)), "context_tokens must be a positive whole number"),
    (_record(_point(context_tokens=8192.0)), "context_tokens must be a positive whole number"),
    (_record(_point(context_tokens=0)), "context_tokens"),
    ('{"measurements": [{"context_tokens": 1' + "0" * 309 + ', '
     '"measured_mb": 19000.0, "what": "footprint", "source": "x"}]}',
     "context_tokens must be a positive whole number no larger than"),
    (_record(_point(measured_mb="19000")), "measured_mb must be a positive number"),
    (_record(_point(measured_mb=-5.0)), "measured_mb must be a positive number"),
    ('{"measurements": [{"context_tokens": 8192, "measured_mb": 1' + "0" * 400 + ', '
     '"what": "footprint", "source": "x"}]}', "measured_mb must be a positive number"),
    (_record(_point(what="RSS")), "'footprint' or 'rss'"),
    (_record(_point(source="   ")), "source must say where"),
    (_record(_point(source="x" * 501)), "longer than 500 characters"),
])
def test_bad_records_are_refused_with_the_location_of_the_fault(text: str, match: str) -> None:
    with pytest.raises(mb.MemoryBudgetError, match=match):
        mb.parse_record(text, "rec.json")


def test_the_error_names_the_point_that_is_wrong(tmp_path: Path) -> None:
    text = json.dumps({"measurements": [_point(), _point(what="RSS")]})
    with pytest.raises(mb.MemoryBudgetError, match=r"rec\.json: measurements\[1\]: what"):
        mb.parse_record(text, "rec.json")


def test_a_missing_record_file_is_refused_clearly(tmp_path: Path) -> None:
    with pytest.raises(mb.MemoryBudgetError, match="cannot read"):
        mb.read_record(tmp_path / "missing.json")


def test_a_record_round_trips_and_is_never_overwritten(tmp_path: Path) -> None:
    points = [mb.Measurement(8_192, 18_900.5, "footprint", "M6, 2026-10-09")]
    path = tmp_path / "record.json"
    mb.write_record(path, points)
    assert mb.read_record(path) == points
    before = path.read_text(encoding="utf-8")
    with pytest.raises(mb.MemoryBudgetError, match="never overwritten"):
        mb.write_record(path, [mb.Measurement(16_384, 20_000.0, "footprint", "second")])
    assert path.read_text(encoding="utf-8") == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["record.json"]


def test_an_empty_record_is_not_written(tmp_path: Path) -> None:
    with pytest.raises(mb.MemoryBudgetError, match="at least one measurement"):
        mb.write_record(tmp_path / "empty.json", [])
    assert not (tmp_path / "empty.json").exists()


def test_cli_prints_the_three_default_points(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["memory-budget"]) == 0
    rows = [line.split() for line in capsys.readouterr().out.splitlines()]
    assert ["8192", "18818", "within"] in rows
    assert ["16384", "20456", "within"] in rows
    assert ["37000", "24580", "over"] in rows


def test_cli_compares_a_record_with_the_predictions(tmp_path: Path,
                                                    capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "measured.json"
    mb.write_record(path, [mb.Measurement(8_192, 19_327.0, "footprint", "made-up reading")])
    assert main(["memory-budget", "--measurements", str(path)]) == 0
    rows = [line.split() for line in capsys.readouterr().out.splitlines()]
    # predicted 18,818 minus measured 19,327 is -509 MB, or -2.6 percent
    assert ["footprint", "8192", "18818", "19327", "-509", "-2.6"] in rows


def test_cli_refuses_a_bad_record_with_exit_1(tmp_path: Path,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "bad.json"
    path.write_text('{"measurements": []}', encoding="utf-8")
    assert main(["memory-budget", "--measurements", str(path)]) == 1
    assert "memory-budget: " in capsys.readouterr().err


def test_cli_refuses_a_zero_context_with_exit_1(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["memory-budget", "0"]) == 1
    assert "context_tokens" in capsys.readouterr().err


def test_cli_refuses_a_huge_context_record_with_exit_1(tmp_path: Path,
                                                       capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "huge.json"
    path.write_text('{"measurements": [{"context_tokens": 1' + "0" * 309 + ', '
                    '"measured_mb": 19000.0, "what": "footprint", "source": "x"}]}',
                    encoding="utf-8")
    assert main(["memory-budget", "--measurements", str(path)]) == 1
    err = capsys.readouterr().err
    assert "memory-budget: " in err and "context_tokens" in err and "Traceback" not in err


def test_cli_prints_a_record_source_escaped(tmp_path: Path,
                                            capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "measured.json"
    mb.write_record(path, [mb.Measurement(8_192, 19_327.0, "footprint", "M6\x1b[2Jcleared")])
    assert main(["memory-budget", "--measurements", str(path)]) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out and "M6\\u001b[2Jcleared" in out
