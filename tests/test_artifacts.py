"""Content-addressed artifacts and receipts (item 3.3, #66)."""

from __future__ import annotations

import hashlib
import os
import socket
import stat
from pathlib import Path

import pytest

from lab.artifacts import (
    MAX_ARTIFACT_BYTES,
    ArtifactError,
    ArtifactStore,
)
from lab.broker import ToolSession, Workspace
from lab.queue import Task, TaskQueue
from lab.supervisor import Supervisor, SupervisorConfig


@pytest.fixture()
def q(tmp_path: Path) -> TaskQueue:
    with TaskQueue(tmp_path / "lab.db") as queue:
        yield queue


@pytest.fixture()
def store(tmp_path: Path, q: TaskQueue) -> ArtifactStore:
    return ArtifactStore(tmp_path / "artifacts", q._conn)


@pytest.fixture()
def ws(tmp_path: Path) -> Workspace:
    root = tmp_path / "ws"
    root.mkdir(mode=0o700)
    return Workspace(root)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_files_are_stored_by_hash_with_a_descriptor(store, ws) -> None:
    (ws.root / "out").mkdir()
    (ws.root / "out" / "a.txt").write_bytes(b"alpha")
    (ws.root / "b.json").write_bytes(b"{}")
    stored, refused = store.ingest_workspace(ws, "t1", 1)
    assert refused == []
    assert {a.path for a in stored} == {"out/a.txt", "b.json"}
    a = next(a for a in stored if a.path == "out/a.txt")
    assert a.sha256 == sha(b"alpha") and a.size == 5 and a.media_type == "text/plain"
    assert store.read(a.sha256) == b"alpha"
    blob = store.blob_path(a.sha256)
    assert stat.S_IMODE(blob.stat().st_mode) == 0o400
    rows = store.for_task("t1")
    assert [(r["attempt"], r["path"], r["sha256"]) for r in rows] == [
        (1, "b.json", sha(b"{}")), (1, "out/a.txt", sha(b"alpha"))]
    assert rows[0]["tool_version"] and rows[0]["lineage"] == "[]"


def test_identical_bytes_are_stored_once(store, ws) -> None:
    (ws.root / "x").write_bytes(b"same")
    (ws.root / "y").write_bytes(b"same")
    store.ingest_workspace(ws, "t1", 1)
    blobs = [p for p in store.root.rglob("*") if p.is_file() and ".incoming" not in p.parts]
    assert len(blobs) == 1


def test_the_workspace_can_be_deleted_and_the_output_survives(store, ws) -> None:
    (ws.root / "result.txt").write_bytes(b"kept")
    store.ingest_workspace(ws, "t1", 1)
    ws.destroy()
    (row,) = store.for_task("t1")
    assert store.read(row["sha256"]) == b"kept"


@pytest.mark.safety
def test_links_devices_sockets_and_fifos_are_refused_not_followed(store, ws, tmp_path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"outside the workspace")
    (ws.root / "good.txt").write_bytes(b"good")
    os.symlink(secret, ws.root / "link")
    os.symlink(tmp_path, ws.root / "dirlink")
    os.link(secret, ws.root / "hard")            # a regular file: allowed, by content
    os.mkfifo(ws.root / "pipe")
    sock = socket.socket(socket.AF_UNIX)
    # AF_UNIX paths are short; bind relative to the workspace.
    cwd = os.getcwd()
    os.chdir(ws.root)
    try:
        sock.bind("s.sock")
    finally:
        os.chdir(cwd)
    try:
        stored, refused = store.ingest_workspace(ws, "t1", 1)
    finally:
        sock.close()
    assert {r.path: r.reason for r in refused} == {
        "link": "symlink", "dirlink": "symlink", "pipe": "fifo", "s.sock": "socket"}
    assert {a.path for a in stored} == {"good.txt", "hard"}
    # The symlink's target was never read into the store.
    assert not any(sha(b"outside the workspace") == a.sha256 and a.path == "link"
                   for a in stored)
    events = [r["kind"] for r in store._conn.execute("SELECT kind FROM events ORDER BY id")]
    assert "artifact_refused" in events and "artifacts_stored" in events


