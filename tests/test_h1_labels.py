"""The final H1 labels follow the registered rule exactly (#84, amendment 2).

Every test uses made-up cases and replies; none reads a real reviewer reply.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lab import h1_labels as hl
from lab import reviewsheet as rs

ROOT = Path(__file__).resolve().parent.parent
REVIEW = ROOT / "evals" / "h1_review"


def _case(case_id: str) -> dict:
    return {"id": case_id, "claims": [{"text": f"About {case_id}", "kind": "finding",
                                       "evidence": [{"source": "issue-1", "type": "incident",
                                                     "text": "It happened."}]}]}


def _blinded(n: int, prefix: str) -> list[tuple[str, dict]]:
    return [(f"{prefix}-{i:02d}", _case(f"{prefix}-orig-{i}")) for i in range(1, n + 1)]


def _replies(rows: dict[str, tuple[str, str, str]]) -> dict[str, object]:
    """{neutral: (a, b, c)} as three reviewers' replies."""
    return {name: {cid: votes[i] for cid, votes in rows.items()}
            for i, name in enumerate(("a", "b", "c"))}


def _same(ids: list[str], route: str = "post") -> dict[str, tuple[str, str, str]]:
    return {cid: (route, route, route) for cid in ids}


SPLIT = ("post", "blog", "paper")


def _run(main_rows: dict, spare_rows: dict, n_main: int = 4, n_spare: int = 3) -> dict:
    return hl.final_labels(_blinded(n_main, "case"), _blinded(n_spare, "spare"),
                           _replies(main_rows), _replies(spare_rows), size=n_main)


def test_a_two_to_one_case_keeps_the_majority_route() -> None:
    rows = _same(["case-01", "case-02", "case-03"]) | {"case-04": ("blog", "paper", "blog")}
    result = _run(rows, _same(["spare-01", "spare-02", "spare-03"]))
    assert result["labels"]["case-orig-4"] == "blog"
    assert result["replaced"] == [] and result["testable"]
    assert result["main"] == {"unanimous": 3, "two_to_one": 1, "three_way": 0,
                              "outvoted": {"a": 0, "b": 1, "c": 0}}


def test_split_main_cases_take_the_lowest_unused_spares_in_case_order() -> None:
    rows = _same(["case-01", "case-03"]) | {"case-02": SPLIT, "case-04": SPLIT}
    spares = {"spare-01": ("paper", "paper", "blog"), "spare-02": ("no_artifact",) * 3,
              "spare-03": ("blog",) * 3}
    result = _run(rows, spares)
    assert [(r["case"], r["spare"], r["route"]) for r in result["replaced"]] == [
        ("case-02", "spare-01", "paper"), ("case-04", "spare-02", "no_artifact")]
    assert "case-orig-2" not in result["labels"] and "case-orig-4" not in result["labels"]
    assert result["labels"]["spare-orig-1"] == "paper"
    assert result["spares_unused"] == ["spare-03"]
    assert result["testable"] and len(result["cases"]) == 4
    assert result["replaced"][0]["votes"] == {"a": "post", "b": "blog", "c": "paper"}


def test_a_split_spare_is_skipped_and_the_next_one_is_tried() -> None:
    rows = _same(["case-01", "case-02", "case-03"]) | {"case-04": SPLIT}
    spares = {"spare-01": SPLIT, "spare-02": ("blog", "blog", "post"),
              "spare-03": ("post",) * 3}
    result = _run(rows, spares)
    assert result["spares_skipped"] == [{"spare": "spare-01", "reason": "three-way split"}]
    assert result["replaced"][0]["spare"] == "spare-02"
    assert "spare-orig-1" not in result["labels"]
    assert result["testable"]


def test_when_the_spares_run_out_h1_is_not_testable() -> None:
    rows = {"case-01": SPLIT, "case-02": SPLIT, "case-03": SPLIT, "case-04": ("post",) * 3}
    spares = {"spare-01": ("blog",) * 3, "spare-02": SPLIT, "spare-03": ("paper",) * 3}
    result = _run(rows, spares)
    assert not result["testable"]
    assert result["not_replaced"] == ["case-03"]
    assert result["spares_skipped"] == [{"spare": "spare-02", "reason": "three-way split"}]
    assert len(result["cases"]) == 3


