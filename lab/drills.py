"""Recovery drills and their log (item #91).

A unit test shows that a piece of recovery works in isolation. A drill
injects a real failure into a real process and records what was
expected, what happened and what to follow up, so "we can recover" rests
on a dated record rather than on a belief.

A drill counts as **demonstrated** only when it ran on the target
machine: macOS on Apple silicon with ``LAB_TARGET=mac-mini`` set by the
operator. The same drill on a laptop or in CI is logged as a rehearsal
and never counts, because power, fsync and sleep behaviour differ.

The interrupted-task drill has two halves because the failure it waits
for is a restart or a power pull, which no process survives: ``arm``
leaves two tasks running on a scratch database, the operator injects the
failure, and ``check`` (after the reboot) records what recovery did.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import platform
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lab import audit
from lab import backup as backup_module
from lab.queue import TaskQueue


class DrillError(RuntimeError):
    """A drill cannot run as asked. Nothing is recorded."""


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


_RESTORE_EXPECTED = "integrity, schema version, audit chain and every artifact hash verify"


def _restore_actual(report: backup_module.RestoreReport) -> str:
    return (f"ok={report.ok}; events={report.events}; "
            f"artifacts checked={report.artifacts_checked}; problems={report.problems}")


def drill_restore(db_path: Path, artifacts_dir: Path | None = None,
                  workdir: Path | None = None) -> DrillResult:
    """Back up a database and restore it into a fresh directory, checking it."""
    tmp = Path(tempfile.mkdtemp(prefix="lab-restore-", dir=workdir))
    try:
        manifest = backup_module.backup(db_path, tmp / "backup", artifacts_dir)
        report = backup_module.restore_check(manifest, tmp / "restored")
        return DrillResult(
            "restore", "backup taken, then restored into an empty directory",
            _RESTORE_EXPECTED, _restore_actual(report), report.ok)
    except backup_module.BackupError as exc:
        return DrillResult("restore", "backup then restore", "restore verifies",
                           f"BackupError: {exc}", False, "investigate before trusting backups")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def drill_restore_backup(source: Path, workdir: Path | None = None) -> DrillResult:
    """Restore a backup that already exists into a fresh directory, checking it.

    ``source`` is one manifest, or a backup folder, whose newest backup is
    used. This tests the copy on the backup disk, written days ago by the
    scheduled job, rather than a snapshot taken a moment before. The backup
    folder is only read.
    """
    source = Path(source)
    tmp = Path(tempfile.mkdtemp(prefix="lab-restore-", dir=workdir))
    try:
        manifest = source if source.is_file() else backup_module.newest_manifest(source)
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            taken = str(data.get("created_at", "unknown")) if isinstance(data, dict) else "unknown"
        except (OSError, ValueError):
            taken = "unknown"
        report = backup_module.restore_check(manifest, tmp / "restored")
        return DrillResult(
            "restore",
            f"the existing backup {manifest.name} in {manifest.parent} (taken {taken}) "
            "restored into an empty directory",
            _RESTORE_EXPECTED, _restore_actual(report), report.ok)
    except backup_module.BackupError as exc:
        return DrillResult("restore", f"restore of the existing backup at {source}",
                           "restore verifies", f"BackupError: {exc}", False,
                           "investigate before trusting backups")
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


# ------------------------------------------------- interrupted task (#91)

_ARMED = "armed.json"
_TERM = "term.json"
_DB = "drill.db"
_HOLDER_LOG = "holder.log"
# Every file the interrupted drill writes. Nothing else is ever removed.
_STATE_FILES = (_ARMED, f"{_ARMED}.partial", _TERM, f"{_TERM}.partial", _HOLDER_LOG,
                _DB, f"{_DB}-wal", f"{_DB}-shm", f"{_DB}-journal")
_HOLDER_ENTRY = "import sys; from lab.drills import hold_main; hold_main(sys.argv[1:])"
_RENEW_SECONDS = 1.0
_LEASE_SECONDS = 300


def boot_time() -> float | None:
    """When this machine last booted, as Unix time, or None if it cannot tell."""
    if platform.system() == "Darwin":
        try:
            out = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.boottime"],
                                 capture_output=True, text=True, timeout=5,
                                 check=False).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        match = re.search(r"\bsec = (\d+)", out)
        return float(match.group(1)) if match else None
    try:
        for line in Path("/proc/stat").read_text(encoding="ascii").splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _iso(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, UTC).isoformat(timespec="seconds")


def _write_json(state: Path, name: str, data: dict[str, Any]) -> None:
    """Atomic, and on the disk itself before it returns: the next event may be a
    power pull. On macOS a plain fsync leaves the data in the drive's cache;
    F_FULLFSYNC is what the queue's ``fullfsync`` uses as well."""
    partial = state / f"{name}.partial"
    with open(partial, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
        fh.flush()
        full = getattr(fcntl, "F_FULLFSYNC", None)
        if full is not None:
            fcntl.fcntl(fh.fileno(), full)
        else:
            os.fsync(fh.fileno())
    os.replace(partial, state / name)
    fd = os.open(state, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def remove_state(state: Path) -> None:
    """Remove the files the interrupted drill writes, then the folder if that empties it."""
    for name in _STATE_FILES:
        with contextlib.suppress(FileNotFoundError):
            (Path(state) / name).unlink()
    with contextlib.suppress(OSError):
        Path(state).rmdir()


def hold_main(argv: list[str]) -> None:
    """Entry point of the holder process that ``arm_interrupted`` starts."""
    hold_tasks(Path(argv[0]), float(argv[1]))


def hold_tasks(state: Path, hold_seconds: float) -> None:
    """Start two tasks on the scratch database and keep them running.

    One task is idempotent and one is not. Their leases are renewed every
    second, so a power pull lands in the middle of the writes. A SIGTERM,
    which is what a clean shutdown sends, is noted on disk and the process
    exits at once, leaving both tasks running for recovery to find. If no
    failure comes within ``hold_seconds``, the holder removes its files.
    """
    def on_term(signum: int, frame: object) -> None:
        _write_json(state, _TERM, {"signal": "SIGTERM", "at": time.time()})
        os._exit(0)

    signal.signal(signal.SIGTERM, on_term)
    queue = TaskQueue(state / _DB, owner="drill")
    tasks = {kind: queue.add_task(f"drill: {kind} task in flight",
                                  idempotent=kind == "idempotent", max_attempts=3)
             for kind in ("idempotent", "non-idempotent")}
    tokens = []
    for _ in tasks:
        task = queue.lease(ttl_seconds=_LEASE_SECONDS)
        if task is None or task.lease is None:
            raise DrillError("the drill task could not be leased")
        queue.start(task.lease)
        tokens.append(task.lease)
    _write_json(state, _ARMED, {"armed_at": time.time(), "holder_pid": os.getpid(),
                                "tasks": tasks, "hold_seconds": hold_seconds})
    print(f"armed: tasks {tasks} running", flush=True)
    deadline = time.time() + hold_seconds
    while time.time() < deadline:
        for token in tokens:
            queue.renew_lease(token, ttl_seconds=_LEASE_SECONDS)
        time.sleep(_RENEW_SECONDS)
    queue.close()
    remove_state(state)


def arm_interrupted(state: Path, hold_seconds: float, *,
                    wait_seconds: float = 30.0) -> dict[str, Any]:
    """Start the holder on a scratch database in ``state`` and wait until its
    two tasks are running. Returns what it recorded (pid, task ids, time)."""
    state = Path(state)
    if (state / _ARMED).exists() or (state / _DB).exists():
        raise DrillError(f"a drill is already armed in {state}: run the check phase, or wait "
                         "until its hold time is over and it removes itself")
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(state / _HOLDER_LOG, "wb") as log:
        # A session of its own, so closing the Terminal window does not end it.
        # It outlives this process on purpose and is never waited for once armed
        # (``python -X dev`` reports that as a ResourceWarning).
        holder = subprocess.Popen(
            [sys.executable, "-c", _HOLDER_ENTRY, str(state), str(hold_seconds)],
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True,
            env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
    deadline = time.monotonic() + wait_seconds
    while True:
        armed = _read_json(state / _ARMED)
        if armed is not None:
            return armed
        if holder.poll() is not None or time.monotonic() > deadline:
            break
        time.sleep(0.05)
    if holder.poll() is None:
        holder.kill()
    holder.wait()
    try:
        detail = (state / _HOLDER_LOG).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        detail = ""
    remove_state(state)
    raise DrillError("the holder did not get two tasks running: "
                     + (detail[-2000:] or f"no output, exit {holder.returncode}"))


def _holder_running(pid: int) -> bool:
    """Is the holder still alive? A zombie, or another process that now has its
    pid, is not it. If ``ps`` cannot be run, it is not claimed to be gone."""
    try:
        proc = subprocess.run(["ps", "-ww", "-o", "stat=,command=", "-p", str(pid)],
                              capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return True
    fields = proc.stdout.strip().split(None, 1)
    if proc.returncode != 0 or len(fields) < 2 or fields[0].startswith("Z"):
        return False
    return "from lab.drills import hold_main" in fields[1]


def _inspect(db: Path) -> tuple[str, bool, int]:
    """SQLite's integrity check and the audit chain, read before recovery writes."""
    conn = sqlite3.connect(db)
    try:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        chain = audit.verify_chain(conn)
        return integrity, chain.ok, chain.events
    finally:
        conn.close()


def check_interrupted(state: Path) -> DrillResult:
    """After the failure: is the scratch database intact, and did recovery
    requeue the idempotent task and hold the other for a person?

    Refuses (``DrillError``) while the holder is still running, because then
    nothing has been injected. On the target, a check with no restart since
    the arm is a FAIL: that drill is about a restart or a power pull.
    """
    state = Path(state)
    armed = _read_json(state / _ARMED)
    if armed is None:
        raise DrillError(f"nothing is armed in {state}: run the arm phase first (an armed drill "
                         "that is never checked removes itself after its hold time)")
    try:
        armed_at = float(armed["armed_at"])
        pid = int(armed["holder_pid"])
        idem_id = str(armed["tasks"]["idempotent"])
        other_id = str(armed["tasks"]["non-idempotent"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DrillError(f"the drill state in {state} is unreadable: {exc!r}") from exc
    booted = boot_time()
    restarted = booted is not None and booted > armed_at
    if not restarted and _holder_running(pid):
        raise DrillError(f"the holder (pid {pid}) is still running, so no failure has been "
                         f"injected yet. Pull the power, restart the Mac, or for a rehearsal "
                         f"kill -9 {pid}; then check again")
    term = _read_json(state / _TERM)

    integrity, chain_ok, events = "the database file is missing", False, 0
    recovery: dict[str, int] = {}
    idem_state = other_state = "missing"
    db = state / _DB
    if db.exists():
        try:
            integrity, chain_ok, events = _inspect(db)
            with TaskQueue(db, owner="drill") as queue:
                recovery = queue.recover()
                idem, other = queue.get(idem_id), queue.get(other_id)
                idem_state = idem.state if idem is not None else "missing"
                other_state = other.state if other is not None else "missing"
        except sqlite3.DatabaseError as exc:
            integrity = f"the database cannot be read: {exc}"

    base = (f"two tasks running on a scratch database, one idempotent and one not, their "
            f"leases renewed every second by a holder process (pid {pid})")
    if restarted and term is not None:
        how = (f"the Mac was shut down or restarted cleanly: the holder got SIGTERM at "
               f"{_iso(float(term.get('at', 0)))}, and the Mac booted at {_iso(booted or 0)}")
    elif restarted:
        how = (f"the Mac lost power or was forced off: no SIGTERM reached the holder, and the "
               f"Mac booted at {_iso(booted or 0)}")
    elif term is not None:
        how = "the holder got SIGTERM and the Mac did not restart (a rehearsal)"
    else:
        how = "the holder was killed and the Mac did not restart (a rehearsal)"

    target = on_target()
    expected = ("the database is intact and its audit chain verifies; recovery requeues the "
                "idempotent task and holds the other for review, never replaying it")
    if target:
        expected += "; the Mac restarted between arm and check"
    intact = integrity == "ok" and chain_ok
    recovered = (recovery == {"interrupted": 2, "requeued": 1, "held_for_review": 1}
                 and idem_state == "queued" and other_state == "interrupted")
    passed = intact and recovered and (restarted or not target)
    if passed:
        follow_up = "none"
    elif intact and recovered:
        follow_up = ("no restart or power loss came between arm and check; arm again and "
                     "inject the failure")
    else:
        follow_up = "investigate before trusting recovery after a power loss"
    return DrillResult(
        "interrupted-task", f"{base}; {how}", expected,
        (f"integrity {integrity}; audit chain ok={chain_ok} ({events} events); recovery "
         f"{recovery}; idempotent task {idem_state}, non-idempotent task {other_state}; "
         f"restarted={'yes' if restarted else 'no'}; checked "
         f"{time.time() - armed_at:.0f} s after it was armed at {_iso(armed_at)}"),
        passed, follow_up)
