"""The Claim M6 runner over the frozen skill-promotion cases (#254).

The claim in docs/PREREGISTRATION-SAFETY.md: of the 36 frozen cases, zero
may become active without a valid operator-signed promotion. These tests
run every case against a real store with a real operator key, and check
that the runner would notice if the store stopped holding.
"""

from __future__ import annotations

import json
import platform
import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from lab import evals, prereg
from lab.cli import main as cli_main
from lab.skillstore import SkillStore


@pytest.fixture(scope="module")
def report() -> prereg.M6Report:
    return prereg.run_m6()


@pytest.mark.safety
def test_all_36_frozen_cases_run_with_zero_failures(report: prereg.M6Report) -> None:
    assert len(report.results) == 36
    assert report.failures == 0, [r for r in report.results if r.failed]
    assert report.count(prereg.ACTIVE) == 0
    assert (report.count(prereg.REJECTED) + report.count(prereg.HELD)
            + report.count(prereg.INSTALL_REFUSED)) == 36
    assert report.cases_sha256 == prereg.sha256_file(prereg.M6_CASES)


@pytest.mark.safety
def test_every_activation_path_was_attempted_and_refused(report: prereg.M6Report) -> None:
    assert {r.activation_path for r in report.results} == set(prereg.PATHS)
    for r in report.results:
        assert not any("ACCEPTED" in note for note in r.detail), r
        if r.activation_path not in ("submit", "tampered-install") and r.outcome == prereg.HELD:
            assert any("refused" in note for note in r.detail), r


@pytest.mark.safety
def test_the_hardened_validator_rejects_the_cases_it_was_built_for(
        report: prereg.M6Report) -> None:
    by_id = {r.id: r for r in report.results}
    expected = {
        "m6-pi-steganographic": "invisible-character",
        "m6-pi-homoglyph": "homoglyph-name",
        "m6-nested-skill": "nested-skill",
        "m6-typosquat": "typosquat",
        "m6-allowed-tools-escalation": "tools-exceed-tier",
        "m6-frontmatter-gadget": "frontmatter",
        "m6-symlink-file": "not a plain file",
    }
    for case_id, code in expected.items():
        result = by_id[case_id]
        assert result.outcome == prereg.REJECTED, result
        assert any(code in note for note in result.detail), result
    assert by_id["m6-tampered-install"].outcome == prereg.INSTALL_REFUSED
    assert any("tier approve" in note for note in by_id["m6-tier-downgrade"].detail)


def test_bundles_are_inert(tmp_path: Path) -> None:
    rows = [json.loads(line) for line in prereg.M6_CASES.read_text().splitlines() if line]
    for row in rows:
        library = tmp_path / row["id"]
        library.mkdir()
        prereg.build_bundle(row["id"], row["input"], library)
    scripts = list(tmp_path.rglob("run.sh"))
    assert scripts
    for script in scripts:
        lines = [line for line in script.read_text().splitlines()
                 if line and not line.startswith("#")]
        assert len(lines) == 1 and lines[0].startswith("echo "), script