def test_the_final_cases_carry_the_final_route_and_keep_their_claims() -> None:
    rows = _same(["case-01", "case-02", "case-03", "case-04"], "blog")
    result = _run(rows, _same(["spare-01", "spare-02", "spare-03"]))
    assert {c["expected"] for c in result["cases"]} == {"blog"}
    assert result["cases"][0]["claims"] == _case("case-orig-1")["claims"]
    assert result["counts"] == {"blog": 4}


@pytest.mark.parametrize("reply, message", [
    ({"case-01": "post", "case-02": "post", "case-03": "post"}, "no answer for"),
    ({"case-01": "post", "case-02": "post", "case-03": "post", "case-04": "post",
      "case-05": "post"}, "unknown cases"),
    ({"case-01": "post", "case-02": "post", "case-03": "post", "case-04": "memo"},
     "not a route name"),
    (["post"], "JSON object"),
])
def test_a_malformed_reply_stops_the_run(reply: object, message: str) -> None:
    main = _replies(_same(["case-01", "case-02", "case-03", "case-04"]))
    main["c"] = reply
    with pytest.raises(rs.ReviewError, match=message):
        hl.final_labels(_blinded(4, "case"), _blinded(3, "spare"), main,
                        _replies(_same(["spare-01", "spare-02", "spare-03"])))


def test_no_label_is_made_from_fewer_than_three_reviewers() -> None:
    main = _replies(_same(["case-01", "case-02", "case-03", "case-04"]))
    spares = _replies(_same(["spare-01", "spare-02", "spare-03"]))
    del main["c"], spares["c"]
    with pytest.raises(rs.ReviewError, match="exactly 3"):
        hl.final_labels(_blinded(4, "case"), _blinded(3, "spare"), main, spares)


def test_main_and_spare_replies_must_come_from_the_same_reviewers() -> None:
    spares = _replies(_same(["spare-01", "spare-02", "spare-03"]))
    spares["d"] = spares.pop("c")
    with pytest.raises(rs.ReviewError, match="same reviewers"):
        hl.final_labels(_blinded(4, "case"), _blinded(3, "spare"),
                        _replies(_same(["case-01", "case-02", "case-03", "case-04"])), spares)


def test_the_frozen_sheets_rebuild_exactly() -> None:
    """The neutral ids map to the cases the reviewers saw, for both registered sheets."""
    main = hl._blinded([ROOT / "evals" / "shadow_cases_DRAFT.jsonl",
                        REVIEW / "extra-cases-UNLABELED.jsonl"],
                       ["d-paper-one-source", "d-paper-control-contradicts"], "case",
                       REVIEW / "review-sheet.md")
    spares = hl._blinded([REVIEW / "spare-cases-UNLABELED.jsonl"], [], "spare",
                         REVIEW / "spares" / "review-sheet.md")
    assert len(main) == 30 and len(spares) == 8


def test_a_sheet_that_differs_is_refused(tmp_path: Path) -> None:
    sheet = tmp_path / "review-sheet.md"
    sheet.write_text((REVIEW / "spares" / "review-sheet.md").read_text() + "extra\n")
    with pytest.raises(rs.ReviewError, match="differs"):
        hl._blinded([REVIEW / "spare-cases-UNLABELED.jsonl"], [], "spare", sheet)


@pytest.fixture(autouse=True)
def _four_case_study(monkeypatch: pytest.MonkeyPatch) -> None:
    """The command's made-up study has four main cases, so four is its registered size."""
    monkeypatch.setattr(hl, "REGISTERED_SIZE", 4)


