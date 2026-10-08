"""The read-only prerequisite check (#188): what it says for each Mac it can meet.

The script is run against stub commands, so every case is deterministic and the
same on a Mac and on the Linux CI runner.
"""

from __future__ import annotations

import re
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check-prerequisites.sh"
GB = 1024 ** 3


def _stub(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def run(tmp_path: Path, *, system: str = "Darwin", arch: str = "arm64", memory_gb: int = 32,
        free_gb: int = 200, with_uv: bool = True,
        macos: str = "27.0") -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    _stub(bin_dir, "uname", f'case "$1" in -m) echo {arch};; *) echo {system};; esac')
    _stub(bin_dir, "sw_vers", f"echo {macos}")
    _stub(bin_dir, "sysctl", f"echo {memory_gb * GB}")
    _stub(bin_dir, "df", 'echo "Filesystem 1024-blocks Used Available Capacity"; '
                         f'echo "/dev/x 1 1 {free_gb * 1024 * 1024} 1%"')
    _stub(bin_dir, "git", 'echo "git version 2.50.0"')
    _stub(bin_dir, "xcrun", "exit 0")
    if with_uv:
        _stub(bin_dir, "uv", 'echo "uv 0.12.0"')
    return subprocess.run(["/bin/sh", str(SCRIPT)], capture_output=True, text=True, timeout=30,
                          env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(home)})


def test_a_32_gb_apple_silicon_mac_passes_and_the_tier_is_marked_measured(tmp_path) -> None:
    result = run(tmp_path)
    assert result.returncode == 0, result.stdout
    assert "MODEL TIER (measured): 32 GB" in result.stdout
    assert "Prerequisite check PASSED. No changes were made." in result.stdout


def test_the_other_sizes_are_marked_untested_and_never_as_a_recommendation(tmp_path) -> None:
    for gb in (16, 64):
        sub = tmp_path / str(gb)
        sub.mkdir()
        out = run(sub, memory_gb=gb).stdout
        assert f"MODEL TIER (untested): {gb} GB" in out, out
        assert "RECOMMENDED" not in out


def test_an_unlisted_size_gets_no_tier_at_all(tmp_path) -> None:
    out = run(tmp_path, memory_gb=24).stdout
    assert "no tested tier is recorded for 24 GB" in out
    assert "MODEL TIER (measured)" not in out and "(untested)" not in out


def test_a_machine_that_is_not_a_mac_fails(tmp_path) -> None:
    result = run(tmp_path, system="Linux")
    assert result.returncode == 1
    assert "MISSING: macOS" in result.stdout and "FAILED" in result.stdout


def test_a_macos_older_than_mlx_supports_fails_and_names_the_floor(tmp_path) -> None:
    """MLX's install page asks for macOS 14.0 or later (#188)."""
    result = run(tmp_path, macos="13.6.1")
    assert result.returncode == 1
    assert "MISSING: macOS 14 or later (detected: 13.6.1)" in result.stdout


def test_macos_14_and_later_pass_the_version_check(tmp_path) -> None:
    for version in ("14.0", "15.7", "27.0"):
        sub = tmp_path / version
        sub.mkdir()
        result = run(sub, macos=version)
        assert result.returncode == 0, result.stdout
        assert f"OK: macOS: {version}" in result.stdout


def test_an_unreadable_macos_version_is_reported_not_guessed(tmp_path) -> None:
    result = run(tmp_path, macos="")
    assert result.returncode == 0, result.stdout
    assert "INFO: could not read the macOS version" in result.stdout
    assert "OK: macOS" not in result.stdout


def test_an_intel_mac_fails(tmp_path) -> None:
    result = run(tmp_path, arch="x86_64")
    assert result.returncode == 1 and "MISSING: Apple silicon" in result.stdout


def test_a_missing_uv_fails_and_says_so(tmp_path) -> None:
    result = run(tmp_path, with_uv=False)
    assert result.returncode == 1 and "MISSING: uv" in result.stdout


def test_too_little_free_space_fails(tmp_path) -> None:
    result = run(tmp_path, free_gb=10)
    assert result.returncode == 1 and "at least 30 GB free" in result.stdout


def test_it_leaves_no_file_behind(tmp_path) -> None:
    run(tmp_path)
    assert list((tmp_path / "home").iterdir()) == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bin", "home"]


def test_the_script_has_no_command_that_escalates_or_writes() -> None:
    code = [line for line in SCRIPT.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")]
    text = "\n".join(code)
    assert not re.search(r"\b(sudo|rm|mv|cp|mkdir|touch|chmod|chown|tee|dd|curl|wget)\b", text)
    unquoted = re.sub(r"'[^']*'|\"[^\"]*\"", "", text)
    without_devnull = re.sub(r"[0-9]?>\s*/dev/null|2>&1", "", unquoted)
    assert ">" not in without_devnull, "the script redirects output to a file"


def test_the_script_is_executable_ends_with_a_newline_and_uses_no_em_dash() -> None:
    assert SCRIPT.stat().st_mode & stat.S_IXUSR
    text = SCRIPT.read_text()
    assert text.endswith("\n") and chr(0x2014) not in text
