"""The nightly self-test (H5b).

Run on the machine that runs the lab, so it proves things a hosted CI run
cannot: that the audit chain of the live database verifies, that a backup of
it restores into a fresh directory with every hash intact, that the health
check is not red, and that the safety-marked tests still pass against the
installed Python and SQLite (a new macOS has needed a sandbox fix before).

It reads the live database through a read-only connection and only ever
writes one thing to it: a ``selftest`` event with the result, so the nightly
record is part of the hash-chained log.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from lab import backup, metrics
from lab.audit import verify_chain
from lab.queue import TaskQueue

DEFAULT_TESTS = Path(__file__).resolve().parent.parent / "tests"
SAFETY_TIMEOUT_SECONDS = 900.0


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class SelfTestReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    def failures(self) -> list[Check]:
        return [check for check in self.checks if not check.ok]


def _readonly(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _failed(name: str, exc: BaseException) -> Check:
    return Check(name, False, f"{type(exc).__name__}: {exc}"[:300])


def _check_chain(db: Path) -> Check:
    try:
        conn = _readonly(db)
        try:
            report = verify_chain(conn)
        finally:
            conn.close()
    except (sqlite3.DatabaseError, OSError) as exc:
        return _failed("audit_chain", exc)
    if report.ok:
        return Check("audit_chain", True, f"{report.events} events verify")
    return Check("audit_chain", False, f"{report.problem} at event {report.bad_id}")


def _check_backup(db: Path) -> Check:
    with tempfile.TemporaryDirectory(prefix="lab-selftest-") as tmp:
        try:
            artifacts = db.parent / "artifacts"
            manifest = backup.backup(db, Path(tmp) / "backup",
                                     artifacts if artifacts.exists() else None)
            restored = backup.restore_check(manifest, Path(tmp) / "restore")
        except (backup.BackupError, OSError, sqlite3.DatabaseError) as exc:
            return Check("backup_restore", False, f"{type(exc).__name__}: {exc}")
    if restored.ok:
        return Check("backup_restore", True,
                     f"{restored.events} events, {restored.artifacts_checked} artifacts restored")
    return Check("backup_restore", False, "; ".join(restored.problems)[:300])


def _check_health(db: Path) -> Check:
    try:
        conn = _readonly(db)
        try:
            report = metrics.collect(conn)
        finally:
            conn.close()
    except (sqlite3.DatabaseError, OSError) as exc:
        return _failed("health", exc)
    detail = report.health + (f" ({'; '.join(report.reasons)})" if report.reasons else "")
    return Check("health", report.health != "unhealthy", detail[:300])


def _check_safety_tests(tests_dir: Path) -> Check:
    tests_dir = tests_dir.resolve()      # used as both cwd and the path, so resolve it once
    if not tests_dir.is_dir():
        return Check("safety_tests", True, "skipped: no tests directory on this install")
    try:
        # A fixed argv: this interpreter running pytest over a directory the
        # operator named. No shell, and nothing here comes from the database.
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", str(tests_dir), "-q", "-m", "safety",
             "--no-header", "-p", "no:cacheprovider"],
            capture_output=True, text=True, timeout=SAFETY_TIMEOUT_SECONDS, cwd=tests_dir,
            check=False)
    except subprocess.TimeoutExpired:
        return Check("safety_tests", False, f"timed out after {SAFETY_TIMEOUT_SECONDS:g}s")
    except (OSError, ValueError) as exc:
        return _failed("safety_tests", exc)
    matched = next((ln for ln in reversed(proc.stdout.splitlines())
                    if re.search(r"\d+ (passed|failed|error)", ln)), None)
    summary = matched if matched is not None else proc.stdout[-200:]
    if matched is None and proc.returncode != 0:
        # pytest gave no summary (for example "No module named pytest", #270), so the
        # reason is on stderr. Its last line says why. The stdout tail is the fallback.
        summary = next((ln for ln in reversed(proc.stderr.splitlines()) if ln.strip()), summary)
    return Check("safety_tests", proc.returncode == 0, summary.strip("= ").strip()[:200])


def run(db: str | Path, *, tests_dir: Path | None = None,
        run_safety_tests: bool = True) -> SelfTestReport:
    db = Path(db)
    report = SelfTestReport()
    report.checks.append(_check_chain(db))
    report.checks.append(_check_backup(db))
    report.checks.append(_check_health(db))
    report.checks.append(_check_safety_tests(tests_dir or DEFAULT_TESTS) if run_safety_tests
                         else Check("safety_tests", True, "skipped by request"))
    try:
        with TaskQueue(db, owner="selftest") as queue:
            queue.record_event(None, "selftest", {
                "ok": report.ok,
                "checks": [{"name": c.name, "ok": c.ok} for c in report.checks]})
    except (sqlite3.DatabaseError, OSError) as exc:
        report.checks.append(Check("record_result", False, f"{type(exc).__name__}: {exc}"))
    return report