@pytest.mark.safety
def test_a_file_swapped_for_a_symlink_after_listing_is_not_read(
        store, ws, tmp_path, monkeypatch) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"do not read")
    (ws.root / "f").write_bytes(b"innocent")
    import lab.artifacts as artifacts
    real_walk = artifacts._walk

    def swapping_walk(w):
        for parts, info in real_walk(w):
            (w.root / "f").unlink()
            os.symlink(secret, w.root / "f")
            yield parts, info

    monkeypatch.setattr(artifacts, "_walk", swapping_walk)
    stored, refused = store.ingest_workspace(ws, "t1", 1)
    assert stored == []
    assert [r.path for r in refused] == ["f"]
    assert not any(sha(b"do not read") == r["sha256"] for r in store.for_task("t1"))


def test_an_oversized_file_fails_the_ingest(store, ws, monkeypatch) -> None:
    import lab.artifacts as artifacts
    monkeypatch.setattr(artifacts, "MAX_ARTIFACT_BYTES", 10)
    (ws.root / "big").write_bytes(b"x" * 11)
    with pytest.raises(ArtifactError, match="ceiling"):
        store.ingest_workspace(ws, "t1", 1)
    assert store.for_task("t1") == []
    assert not list((store.root / ".incoming").iterdir()), "no temp file left behind"
    assert MAX_ARTIFACT_BYTES == 64 * 1024 * 1024


def test_reingesting_an_attempt_replaces_the_descriptor(store, ws) -> None:
    (ws.root / "f").write_bytes(b"one")
    store.ingest_workspace(ws, "t1", 1)
    (ws.root / "f").write_bytes(b"two")
    store.ingest_workspace(ws, "t1", 1)
    (row,) = store.for_task("t1")
    assert row["sha256"] == sha(b"two")


# ------------------------------------------------------------- verification


@pytest.mark.safety
def test_verify_all_finds_a_missing_and_an_altered_blob(store, ws) -> None:
    (ws.root / "a").write_bytes(b"aaa")
    (ws.root / "b").write_bytes(b"bbb")
    (ws.root / "c").write_bytes(b"ccc")
    store.ingest_workspace(ws, "t1", 1)
    assert store.verify_all() == []

    a, b = store.blob_path(sha(b"aaa")), store.blob_path(sha(b"bbb"))
    a.unlink()
    os.chmod(b, 0o600)
    b.write_bytes(b"BBB")
    found = {(p.path, p.problem) for p in store.verify_all()}
    assert found == {("a", "missing"), ("b", "hash")}


def test_read_refuses_a_corrupt_blob(store, ws) -> None:
    (ws.root / "a").write_bytes(b"aaa")
    store.ingest_workspace(ws, "t1", 1)
    blob = store.blob_path(sha(b"aaa"))
    os.chmod(blob, 0o600)
    blob.write_bytes(b"zzz")
    with pytest.raises(ArtifactError, match="does not match"):
        store.read(sha(b"aaa"))


def test_a_bad_digest_never_becomes_a_path(store) -> None:
    for bad in ("../../etc/passwd", "abc", "g" * 64, "A" * 64):
        with pytest.raises(ArtifactError):
            store.blob_path(bad)


def test_receipt_lists_the_attempt(store, ws) -> None:
    (ws.root / "a").write_bytes(b"aaa")
    store.ingest_workspace(ws, "t1", 2)
    assert store.receipt("t1", 2) == {
        "task_id": "t1", "attempt": 2,
        "artifacts": [{"path": "a", "sha256": sha(b"aaa"), "size": 3}]}
    assert store.receipt("t1", 1)["artifacts"] == []


