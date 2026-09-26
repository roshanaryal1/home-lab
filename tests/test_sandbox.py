"""Tests for OS-level process isolation. Issue #17.

The load-bearing test is `test_subprocess_cannot_escape_via_a_child`.
Python-level path confinement stops the caller; it does nothing about a
subprocess making its own syscalls. That is the whole reason this module
exists, so it is tested directly rather than assumed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lab import sandbox

pytestmark = pytest.mark.skipif(
    not sandbox.available(),
    reason="OS-level sandbox is macOS only",
)


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "work"
    ws.mkdir()
    (ws / "allowed.txt").write_text("inside")
    (tmp_path / "secret.txt").write_text("do not read me")
    return ws


# ------------------------------------------------------------- allowed


def test_reads_inside_the_workspace_succeed(workspace) -> None:
    result = sandbox.run(
        ["/bin/cat", str(workspace / "allowed.txt")], workspace
    )
    assert result.ok
    assert "inside" in result.stdout


def test_writes_inside_the_workspace_succeed(workspace) -> None:
    result = sandbox.run(
        ["/bin/sh", "-c", f"echo written > {workspace}/new.txt"], workspace
    )
    assert result.ok
    assert (workspace / "new.txt").read_text().strip() == "written"


# -------------------------------------------------------------- denied


def test_reads_outside_the_workspace_are_denied(workspace) -> None:
    outside = workspace.parent / "secret.txt"
    result = sandbox.run(["/bin/cat", str(outside)], workspace)
    assert not result.ok
    assert result.denied
    assert "do not read me" not in result.stdout


def test_writes_outside_the_workspace_are_denied(workspace) -> None:
    escape = workspace.parent / "escaped.txt"
    sandbox.run(["/bin/sh", "-c", f"echo nope > {escape}"], workspace)
    assert not escape.exists(), "a write outside the workspace must not land"


def test_subprocess_cannot_escape_via_a_child(workspace) -> None:
    """The reason this module exists.

    The broker confines paths in Python, which only binds the caller. A
    shell that spawns another process makes its own syscalls. Seatbelt
    restrictions are inherited by children, so the grandchild is confined
    too. Without kernel enforcement this read would succeed.
    """
    outside = workspace.parent / "secret.txt"
    result = sandbox.run(
        ["/bin/sh", "-c", f"/bin/sh -c '/bin/cat {outside}'"], workspace
    )
    assert "do not read me" not in result.stdout, (
        "a grandchild process escaped the sandbox"
    )


def test_home_directory_is_not_readable(workspace) -> None:
    """A concrete case: the agent must not read Roshan's own files."""
    result = sandbox.run(
        ["/bin/sh", "-c", "/bin/ls ~ 2>&1 | head -5"], workspace
    )
    assert "Documents" not in result.stdout
    assert "Desktop" not in result.stdout


def test_ssh_keys_are_not_readable(workspace) -> None:
    key = Path.home() / ".ssh" / "id_ed25519"
    if not key.exists():
        pytest.skip("no key present to attempt")
    result = sandbox.run(["/bin/cat", str(key)], workspace)
    assert "PRIVATE KEY" not in result.stdout


# -------------------------------------------------------------- limits


def test_a_hanging_command_is_killed(workspace) -> None:
    result = sandbox.run(["/bin/sleep", "10"], workspace, timeout=0.5)
    assert not result.ok
    assert "timed out" in result.stderr


# ------------------------------------------------------------- profile


def test_profile_uses_resolved_paths(tmp_path) -> None:
    """The symlink trap.

    On macOS /tmp is a symlink to /private/tmp, so an unresolved path in
    a profile matches nothing and every operation is denied, including
    the permitted ones. That looks like a broken sandbox rather than a
    misconfigured one, which is why it is worth a test.
    """
    ws = tmp_path / "work"
    ws.mkdir()
    profile = sandbox.build_profile(ws)
    assert str(ws.resolve()) in profile
    if str(ws) != str(ws.resolve()):
        assert f'"{ws}"' not in profile, "unresolved path leaked into profile"


def test_profile_denies_by_default(tmp_path) -> None:
    ws = tmp_path / "work"
    ws.mkdir()
    profile = sandbox.build_profile(ws)
    assert "(deny default)" in profile


def test_network_is_denied_unless_asked_for(tmp_path) -> None:
    ws = tmp_path / "work"
    ws.mkdir()
    assert "network-outbound" not in sandbox.build_profile(ws)
    assert "network-outbound" in sandbox.build_profile(ws, allow_network=True)


def test_extra_readable_paths_are_read_only(tmp_path) -> None:
    ws = tmp_path / "work"
    ws.mkdir()
    shared = tmp_path / "shared"
    shared.mkdir()
    profile = sandbox.build_profile(ws, extra_readable=(shared,))
    assert f'(allow file-read* (subpath "{shared.resolve()}"))' in profile
    assert f'file-write* (subpath "{shared.resolve()}")' not in profile


# --------------------------------------------------------- availability


def test_missing_workspace_is_refused(tmp_path) -> None:
    with pytest.raises(sandbox.SandboxUnavailable):
        sandbox.run(["/bin/echo", "hi"], tmp_path / "nope")


def test_unavailable_sandbox_refuses_rather_than_downgrading(
    workspace, monkeypatch
) -> None:
    """Never run unconfined work that asked to be confined.

    A caller that requested isolation and silently did not get it is
    worse off than one that failed, because it will be trusted with work
    it cannot safely run.
    """
    monkeypatch.setattr(sandbox, "available", lambda: False)
    with pytest.raises(sandbox.SandboxUnavailable):
        sandbox.run(["/bin/echo", "hi"], workspace)
