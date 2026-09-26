"""Tests for OS-level process isolation. Issue #17.

The load-bearing test is `test_subprocess_cannot_escape_via_a_child`.
Python-level path confinement stops the caller; it does nothing about a
subprocess making its own syscalls. That is the whole reason this module
exists, so it is tested directly rather than assumed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

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


# ------------------------- macOS 27 findings, issue #24


def test_profile_grants_read_on_the_root_entry(tmp_path) -> None:
    """Without this, dyld can abort the process before main on macOS 27.

    Found on the deployment target by bisecting a profile that worked on
    26.5.1 and aborted with SIGABRT on 27. Granting subpaths of /usr,
    /bin and /System is not sufficient; the root entry itself is needed.
    """
    ws = tmp_path / "work"
    ws.mkdir()
    assert '(allow file-read* (literal "/"))' in sandbox.build_profile(ws)


def test_root_entry_access_does_not_weaken_confinement(workspace) -> None:
    """The clause above grants the directory entry, not its contents.

    Worth asserting rather than trusting, since a clause added to stop a
    crash is exactly the kind of thing that quietly opens a hole.
    """
    for target in ("/etc/hosts", "/etc/passwd", "/etc/ssh/sshd_config"):
        result = sandbox.run(["/bin/cat", target], workspace)
        assert not result.ok, f"{target} should not be readable"

    listing = sandbox.run(["/bin/ls", str(Path.home())], workspace)
    assert "Documents" not in listing.stdout


def test_resolved_path_is_enforced_not_merely_generated(tmp_path) -> None:
    """The symlink trap, asserted by behaviour rather than by string.

    The existing test checks the profile text. This one checks that the
    sandbox actually permits the workspace when reached through the
    symlinked /tmp path, which is what the trap really breaks.
    """
    import tempfile
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
        ws = Path(raw)
        (ws / "f.txt").write_text("reachable")
        result = sandbox.run(["/bin/cat", str((ws / "f.txt").resolve())], ws)
        assert result.ok, "resolved workspace path must be readable"
        assert "reachable" in result.stdout


def test_account_enumeration_is_NOT_prevented(workspace) -> None:
    """Documents a known gap, so nobody assumes it is closed.

    Measured on macOS 27 by the mini: with /etc fully denied, `id -un`
    and `dscl . -list /Users` still work, because they reach
    opendirectoryd over a mach port rather than reading /etc/passwd.

    This test asserts the gap EXISTS. If it starts failing, the sandbox
    got stronger and SECURITY.md should be updated to match, which is a
    better problem than believing a protection we do not have.
    """
    result = sandbox.run(["/usr/bin/id", "-un"], workspace)
    assert result.ok, (
        "account enumeration is expected to still work; if this now "
        "fails, update SECURITY.md, the gap has closed"
    )


def test_home_directories_still_cannot_be_walked(workspace) -> None:
    """The confinement that IS intact, alongside the gap above."""
    assert not sandbox.run(["/bin/ls", "/Users"], workspace).ok


def test_etc_is_denied_despite_the_bsd_import(workspace) -> None:
    """bsd.sb permits /etc; we deny it back.

    Apple's base profile allows /etc so processes can do user lookups.
    For a sandboxed agent that leaks usernames, home directories and
    shells. Later SBPL rules win, so the deny is applied after the
    import. Startup still works, which is the thing that could have
    broken.
    """
    assert not sandbox.run(["/bin/cat", "/etc/passwd"], workspace).ok
    # The resolved path too, not only the symlink.
    assert not sandbox.run(["/bin/cat", "/private/etc/passwd"], workspace).ok
    assert sandbox.run(["/bin/echo", "ok"], workspace).ok
    assert sandbox.run(["/bin/sh", "-c", "echo ok"], workspace).ok


def test_output_is_captured_through_pipes(workspace) -> None:
    """Under deny-default a controlling tty is not writable.

    A blanket file-write* does not cover it, and the failure blames
    stdout rather than the sandbox. run() always captures, so stdout
    must arrive intact rather than being refused.
    """
    result = sandbox.run(["/bin/echo", "captured"], workspace)
    assert result.ok
    assert "captured" in result.stdout
    assert "Operation not permitted" not in result.stderr