# ------------------------------------------------------- supervisor wiring


def make_supervisor(tmp_path: Path, **kw) -> Supervisor:
    return Supervisor(SupervisorConfig(db_path=tmp_path / "lab.db",
                                       idle_poll_seconds=0.01, **kw))


@pytest.mark.asyncio
async def test_a_succeeded_task_has_its_outputs_stored(tmp_path: Path) -> None:
    sup = make_supervisor(tmp_path)

    async def handler(task: Task, tools: ToolSession) -> dict:
        assert tools.submit("fs.write", path="report.md", content="# result").ok
        return {"done": True}

    sup.register("demo", handler, tools={"fs.write"})
    task_id = sup.queue.add_task("write", agent_kind="demo")
    await sup.run(max_tasks=1)
    assert sup.queue.get(task_id).state == "succeeded"
    assert task_id not in sup.broker._workspaces, "workspace gone"
    (row,) = sup.artifacts.for_task(task_id)
    assert row["path"] == "report.md" and row["attempt"] == 1
    assert sup.artifacts.read(row["sha256"]) == b"# result"
    assert sup.artifacts.verify_all() == []
    sup.close()


@pytest.mark.safety
@pytest.mark.asyncio
async def test_a_task_whose_outputs_cannot_be_stored_does_not_succeed(
        tmp_path: Path, monkeypatch) -> None:
    sup = make_supervisor(tmp_path)

    async def handler(task: Task, tools: ToolSession) -> dict:
        tools.submit("fs.write", path="out.txt", content="x")
        return {"done": True}

    def broken(*args, **kwargs):
        raise ArtifactError("store is full")

    monkeypatch.setattr(sup.artifacts, "ingest_workspace", broken)
    sup.register("demo", handler, tools={"fs.write"})
    task_id = sup.queue.add_task("write", agent_kind="demo", max_attempts=1)
    await sup.run(max_tasks=1)
    task = sup.queue.get(task_id)
    assert task.state == "failed" and "store is full" in (task.last_error or "")
    sup.close()


@pytest.mark.safety
@pytest.mark.asyncio
async def test_no_succeeded_task_points_at_a_missing_artifact(tmp_path: Path) -> None:
    sup = make_supervisor(tmp_path)

    async def handler(task: Task, tools: ToolSession) -> dict:
        tools.submit("fs.write", path=f"{task.title}.txt", content=task.title)
        return {}

    sup.register("demo", handler, tools={"fs.write"})
    ids = [sup.queue.add_task(f"n{i}", agent_kind="demo") for i in range(3)]
    await sup.run(max_tasks=3)
    for task_id in ids:
        assert sup.queue.get(task_id).state == "succeeded"
        rows = sup.artifacts.for_task(task_id)
        assert rows and all(sup.artifacts.read(r["sha256"]) for r in rows)
    assert sup.artifacts.verify_all() == []
    sup.close()


# --------------------------------------------------------------------- CLI


def test_cli_list_and_verify(tmp_path: Path, capsys) -> None:
    from lab.cli import main
    db = tmp_path / "lab.db"
    with TaskQueue(db) as q:
        store = ArtifactStore(tmp_path / "artifacts", q._conn)
        root = tmp_path / "ws"
        root.mkdir()
        (root / "a.txt").write_bytes(b"aaa")
        store.ingest_workspace(Workspace(root), "t1", 1)
    assert main(["--db", str(db), "artifacts", "list", "t1"]) == 0
    assert "a.txt" in capsys.readouterr().out
    assert main(["--db", str(db), "artifacts", "verify"]) == 0
    os.chmod(tmp_path / "artifacts" / sha(b"aaa")[:2] / sha(b"aaa"), 0o600)
    (tmp_path / "artifacts" / sha(b"aaa")[:2] / sha(b"aaa")).unlink()
    assert main(["--db", str(db), "artifacts", "verify"]) == 1
    assert "missing" in capsys.readouterr().out