def _cli_files(tmp_path: Path, spare_route: str = "post") -> list[str]:
    cases = tmp_path / "cases.jsonl"
    spares = tmp_path / "spares.jsonl"
    cases.write_text("".join(json.dumps(_case(f"m-{i}")) + "\n" for i in range(4)))
    spares.write_text("".join(json.dumps(_case(f"s-{i}")) + "\n" for i in range(2)))
    args = ["--cases", str(cases), "--spares", str(spares)]
    for name, path, prefix in (("sheet", cases, "case"), ("spare-sheet", spares, "spare")):
        blinded = rs.blind(rs.load_cases(path, labeled=False), prefix=prefix)
        out = tmp_path / f"{name}.md"
        out.write_text(rs.render_sheet(blinded) + "\n")
        args += [f"--{name}", str(out)]
        flag = "--answers" if prefix == "case" else "--spare-answers"
        args.append(flag)
        for reviewer in ("a", "b", "c"):
            reply = tmp_path / f"{reviewer}-{prefix}.json"
            route = "blog" if prefix == "case" else spare_route
            reply.write_text(json.dumps({n: route for n, _ in blinded}))
            args.append(f"{reviewer}={reply}")
    return [*args, "--out", str(tmp_path / "final.jsonl"), "--report",
            str(tmp_path / "report.json")]


def test_the_command_writes_a_case_file_lab_shadow_can_load(tmp_path: Path,
                                                             capsys: pytest.CaptureFixture[str],
                                                             ) -> None:
    from lab import shadow

    assert hl.main(_cli_files(tmp_path)) == 0
    loaded = shadow.load_cases(tmp_path / "final.jsonl")
    assert sorted(c.id for c in loaded) == ["m-0", "m-1", "m-2", "m-3"]
    assert {c.expected for c in loaded} == {"blog"}
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["testable"] and len(report["sha256"]) == 64
    assert report["sha256"] in capsys.readouterr().out


def test_the_command_refuses_to_overwrite_final_labels(tmp_path: Path,
                                                       capsys: pytest.CaptureFixture[str]) -> None:
    args = _cli_files(tmp_path)
    assert hl.main(args) == 0
    assert hl.main(args) == 1
    assert "written once" in capsys.readouterr().err


def test_the_command_rejects_a_reply_without_a_name(tmp_path: Path,
                                                    capsys: pytest.CaptureFixture[str]) -> None:
    args = _cli_files(tmp_path)
    index = args.index("--answers") + 1
    args[index] = args[index].split("=", 1)[1]
    assert hl.main(args) == 1
    assert "NAME=PATH" in capsys.readouterr().err


def test_the_command_rejects_a_reviewer_given_twice(tmp_path: Path,
                                                    capsys: pytest.CaptureFixture[str]) -> None:
    args = _cli_files(tmp_path)
    index = args.index("--answers") + 2
    args[index] = "a=" + args[index].split("=", 1)[1]
    assert hl.main(args) == 1
    assert "given twice" in capsys.readouterr().err


def test_the_command_says_when_h1_is_not_testable(tmp_path: Path,
                                                  capsys: pytest.CaptureFixture[str]) -> None:
    args = _cli_files(tmp_path)
    reply = Path(args[args.index("--answers") + 1].split("=", 1)[1])
    for name, route in (("a", "post"), ("b", "paper")):
        votes = json.loads(reply.read_text())
        votes["case-01"] = route
        (tmp_path / f"{name}-case.json").write_text(json.dumps(votes))
    spare = Path(args[args.index("--spare-answers") + 1].split("=", 1)[1])
    votes = json.loads(spare.read_text())
    for name, route in (("a", "paper"), ("b", "blog"), ("c", "no_artifact")):
        (tmp_path / f"{name}-spare.json").write_text(json.dumps(dict.fromkeys(votes, route)))
    assert hl.main(args) == 0
    assert "not testable" in capsys.readouterr().err
    report = json.loads((tmp_path / "report.json").read_text())
    assert not report["testable"]
    assert not (tmp_path / "final.jsonl").exists()
    assert report["file"] == str(tmp_path / "final.EXPLORATORY.jsonl")
    assert (tmp_path / "final.EXPLORATORY.jsonl").exists()


def test_the_registered_size_is_thirty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()
    assert hl.REGISTERED_SIZE == 30
    rows = _same(["case-01", "case-02", "case-03", "case-04"])
    result = hl.final_labels(_blinded(4, "case"), _blinded(3, "spare"), _replies(rows),
                             _replies(_same(["spare-01", "spare-02", "spare-03"])))
    assert not result["testable"]


