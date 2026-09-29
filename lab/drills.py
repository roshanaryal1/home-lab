"""Recovery drills and their log (item #91).

A unit test shows that a piece of recovery works in isolation. A drill
injects a real failure into a real process and records what was
expected, what happened and what to follow up, so "we can recover" rests
on a dated record rather than on a belief.

A drill counts as **demonstrated** only when it ran on the target
machine: macOS on Apple silicon with ``LAB_TARGET=mac-mini`` set by the
operator. The same drill on a laptop or in CI is logged as a rehearsal
and never counts, because power, fsync and sleep behaviour differ.
"""

from __future__ import annotations

import os
import platform
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from lab import backup as backup_module
from lab.queue import TaskQueue


@dataclass(frozen=True)
class DrillResult:
    name: str
    injected: str
    expected: str
    actual: str
    passed: bool
    follow_up: str = "none"


def on_target() -> bool:
    return (platform.system() == "Darwin" and platform.machine() == "arm64"
            and os.environ.get("LAB_TARGET") == "mac-mini")


def machine_line() -> str:
    return (f"{platform.system()} {platform.release()} {platform.machine()}, "
            f"python {platform.python_version()}, sqlite {sqlite3.sqlite_version}")


def record(result: DrillResult, log_dir: Path) -> Path:
    """Write one dated markdown record; the file name is the drill's identity."""
    log_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    demonstrated = on_target() and result.passed
    path = log_dir / f"{now.strftime('%Y-%m-%dT%H%M%SZ')}-{result.name}.md"
    path.write_text(
        f"# Drill: {result.name}\n\n"
        f"- date: {now.isoformat(timespec='seconds')}\n"
        f"- machine: {machine_line()}\n"
        f"- target: {'mac-mini' if on_target() else 'not the target (rehearsal)'}\n"
        f"- result: {'PASS' if result.passed else 'FAIL'}\n"
        f"- counts as demonstrated: {'yes' if demonstrated else 'no'}\n\n"
        f"## Failure injected\n{result.injected}\n\n"
        f"## Expected\n{result.expected}\n\n"
        f"## Actual\n{result.actual}\n\n"
        f"## Follow-up\n{result.follow_up}\n",
        encoding="utf-8",
    )
    return path


_CHILD = """
import os, signal, sys
from lab.queue import TaskQueue
q = TaskQueue(sys.argv[1], owner="drill")
idem = sys.argv[2] == "idempotent"
q.add_task("drill task", idempotent=idem, max_attempts=3)
t = q.lease()
q.start(t.lease)
print("running", flush=True)
os.kill(os.getpid(), signal.SIGKILL)
"""


