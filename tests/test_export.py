"""``lab export`` (#381): memory, the action log and task results, read-only.

The database is only read. Each test checks that its file is byte for byte
the same afterwards and that no other connection saw a write.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

import lab.export as export_mod
from lab.audit import event_hash
from lab.cli import main
from lab.export import ExportError, ExportReport, export
from lab.memory import Memory
from lab.queue import TaskQueue

SHA = hashlib.sha256(b"source").hexdigest()
NOW = datetime(2026, 10, 9, 12, 30, 5, tzinfo=UTC)
FOLDER = "lab-export-20261009T123005Z"
TABLES = ("memories", "memory_uses", "memory_proposals", "events", "tasks")
HOSTILE = "[x](http://evil) <script>"


@pytest.fixture()
def lab_db(tmp_path: Path) -> Path:
    db = tmp_path / "lab.db"
    with TaskQueue(db, owner="test") as queue:
        memory = Memory(queue._conn)
        memory.add_curated("The heavy model slot is exclusive", "ADR 0001", "roshan")
        memory.add_curated("A fact with [a](http://evil) and <i>tags</i>", "ADR 0002", "roshan")
        memory.add_evidence("A source note on slots", "https://a.example", SHA, "researcher")
        memory.search("source slots", task_id="task-1")
        retired = memory.add_evidence("An old note", "https://b.example", SHA, "researcher")
        memory.revoke(retired, "roshan", "stale")

        done = queue.add_task(f"Summarise {HOSTILE} report", {"url": "https://a.example",
                                                             "pages": 2})
        leased = queue.lease()
        assert leased is not None and leased.id == done and leased.lease is not None
        queue.start(leased.lease)
        queue.succeed(leased.lease, {"summary": "three findings"})
        queue.add_task("second task", {})
        cancelled = queue.add_task("never runs")
        queue.cancel(cancelled, "not needed")
    return db


@pytest.fixture()
def out(tmp_path: Path) -> Path:
    folder = tmp_path / "out"
    folder.mkdir()
    return folder


def _counts(db: Path) -> dict[str, int]:
    conn = sqlite3.connect(db)
    try:
        return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in TABLES}
    finally:
        conn.close()


def _rows(db: Path, sql: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _export(db: Path, out: Path) -> ExportReport:
    return export(db, out, now=NOW)


# ------------------------------------------------------------------ counts and shape


def test_counts_match_the_tables(lab_db: Path, out: Path) -> None:
    report = _export(lab_db, out)
    counts = _counts(lab_db)
    assert report.memory_items == counts["memories"] == 4
    assert report.events == counts["events"] > 0
    assert report.tasks == counts["tasks"] == 3
    assert report.folder == out / FOLDER and report.folder.is_dir()


def test_the_folder_holds_exactly_the_five_files(lab_db: Path, out: Path) -> None:
    report = _export(lab_db, out)
    names = sorted(path.name for path in report.folder.iterdir())
    assert names == ["events.jsonl", "memory.json", "memory.md", "tasks.json", "tasks.md"]


def test_json_files_round_trip_the_rows(lab_db: Path, out: Path) -> None:
    report = _export(lab_db, out)
    memory = json.loads((report.folder / "memory.json").read_text(encoding="utf-8"))
    rows = _rows(lab_db, "SELECT * FROM memories ORDER BY id")
    assert [item["id"] for item in memory] == [row["id"] for row in rows]
    for item, row in zip(memory, rows, strict=True):
        columns = row.keys()
        assert [item[name] for name in columns] == list(row)

    tasks = json.loads((report.folder / "tasks.json").read_text(encoding="utf-8"))
    task_rows = _rows(lab_db, "SELECT * FROM tasks ORDER BY created_at, id")
    assert [task["id"] for task in tasks] == [row["id"] for row in task_rows]
    for task, row in zip(tasks, task_rows, strict=True):
        assert set(task) == set(row.keys())
        assert task["payload"] == json.loads(row["payload"])
        expected_result = None if row["result"] is None else json.loads(row["result"])
        assert task["result"] == expected_result


def test_events_jsonl_has_one_object_per_event_row(lab_db: Path, out: Path) -> None:
    report = _export(lab_db, out)
    lines = (report.folder / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == _counts(lab_db)["events"]
    rows = _rows(lab_db, "SELECT * FROM events ORDER BY id")
    for line, row in zip(lines, rows, strict=True):
        event = json.loads(line)
        assert set(event) == set(row.keys())
        assert event["id"] == row["id"] and event["kind"] == row["kind"]
        assert event["hash"] == row["hash"] and event["prev_hash"] == row["prev_hash"]
        assert event["detail"] == row["detail"], "the stored text, not a parsed copy"


def test_the_hash_chain_can_be_checked_from_events_jsonl(lab_db: Path, out: Path) -> None:
    report = _export(lab_db, out)
    events = [json.loads(line) for line in
              (report.folder / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(e["detail"] is not None for e in events), "the fixture has events with detail"
    prev = events[0]["prev_hash"]
    for event in events:
        assert event["prev_hash"] == prev
        assert event_hash(prev, event["task_id"], event["kind"], event["from_state"],
                          event["to_state"], event["detail"], event["created_at"]) == event["hash"]
        prev = event["hash"]


def test_memory_json_carries_provenance_status_and_readers(lab_db: Path, out: Path) -> None:
    report = _export(lab_db, out)
    items = json.loads((report.folder / "memory.json").read_text(encoding="utf-8"))
    by_text = {item["text"]: item for item in items}
    note = by_text["A source note on slots"]
    assert note["source_id"] == "https://a.example" and note["source_sha256"] == SHA
    assert note["created_by"] == "researcher" and note["created_at"]
    assert note["trust"] == "untrusted" and note["state"] == "active"
    assert note["read_by_tasks"] == ["task-1"]
    retired = [item for item in items if item["state"] == "revoked"]
    assert len(retired) == 1
    assert retired[0]["ended_by"] == "roshan" and retired[0]["ended_reason"] == "stale"
    assert retired[0]["read_by_tasks"] == []
    assert all(item["proposal_id"] is None for item in items)


# -------------------------------------------------------------------- read only


def test_the_database_is_not_changed(lab_db: Path, out: Path) -> None:
    before = lab_db.read_bytes()
    watcher = sqlite3.connect(lab_db)
    try:
        version = watcher.execute("PRAGMA data_version").fetchone()[0]
        counts = _counts(lab_db)
        _export(lab_db, out)
        assert lab_db.read_bytes() == before
        assert watcher.execute("PRAGMA data_version").fetchone()[0] == version
        assert _counts(lab_db) == counts
    finally:
        watcher.close()


def test_a_file_that_is_not_a_database_is_refused_and_no_folder_is_made(
        tmp_path: Path, out: Path) -> None:
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not a database\n" * 200)
    with pytest.raises(ExportError, match="cannot read"):
        _export(junk, out)
    assert list(out.iterdir()) == []


def test_a_missing_database_is_refused_and_not_created(tmp_path: Path, out: Path) -> None:
    missing = tmp_path / "missing.db"
    with pytest.raises(ExportError, match="no database"):
        _export(missing, out)
    assert not missing.exists() and list(out.iterdir()) == []



def test_a_path_with_uri_delimiters_is_read_and_nothing_else_is_made(
        lab_db: Path, tmp_path: Path, out: Path) -> None:
    odd = tmp_path / "a?b#c%41" / "lab.db"
    odd.parent.mkdir()
    lab_db.rename(odd)
    before = sorted(p.name for p in tmp_path.iterdir())
    digest = hashlib.sha256(odd.read_bytes()).hexdigest()
    report = _export(odd, out)
    assert report.tasks == 3
    assert sorted(p.name for p in tmp_path.iterdir()) == before, "no other file was made"
    assert hashlib.sha256(odd.read_bytes()).hexdigest() == digest

# ------------------------------------------------------------------ permissions


def test_the_folder_is_700_and_every_file_600(lab_db: Path, out: Path) -> None:
    report = _export(lab_db, out)
    assert stat.S_IMODE(report.folder.stat().st_mode) == 0o700
    for path in report.folder.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path.name


def test_an_existing_folder_is_refused_and_left_alone(lab_db: Path, out: Path) -> None:
    report = _export(lab_db, out)
    marker = report.folder / "memory.md"
    before = marker.read_bytes()
    with pytest.raises(ExportError, match="already exists"):
        _export(lab_db, out)
    assert marker.read_bytes() == before


def test_a_failed_write_leaves_no_partial_folder(lab_db: Path, out: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    real_write = export_mod._write_file
    calls = {"n": 0}

    def fail_second(path: Path, text: str) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        real_write(path, text)

    monkeypatch.setattr(export_mod, "_write_file", fail_second)
    with pytest.raises(ExportError, match="disk full"):
        _export(lab_db, out)
    assert list(out.iterdir()) == []


# ---------------------------------------------------------- hostile text and Markdown


def test_a_hostile_title_is_escaped_in_tasks_md(lab_db: Path, out: Path) -> None:
    text = (_export(lab_db, out).folder / "tasks.md").read_text(encoding="utf-8")
    assert HOSTILE not in text
    assert r"\[x\]\(http://evil\) \<script\>" in text


def test_a_hostile_memory_is_escaped_in_memory_md(lab_db: Path, out: Path) -> None:
    text = (_export(lab_db, out).folder / "memory.md").read_text(encoding="utf-8")
    assert "[a](http://evil)" not in text and "<i>" not in text
    assert r"\[a\]\(http://evil\) and \<i\>tags\</i\>" in text


def test_a_title_cannot_start_a_heading_or_a_new_line(tmp_path: Path, out: Path) -> None:
    db = tmp_path / "newline.db"
    with TaskQueue(db, owner="test") as queue:
        queue.add_task("one\n# two\r\n- three‮end")
    text = (_export(db, out).folder / "tasks.md").read_text(encoding="utf-8")
    assert "\n# two" not in text and "\n- three" not in text
    assert "‮" not in text and "\r" not in text
    assert r"## one \# two \- three" in text


def test_json_keeps_bidi_overrides_visible_as_escapes(tmp_path: Path, out: Path) -> None:
    db = tmp_path / "bidi.db"
    with TaskQueue(db, owner="test") as queue:
        queue.add_task("reads‮fdp.exe", {"note": "a‮b"})
    folder = _export(db, out).folder
    raw = (folder / "tasks.json").read_text(encoding="utf-8")
    assert "‮" not in raw and "\\u202e" in raw
    assert json.loads(raw)[0]["title"] == "reads‮fdp.exe"


# ------------------------------------------------------------------ refusals


def test_a_symlink_as_to_is_refused(lab_db: Path, tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(ExportError, match="symbolic link"):
        _export(lab_db, link)
    assert list(real.iterdir()) == []


def test_a_missing_to_directory_is_refused(lab_db: Path, tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    with pytest.raises(ExportError, match="does not exist"):
        _export(lab_db, missing)
    assert not missing.exists()


def test_a_to_that_is_a_file_is_refused(lab_db: Path, tmp_path: Path) -> None:
    afile = tmp_path / "afile"
    afile.write_text("x", encoding="utf-8")
    with pytest.raises(ExportError, match="not a directory"):
        _export(lab_db, afile)


# ------------------------------------------------------------------------ the CLI


def test_the_cli_writes_the_folder_and_prints_the_counts(
        lab_db: Path, out: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--db", str(lab_db), "export", "--to", str(out)]) == 0
    printed = capsys.readouterr().out.splitlines()
    folders = [path for path in out.iterdir() if path.name.startswith("lab-export-")]
    assert len(folders) == 1 and printed[0] == f"exported to {folders[0]}"
    counts = _counts(lab_db)
    assert printed[1] == (f"memory items: {counts['memories']}, "
                          f"events: {counts['events']}, tasks: {counts['tasks']}")


def test_the_cli_refusal_is_one_line_on_stderr_and_exit_1(
        lab_db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--db", str(lab_db), "export", "--to", str(tmp_path / "nope")]) == 1
    err = capsys.readouterr().err.splitlines()
    assert len(err) == 1 and err[0].startswith("export: ")


def test_the_cli_does_not_create_a_missing_database(tmp_path: Path, out: Path,
                                                    capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "none.db"
    assert main(["--db", str(missing), "export", "--to", str(out)]) == 1
    assert not missing.exists() and list(out.iterdir()) == []
    assert "no database" in capsys.readouterr().err


def test_the_cli_requires_to(lab_db: Path) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["--db", str(lab_db), "export"])
    assert caught.value.code == 2