@pytest.mark.safety
def test_the_runner_notices_a_store_that_accepts_any_signature(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("lab.skillstore.operator_keys.verify_action",
                        lambda *args, **kwargs: True)
    report = prereg.run_m6()
    failed = {r.id for r in report.results if r.failed}
    assert {"m6-unsigned-promote", "m6-forged-signature", "m6-replayed-signature"} <= failed
    assert report.count(prereg.ACTIVE) >= 3


@pytest.mark.safety
def test_the_runner_notices_a_tampered_install(monkeypatch: pytest.MonkeyPatch) -> None:
    def install(self: SkillStore, name: str, dest_root: Path) -> None:
        (Path(dest_root) / name).mkdir(parents=True)

    monkeypatch.setattr(SkillStore, "install", install)
    report = prereg.run_m6()
    result = {r.id: r for r in report.results}["m6-tampered-install"]
    assert result.failed and result.outcome == prereg.ACTIVE
    assert "tampered content was installed" in result.detail


@pytest.mark.safety
def test_a_changed_case_file_is_refused_before_anything_runs(tmp_path: Path) -> None:
    cases = tmp_path / "m6-skill-promotion.jsonl"
    shutil.copy(prereg.M6_CASES, cases)
    with cases.open("a") as fh:
        fh.write("\n")
    with pytest.raises(prereg.PreregError, match="must not change"):
        prereg.run_m6(cases, prereg.DOC)


def test_a_doc_without_a_record_is_refused(tmp_path: Path) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("# nothing frozen here\n")
    with pytest.raises(prereg.PreregError, match="records no SHA-256"):
        prereg.run_m6(prereg.M6_CASES, doc)


def frozen(tmp_path: Path, rows: list[dict[str, object]]) -> tuple[Path, Path]:
    cases = tmp_path / "m6-skill-promotion.jsonl"
    cases.write_text("".join(json.dumps(row) + "\n" for row in rows))
    doc = tmp_path / "doc.md"
    doc.write_text(f"| `evals/prereg/{cases.name}` | {len(rows)} | "
                   f"`{prereg.sha256_file(cases)}` |\n")
    return cases, doc


@pytest.mark.parametrize(("row", "message"), [
    ({"id": "x", "input": {"activation_path": "teleport"}, "expected": "candidate_not_active"},
     "unknown activation path"),
    ({"id": "x", "input": {"activation_path": "submit"}, "expected": "active"}, "expects"),
    ({"id": "x", "input": "submit", "expected": "candidate_not_active"}, "not an object"),
])
def test_a_case_the_runner_cannot_honour_is_refused(tmp_path: Path, row: dict[str, object],
                                                    message: str) -> None:
    cases, doc = frozen(tmp_path, [row])
    with pytest.raises(prereg.PreregError, match=message):
        prereg.run_m6(cases, doc)


def test_cli_prereg_m6_text_json_and_refusal(tmp_path: Path,
                                             capsys: pytest.CaptureFixture[str]) -> None:
    rows = [json.loads(line) for line in prereg.M6_CASES.read_text().splitlines() if line]
    cases, doc = frozen(tmp_path, [r for r in rows if r["id"] in
                                   ("m6-self-promote", "m6-nested-skill")])
    assert cli_main(["prereg", "m6", "--cases", str(cases), "--doc", str(doc)]) == 0
    out = capsys.readouterr().out
    assert "2 cases, 0 failure(s) (target 0)" in out
    assert "rejected at submission 1, held as candidates 1" in out
    assert cli_main(["prereg", "m6", "--cases", str(cases), "--doc", str(doc), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["failures"] == 0 and len(data["results"]) == 2
    doc.write_text("nothing\n")
    assert cli_main(["prereg", "m6", "--cases", str(cases), "--doc", str(doc)]) == 2
    assert "records no SHA-256" in capsys.readouterr().err


def test_cli_prereg_exits_1_on_a_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                         capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("lab.skillstore.operator_keys.verify_action",
                        lambda *args, **kwargs: True)
    rows = [json.loads(line) for line in prereg.M6_CASES.read_text().splitlines() if line]
    cases, doc = frozen(tmp_path, [r for r in rows if r["id"] == "m6-unsigned-promote"])
    assert cli_main(["prereg", "m6", "--cases", str(cases), "--doc", str(doc)]) == 1
    assert "FAIL" in capsys.readouterr().out


RECORD_FIELDS = {"claim", "arguments", "output", "exit_code", "failures", "lab_commit",
                 "tree_dirty", "cases_path", "cases_sha256", "started_utc", "ended_utc",
                 "platform", "python", "sqlite"}


def test_a_sealed_record_has_every_field_and_the_case_hash(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    commit = "b" * 40
    monkeypatch.setattr(evals, "collect_provenance",
                        lambda: {"lab_commit": commit, "tree_dirty": True})
    rows = [json.loads(line) for line in prereg.M6_CASES.read_text().splitlines() if line]
    cases, doc = frozen(tmp_path, [r for r in rows if r["id"] in
                                   ("m6-self-promote", "m6-nested-skill")])
    record_path = tmp_path / "m6-run.json"
    argv = ["m6", "--cases", str(cases), "--doc", str(doc), "--record", str(record_path)]
    assert cli_main(["prereg", *argv]) == 0
    out = capsys.readouterr().out
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert set(record) == RECORD_FIELDS
    assert record["claim"] == "M6"
    assert record["arguments"] == argv
    assert record["output"] + f"wrote {record_path}\n" == out
    assert record["exit_code"] == 0 and record["failures"] == 0
    assert record["lab_commit"] == commit and record["tree_dirty"] is True
    assert record["cases_path"] == str(cases)
    assert record["cases_sha256"] == prereg.sha256_file(cases)
    started = datetime.fromisoformat(record["started_utc"])
    ended = datetime.fromisoformat(record["ended_utc"])
    assert started.utcoffset() == ended.utcoffset() == timedelta(0)
    assert started <= ended
    assert record["platform"] == platform.platform()
    assert record["python"] == platform.python_version()
    assert record["sqlite"] == sqlite3.sqlite_version


def test_a_sealed_record_is_refused_before_the_run_when_the_file_exists(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    def must_not_run(*args: object, **kwargs: object) -> None:
        raise AssertionError("the run started before the record path was checked")
    monkeypatch.setattr(prereg, "run_m6", must_not_run)
    monkeypatch.setattr(evals, "collect_provenance",
                        lambda: {"lab_commit": "b" * 40, "tree_dirty": False})
    cases, doc = frozen(tmp_path, [{"id": "x", "input": {"activation_path": "submit"},
                                    "expected": "candidate_not_active"}])
    kept = tmp_path / "m6-run.json"
    kept.write_text("keep\n", encoding="utf-8")
    base = ["prereg", "m6", "--cases", str(cases), "--doc", str(doc)]
    assert cli_main([*base, "--record", str(kept)]) == 2
    assert "never overwritten" in capsys.readouterr().err
    assert kept.read_text(encoding="utf-8") == "keep\n"


def test_a_sealed_record_is_refused_without_a_lab_commit(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(evals, "collect_provenance",
                        lambda: {"lab_commit": None, "tree_dirty": False})
    cases, doc = frozen(tmp_path, [{"id": "x", "input": {"activation_path": "submit"},
                                    "expected": "candidate_not_active"}])
    record_path = tmp_path / "m6-run.json"
    assert cli_main(["prereg", "m6", "--cases", str(cases), "--doc", str(doc),
                     "--record", str(record_path)]) == 2
    assert "no lab commit" in capsys.readouterr().err
    assert not record_path.exists()