def drill_crash(workdir: Path | None = None) -> list[DrillResult]:
    """SIGKILL a process mid-task, restart, recover. Both kinds of task."""
    results = []
    for kind in ("idempotent", "non-idempotent"):
        tmp = Path(tempfile.mkdtemp(prefix="lab-drill-", dir=workdir))
        try:
            db = tmp / "lab.db"
            proc = subprocess.run([sys.executable, "-c", _CHILD, str(db), kind],
                                  capture_output=True, text=True, timeout=60,
                                  env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
            killed = proc.returncode == -signal.SIGKILL
            with TaskQueue(db, owner="drill") as q:
                counts = q.recover()
                states = q.counts()
            if kind == "idempotent":
                want = states == {"queued": 1} and counts["requeued"] == 1
                expected = "the task returns to the queue"
            else:
                want = states == {"interrupted": 1} and counts["held_for_review"] == 1
                expected = "the task is held for a person; it is never replayed blindly"
            results.append(DrillResult(
                f"crash-{kind}",
                f"SIGKILL of the process while {'an' if kind == 'idempotent' else 'a'} "
                f"{kind} task was running",
                expected, f"process killed={killed}; recovery {counts}; states {states}",
                killed and want))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return results


def drill_restore(db_path: Path, artifacts_dir: Path | None = None,
                  workdir: Path | None = None) -> DrillResult:
    """Back up a database and restore it into a fresh directory, checking it."""
    tmp = Path(tempfile.mkdtemp(prefix="lab-restore-", dir=workdir))
    try:
        manifest = backup_module.backup(db_path, tmp / "backup", artifacts_dir)
        report = backup_module.restore_check(manifest, tmp / "restored")
        return DrillResult(
            "restore", "backup taken, then restored into an empty directory",
            "integrity, schema version, audit chain and every artifact hash verify",
            (f"ok={report.ok}; events={report.events}; "
             f"artifacts checked={report.artifacts_checked}; problems={report.problems}"),
            report.ok)
    except backup_module.BackupError as exc:
        return DrillResult("restore", "backup then restore", "restore verifies",
                           f"BackupError: {exc}", False, "investigate before trusting backups")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _closed_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _stub_server(answer_as: str):  # type: ignore[no-untyped-def]
    """A loopback chat-completions server that answers as ``answer_as``."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = json.dumps({"model": answer_as, "usage": {"completion_tokens": 1},
                               "choices": [{"message": {"content": "ok"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}/v1"


def drill_model_load(endpoint: str | None = None) -> list[DrillResult]:
    """Three ways a model can fail to load or answer, each of which must be
    refused with a typed error, quickly, leaving the heavy slot free.

    ``endpoint`` is a live loopback server for the wrong-model case; without
    it a stub stands in, so the drill also runs off the target."""
    import time

    from lab.model import (
        AdmissionController,
        AdmissionRefused,
        BoundedModel,
        ModelError,
        ModelMismatch,
        ModelSpec,
        OpenAICompatibleAdapter,
    )

    rev = "0" * 40
    msgs = [{"role": "user", "content": "Say ok."}]

    def slot_free(ctrl: AdmissionController, spec: ModelSpec) -> bool:
        try:
            with ctrl.admit(spec, msgs, 4):
                return True
        except AdmissionRefused:
            return False

    results = []

    ctrl = AdmissionController()
    spec = ModelSpec("drill-model", rev, rev, 8192, 16, 1000)
    url = f"http://127.0.0.1:{_closed_port()}/v1"
    began, err = time.monotonic(), None
    try:
        BoundedModel(spec, OpenAICompatibleAdapter(url), ctrl).generate(
            msgs, max_tokens=4, timeout_seconds=10)
    except ModelError as exc:
        err = exc
    took = time.monotonic() - began
    free = slot_free(ctrl, spec)
    results.append(DrillResult(
        "model-load-server-down", "the inference server is not listening",
        "a ModelError within 10 s, and the heavy slot is free afterwards",
        f"{type(err).__name__ if err else 'no error'} after {took:.2f} s: {err}; slot free={free}",
        isinstance(err, ModelError) and not isinstance(err, ModelMismatch)
        and took < 10 and free))

    stub = None
    if endpoint is None:
        stub, endpoint = _stub_server("some-other-model")
    ctrl = AdmissionController()
    spec = ModelSpec("drill-expected-model", rev, rev, 8192, 16, 1000)
    err = None
    try:
        BoundedModel(spec, OpenAICompatibleAdapter(endpoint), ctrl).generate(
            msgs, max_tokens=4, timeout_seconds=60)
    except ModelError as exc:
        err = exc
    finally:
        if stub is not None:
            stub.shutdown()
            stub.server_close()
    free = slot_free(ctrl, spec)
    # Two safe outcomes: a server that answers anyway is caught by the lab
    # (ModelMismatch); mlx_lm.server instead rejects an unknown model name
    # itself with HTTP 404 (first seen in the 2026-09-29 drill on the M6).
    # Only 404 counts: a 400, 408 or 429 says nothing about the model.
    refused = isinstance(err, ModelMismatch) or (
        isinstance(err, ModelError) and str(err) == "inference server returned 404")
    results.append(DrillResult(
        "model-load-wrong-model",
        f"the server at {endpoint} is asked for a model it does not serve",
        "refused, never answered: ModelMismatch if the server answers as another model, "
        "or the server's own 404 for an unknown model; the heavy slot is free afterwards",
        f"{type(err).__name__ if err else 'no error'}: {err}; slot free={free}",
        refused and free))

    calls: list[object] = []

    class Counting(OpenAICompatibleAdapter):
        def complete(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(args)
            return super().complete(*args, **kwargs)

    ctrl = AdmissionController()
    spec = ModelSpec("drill-too-big", rev, rev, 8192, 16, ctrl.budget_mb + 1)
    err = None
    try:
        BoundedModel(spec, Counting(f"http://127.0.0.1:{_closed_port()}/v1"), ctrl).generate(
            msgs, max_tokens=4)
    except ModelError as exc:
        err = exc
    results.append(DrillResult(
        "model-load-too-big", "a model larger than the memory budget",
        "AdmissionRefused before the server is called",
        f"{type(err).__name__ if err else 'no error'}: {err}; server calls={len(calls)}",
        isinstance(err, AdmissionRefused) and not calls))
    return results
