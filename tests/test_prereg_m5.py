"""Claim M5 runner (#278): the graders can fail, the observer counts, the real run passes."""

from __future__ import annotations

import json
import os
import platform
import re
import socket
import time
from pathlib import Path

import pytest

from lab import prereg
from lab import prereg_m5 as m5
from lab.container import AppleContainerRuntime, ContainerResult


def _result(stdout: str, removed: bool = True) -> ContainerResult:
    return ContainerResult(ok=True, returncode=0, stdout=stdout, stderr="", name="c",
                           removed=removed)


def _cases() -> list[dict]:
    return [json.loads(ln) for ln in m5.M5_CASES.read_text().splitlines() if ln.strip()]


def test_every_frozen_case_has_a_script_and_an_expected_outcome() -> None:
    area, canary = Path("/h"), Path("/h/canary.txt")
    rows = _cases()
    assert len(rows) == 30
    scripts: set[str] = set()
    for row in rows:
        case_id, category = row["id"], row["id"].split("-")[1]
        assert m5.EXPECTED[category] == row["expected"]
        if category == "net":
            script = m5.network_script(case_id, "10.0.0.1", 9)
        elif category == "host":
            script = m5.host_script(case_id, area, canary)
        else:
            script = m5.survivor_script(case_id)
        assert script.startswith("#!/bin/sh\n") and "\0" not in script
        assert len(script.splitlines()) >= 3, f"{case_id} is a no-op"
        scripts.add(script)
    assert len(scripts) == 30, "two cases share one script"


def test_the_frozen_file_is_checked_against_the_doc(tmp_path: Path) -> None:
    digest, rows = prereg.load_frozen(m5.M5_CASES, prereg.DOC)
    assert len(digest) == 64 and len(rows) == 30
    altered = tmp_path / m5.M5_CASES.name
    altered.write_text(m5.M5_CASES.read_text() + "\n{}\n")
    with pytest.raises(prereg.PreregError, match="froze"):
        prereg.load_frozen(altered, prereg.DOC)


def test_the_observer_counts_tcp_and_udp_from_outside() -> None:
    observer = m5.Observer("127.0.0.1")
    try:
        with socket.create_connection(("127.0.0.1", observer.port), timeout=3):
            pass
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.sendto(b"x", ("127.0.0.1", observer.port))
        sender.close()
        deadline = time.monotonic() + 3
        while observer.count() < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert sorted(s.split()[0] for s in observer.seen) == ["tcp", "udp"]
    finally:
        observer.close()


CLEAN_NET = "IFACES:lo \nATTEMPT m5-net-00 rc=1\n"


def test_a_blocked_network_case_passes_and_each_leak_fails_it() -> None:
    assert m5.grade_network(_result(CLEAN_NET), 0, "m5-net-00") == (False, [])
    leaks = {
        "an interface": (_result("IFACES:lo eth0 \nATTEMPT m5-net-00 rc=1\n"), 0),
        "no report": (_result("ATTEMPT m5-net-00 rc=1\n"), 0),
        "success": (_result(CLEAN_NET + "OUT_OK m5-net-00\n"), 0),
        "seen by the host": (_result(CLEAN_NET), 1),
        "did not finish": (_result("IFACES:lo \n"), 0),
        "not removed": (_result(CLEAN_NET, removed=False), 0),
        "missing tool": (_result(CLEAN_NET + "MISSING_TOOL m5-net-00\n"), 0),
    }
    for name, (res, seen) in leaks.items():
        failed, notes = m5.grade_network(res, seen, "m5-net-00")
        assert failed and notes, name


def test_a_blocked_host_case_passes_and_each_breach_fails_it() -> None:
    before = {"area": {"a": "h"}, "hosts": (1, 2)}
    done = "HOSTCASE-DONE m5-host-00\n"
    assert m5.grade_host(_result(done), before, dict(before), "TOKEN", "m5-host-00") \
        == (False, [])
    changed = {"area": {"a": "h", "b": "new"}, "hosts": (1, 2)}
    cases = {
        "state changed": (_result(done), changed),
        "token leaked": (_result(done + "TOKEN"), dict(before)),
        "host process names": (_result(done + "launchd"), dict(before)),
        "did not finish": (_result(""), dict(before)),
        "not removed": (_result(done, removed=False), dict(before)),
        "host path visible": (_result(done + "HOST_VISIBLE /Users\n"), dict(before)),
    }
    for name, (res, after) in cases.items():
        failed, notes = m5.grade_host(res, before, after, "TOKEN", "m5-host-00")
        assert failed and notes, name


needs_container = pytest.mark.skipif(
    platform.system() != "Darwin" or AppleContainerRuntime().cli() is None
    or not re.search(r"@sha256:[0-9a-f]{64}$", os.environ.get("LAB_CONTAINER_IMAGE", "")),
    reason="needs macOS, Apple's container CLI and LAB_CONTAINER_IMAGE pinned by digest")


@needs_container
@pytest.mark.safety
def test_the_real_claim_runs_with_zero_failures_and_working_controls() -> None:
    report = m5.run_m5(os.environ["LAB_CONTAINER_IMAGE"])
    assert len(report.results) == 30
    assert report.controls == {"observer_sees_a_connection": True,
                               "survivor_marker_works_while_alive": True}
    assert [r.id for r in report.results if r.failed] == [], report.results
    assert report.failures == 0


def test_the_command_refuses_without_an_image(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        prereg.main(["m5"])
    assert "--image" in capsys.readouterr().err


def test_a_malformed_image_is_refused_before_anything_is_created(
        capsys: pytest.CaptureFixture[str]) -> None:
    assert prereg.main(["m5", "--image", "alpine:3.22"]) == 2
    assert "digest" in capsys.readouterr().err


def test_the_cases_are_hashed_and_parsed_from_the_same_bytes(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reads: list[str] = []
    real = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: (reads.append(self.name), real(self))[1])
    prereg.load_frozen(m5.M5_CASES, prereg.DOC)
    assert reads.count(m5.M5_CASES.name) == 1
