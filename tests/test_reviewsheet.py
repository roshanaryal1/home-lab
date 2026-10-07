"""The blinded H1 review sheet (#84): it must not leak the drafted labels."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lab import reviewsheet as rs

DRAFTS = Path(__file__).resolve().parent.parent / "evals" / "shadow_cases_DRAFT.jsonl"


def _case(case_id: str, expected: str, text: str = "A claim") -> dict:
    return {"id": case_id, "expected": expected, "claims": [
        {"text": text, "kind": "finding", "evidence": [
            {"source": "issue-1", "type": "incident", "text": "It happened.",
             "relation": "supports"}]}]}


def _cases() -> list[dict]:
    return [_case(f"secret-{name}", route, f"About {name}") for name, route in
            [("a", "post"), ("b", "blog"), ("c", "paper"), ("d", "post"), ("e", "blog"),
             ("f", "insufficient_evidence")]]


def _write(tmp_path: Path, cases: list[dict]) -> Path:
    path = tmp_path / "cases.jsonl"
    path.write_text("\n".join(json.dumps(c) for c in cases) + "\n")
    return path


def test_the_sheet_leaks_neither_original_ids_nor_drafted_labels() -> None:
    text = rs.render_sheet(rs.blind(_cases()))
    assert "secret-" not in text
    assert "expected" not in text
    body = text.split("## case-01", 1)[1]
    for route in ("post", "blog", "paper", "insufficient_evidence"):
        assert f"Route: {route}" not in body


def test_the_sheet_withholds_the_rubric_thresholds() -> None:
    header = rs.render_sheet(rs.blind(_cases())).split("## case-01")[0].lower()
    assert "three" not in header and "at least 3" not in header
    assert "independent sources" not in header


def test_neutral_ids_are_deterministic_and_the_seed_changes_the_order() -> None:
    first = [(n, c["id"]) for n, c in rs.blind(_cases(), seed=1)]
    assert first == [(n, c["id"]) for n, c in rs.blind(_cases(), seed=1)]
    assert first != [(n, c["id"]) for n, c in rs.blind(_cases(), seed=2)]
    assert [n for n, _ in first] == [f"case-0{i}" for i in range(1, 7)]


def test_input_order_does_not_change_the_result() -> None:
    forward = [(n, c["id"]) for n, c in rs.blind(_cases(), seed=5)]
    backward = [(n, c["id"]) for n, c in rs.blind(list(reversed(_cases())), seed=5)]
    assert forward == backward


def test_exclude_leaves_a_case_out_and_rejects_unknown_ids() -> None:
    blinded = rs.blind(_cases(), exclude=["secret-a"])
    assert len(blinded) == 5 and "secret-a" not in {c["id"] for _, c in blinded}
    with pytest.raises(rs.ReviewError, match="unknown case ids"):
        rs.blind(_cases(), exclude=["nope"])


def test_compare_lists_where_the_reviewer_differs_and_counts_agreement() -> None:
    cases = _cases()
    blinded = rs.blind(cases, seed=3)
    answers = {n: c["expected"] for n, c in blinded}
    assert rs.compare(cases, answers, seed=3)["agree"] == 6
    flipped = next(n for n, c in blinded if c["id"] == "secret-c")
    answers[flipped] = "post"
    result = rs.compare(cases, answers, seed=3)
    assert result["agree"] == 5
    assert result["differences"] == [{"case": "secret-c", "neutral": flipped,
                                      "drafted": "paper", "reviewer": "post"}]
    assert "paper -> post" in result["confusion"]


@pytest.mark.parametrize("claims", [
    [{}], [None], [{"text": 5}], [{"text": "x", "kind": 3}], [{"text": "x", "verified": "yes"}],
    [{"text": "x", "evidence": "none"}], [{"text": "x", "evidence": [None]}],
    [{"text": "x", "evidence": [{"source": "s", "type": "t"}]}],
    [{"text": "x", "evidence": [{"source": "s", "type": "t", "text": "u", "relation": "maybe"}]}]])
def test_malformed_claims_are_refused_with_the_line_before_rendering(
        tmp_path: Path, claims: list) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text(json.dumps({"id": "x", "expected": "post", "claims": claims}) + "\n")
    with pytest.raises(rs.ReviewError, match=r"bad\.jsonl:1"):
        rs.load_cases(path)


@pytest.mark.parametrize("answers", [[], None, "post", 3])
def test_answers_that_are_not_an_object_are_refused(answers: object) -> None:
    with pytest.raises(rs.ReviewError, match="JSON object"):
        rs.compare(_cases(), answers, seed=3)   # type: ignore[arg-type]


def test_the_command_line_reports_a_bad_answers_file_instead_of_crashing(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cases = _write(tmp_path, _cases())
    path = tmp_path / "answers.json"
    path.write_text("[]")
    assert rs.main(["compare", "--cases", str(cases), "--answers", str(path)]) == 1
    assert "JSON object" in capsys.readouterr().err


def test_compare_refuses_incomplete_or_invalid_answers() -> None:
    cases = _cases()
    blinded = rs.blind(cases, seed=3)
    full = {n: c["expected"] for n, c in blinded}
    with pytest.raises(rs.ReviewError, match="no answer"):
        rs.compare(cases, dict(list(full.items())[:-1]), seed=3)
    with pytest.raises(rs.ReviewError, match="no answer"):
        rs.compare(cases, {**full, "case-01": None}, seed=3)
    with pytest.raises(rs.ReviewError, match="not a route name"):
        rs.compare(cases, {**full, "case-01": "banana"}, seed=3)
    with pytest.raises(rs.ReviewError, match="unknown cases"):
        rs.compare(cases, {**full, "case-99": "post"}, seed=3)


def test_load_cases_rejects_bad_files(tmp_path: Path) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"id": "x", "expected": "banana", "claims": []}\n')
    with pytest.raises(rs.ReviewError, match="known expected route"):
        rs.load_cases(bad)
    dup = _write(tmp_path, [_case("same", "post"), _case("same", "blog")])
    with pytest.raises(rs.ReviewError, match="duplicate"):
        rs.load_cases(dup)


def test_the_command_line_writes_the_sheet_and_compares(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cases = _write(tmp_path, _cases())
    out = tmp_path / "out"
    assert rs.main(["sheet", "--cases", str(cases), "--out", str(out), "--seed", "9"]) == 0
    template = json.loads((out / "answers-template.json").read_text())
    assert set(template) == {f"case-0{i}" for i in range(1, 7)}
    assert set(template.values()) == {None}
    assert "Route: ______" in (out / "review-sheet.md").read_text()

    answers = {n: c["expected"] for n, c in rs.blind(_cases(), seed=9)}
    path = tmp_path / "answers.json"
    path.write_text(json.dumps(answers))
    capsys.readouterr()
    assert rs.main(["compare", "--cases", str(cases), "--answers", str(path),
                    "--seed", "9"]) == 0
    assert "agree on 6 of 6 cases" in capsys.readouterr().out
    path.write_text(json.dumps({**answers, "case-01": None}))
    assert rs.main(["compare", "--cases", str(cases), "--answers", str(path),
                    "--seed", "9"]) == 1


@pytest.mark.skipif(not DRAFTS.exists(), reason="the draft case file is not in this checkout")
def test_the_real_drafts_render_without_their_ids() -> None:
    cases = rs.load_cases(DRAFTS)
    text = rs.render_sheet(rs.blind(cases))
    assert len(cases) == 18 and text.count("Route: ______") == 18
    assert not any(case["id"] in text for case in cases)


def test_non_ascii_text_survives_a_non_utf8_locale(tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    # The files are UTF-8 whatever the locale says: force a locale that could not encode them.
    import locale
    monkeypatch.setattr(locale, "getpreferredencoding", lambda *a, **k: "cp1252")
    case = _case("secret-zh", "post", "\u6d4b\u91cf cache \u2014 latency")
    case["claims"][0]["evidence"][0]["text"] = "\u8ba1\u65f6\u5668 ran"
    cases = tmp_path / "cases.jsonl"
    cases.write_text(json.dumps(case, ensure_ascii=False) + "\n", encoding="utf-8")
    out = tmp_path / "out"
    assert rs.main(["sheet", "--cases", str(cases), "--out", str(out)]) == 0
    sheet = (out / "review-sheet.md").read_bytes().decode("utf-8")
    assert "\u6d4b\u91cf cache \u2014 latency" in sheet and "\u8ba1\u65f6\u5668 ran" in sheet


def test_model_and_build_names_are_masked_and_distinct_names_stay_distinct() -> None:
    text = "DWQ beats 4bit; the gguf control; Qwen3-Coder and QWEN; run-dwq vs run-4bit"
    out = rs.mask_text(text)
    for word in ("dwq", "4bit", "gguf", "qwen"):
        assert word not in out.lower()
    # one word gives one label, whatever its case; different words give different labels
    assert len(set(rs.mask_text("dwq DWQ Dwq").split())) == 1
    assert len(set(rs.mask_text("dwq 4bit gguf").split())) == 3


def test_the_rendered_sheet_names_no_model_by_default() -> None:
    cases = [{"id": "c1", "expected": "post", "claims": [{
        "text": "DWQ copies paths correctly", "kind": "measurement", "verified": True,
        "evidence": [{"source": "run-dwq", "type": "measurement", "relation": "supports",
                      "text": "Qwen3-Coder served with MLX"}]}]}]
    blinded = rs.blind(cases)
    sheet = rs.render_sheet(blinded)
    for word in ("dwq", "qwen", "mlx"):
        assert word not in sheet.lower()
    assert "dwq" in rs.render_sheet(blinded, mask=()).lower()      # off only when asked


def test_the_shipped_sheet_names_no_model() -> None:
    sheet = (Path(__file__).resolve().parent.parent / "evals" / "h1_review"
             / "review-sheet.md").read_text().lower()
    assert [t for t in rs.MASK_TERMS if t in sheet] == []


def _write_cases(path: Path, rows: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


UNLABELED = {"id": "new-1", "claims": [{"text": "A thing happened", "kind": "finding",
                                        "evidence": [{"source": "issue-1", "type": "incident",
                                                      "text": "It did."}]}]}


def test_an_unlabeled_case_loads_only_when_asked_and_a_set_label_must_be_a_route(
        tmp_path: Path) -> None:
    f = _write_cases(tmp_path / "u.jsonl", [UNLABELED])
    assert rs.load_cases(f, labeled=False)[0]["id"] == "new-1"
    with pytest.raises(rs.ReviewError):
        rs.load_cases(f)                                   # compare needs a label
    bad = _write_cases(tmp_path / "b.jsonl", [{**UNLABELED, "expected": "nonsense"}])
    with pytest.raises(rs.ReviewError):
        rs.load_cases(bad, labeled=False)
    nulled = _write_cases(tmp_path / "n.jsonl", [{**UNLABELED, "expected": None}])
    assert len(rs.load_cases(nulled, labeled=False)) == 1


def test_the_sheet_command_combines_files_and_refuses_a_shared_id(tmp_path: Path) -> None:
    labeled = _write_cases(tmp_path / "l.jsonl", [{**UNLABELED, "id": "old-1", "expected": "post"}])
    new = _write_cases(tmp_path / "u.jsonl", [UNLABELED])
    out = tmp_path / "out"
    assert rs.main(["sheet", "--cases", str(labeled), str(new), "--out", str(out)]) == 0
    sheet = (out / "review-sheet.md").read_text()
    assert "case-01" in sheet and "case-02" in sheet and "case-03" not in sheet
    clash = _write_cases(tmp_path / "c.jsonl", [{**UNLABELED, "id": "old-1"}])
    assert rs.main(["sheet", "--cases", str(labeled), str(clash), "--out", str(out)]) == 1


def test_compare_still_needs_labels_in_its_one_case_file(tmp_path: Path) -> None:
    f = _write_cases(tmp_path / "u.jsonl", [UNLABELED])
    answers = tmp_path / "a.json"
    answers.write_text(json.dumps({"case-01": "post"}))
    assert rs.main(["compare", "--cases", str(f), "--answers", str(answers)]) == 1
