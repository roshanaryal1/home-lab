"""The built wheel carries every module under lab/ (#329).

setuptools installs only the packages pyproject.toml names. The wheel
once had no lab/handlers/ and no lab/migrations/__init__.py, so
`import lab.cli` failed from an installed wheel while the source tree
still worked.
"""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def wheel_names(tmp_path_factory: pytest.TempPathFactory) -> set[str]:
    if shutil.which("uv") is None:
        pytest.skip("uv is not on PATH, so the wheel cannot be built")
    # Built from a copy, never in place: setuptools reuses a build/ directory
    # left by an earlier build, so an in-place build can ship files that the
    # pyproject no longer lists.
    source = tmp_path_factory.mktemp("source")
    for name in ("pyproject.toml", ".python-version", "LICENSE"):
        shutil.copy(ROOT / name, source / name)
    shutil.copytree(ROOT / "lab", source / "lab", ignore=shutil.ignore_patterns("__pycache__"))
    out = tmp_path_factory.mktemp("dist")
    subprocess.run(["uv", "build", "--wheel", "--out-dir", str(out)], cwd=source, check=True,
                   capture_output=True, text=True, timeout=600)
    (wheel,) = out.glob("*.whl")
    with zipfile.ZipFile(wheel) as archive:
        return set(archive.namelist())


def _relative(paths: list[Path]) -> set[str]:
    return {path.relative_to(ROOT).as_posix() for path in paths}


def test_the_wheel_has_every_python_module_under_lab(wheel_names: set[str]) -> None:
    expected = _relative(list((ROOT / "lab").rglob("*.py")))
    missing = sorted(expected - wheel_names)
    assert not missing, f"missing from the wheel: {missing}"


def test_the_wheel_has_every_migration(wheel_names: set[str]) -> None:
    expected = _relative(list((ROOT / "lab" / "migrations").glob("*.sql")))
    assert expected, "no migrations found under lab/migrations"
    missing = sorted(expected - wheel_names)
    assert not missing, f"missing from the wheel: {missing}"