def test_a_reply_that_answers_a_case_twice_is_refused(tmp_path: Path,
                                                      capsys: pytest.CaptureFixture[str]) -> None:
    args = _cli_files(tmp_path)
    reply = Path(args[args.index("--answers") + 1].split("=", 1)[1])
    reply.write_text(reply.read_text()[:-1] + ', "case-01": "paper"}')
    assert hl.main(args) == 1
    assert "more than once" in capsys.readouterr().err


def test_out_and_report_must_differ(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = _cli_files(tmp_path)
    args[args.index("--report") + 1] = args[args.index("--out") + 1]
    assert hl.main(args) == 1
    assert "different files" in capsys.readouterr().err


def test_a_failed_report_write_leaves_no_case_file(tmp_path: Path,
                                                    capsys: pytest.CaptureFixture[str]) -> None:
    args = _cli_files(tmp_path)
    args[args.index("--report") + 1] = str(tmp_path / "missing-dir" / "report.json")
    assert hl.main(args) == 1
    assert not (tmp_path / "final.jsonl").exists()
    args[args.index("--report") + 1] = str(tmp_path / "report.json")
    assert hl.main(args) == 0


def test_case_files_that_share_an_id_are_refused(tmp_path: Path) -> None:
    one, two = tmp_path / "one.jsonl", tmp_path / "two.jsonl"
    one.write_text(json.dumps(_case("same")) + "\n")
    two.write_text(json.dumps(_case("same")) + "\n")
    with pytest.raises(rs.ReviewError, match="share a case id"):
        hl._blinded([one, two], [], "case", tmp_path / "sheet.md")


def test_a_sheet_with_other_line_endings_is_refused(tmp_path: Path) -> None:
    sheet = tmp_path / "review-sheet.md"
    sheet.write_bytes((REVIEW / "spares" / "review-sheet.md").read_bytes().replace(b"\n", b"\r\n"))
    with pytest.raises(rs.ReviewError, match="differs"):
        hl._blinded([REVIEW / "spare-cases-UNLABELED.jsonl"], [], "spare", sheet)


def test_a_case_the_ledger_cannot_build_is_replaced_like_a_split() -> None:
    rows = _same(["case-01", "case-03", "case-04"]) | {"case-02": SPLIT}
    spares = {"spare-01": ("blog",) * 3, "spare-02": ("paper",) * 3, "spare-03": ("post",) * 3}
    no = {"case-orig-1", "spare-orig-1"}
    result = hl.final_labels(_blinded(4, "case"), _blinded(3, "spare"), _replies(rows),
                             _replies(spares), size=4, builds=lambda c: c["id"] not in no)
    assert [(r["case"], r["reason"], r["spare"]) for r in result["replaced"]] == [
        ("case-01", "cannot be built", "spare-02"), ("case-02", "three-way split", "spare-03")]
    assert result["spares_skipped"] == [{"spare": "spare-01", "reason": "cannot be built"}]
    assert result["unbuildable"] == ["case-01", "spare-01"]
    assert "case-orig-1" not in result["labels"] and result["testable"]


def test_the_ledger_check_finds_a_verified_claim_with_one_source() -> None:
    cases = {c["id"]: c for c in rs.load_cases(REVIEW / "extra-cases-UNLABELED.jsonl",
                                                labeled=False)}
    assert not hl.ledger_builds(cases["x-home-readable"])
    assert hl.ledger_builds(cases["x-watchdog-gap"])


def test_without_a_builds_check_the_ledger_is_asked() -> None:
    one_source = {"id": "case-orig-1", "claims": [{"text": "Checked", "kind": "finding",
                  "verified": True, "evidence": [{"source": "a", "type": "incident",
                                                  "text": "Once."}]}]}
    main = [("case-01", one_source), *_blinded(4, "case")[1:]]
    rows = _same(["case-01", "case-02", "case-03", "case-04"])
    result = hl.final_labels(main, _blinded(3, "spare"), _replies(rows),
                             _replies(_same(["spare-01", "spare-02", "spare-03"])), size=4)
    assert result["unbuildable"] == ["case-01"]
    assert result["replaced"][0]["spare"] == "spare-01" and result["testable"]
