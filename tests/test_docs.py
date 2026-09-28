"""The documents keep up with the code."""

from __future__ import annotations

import tomllib
from pathlib import Path

from lab.cli import build_parser

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")


def test_every_cli_command_is_documented_in_the_readme() -> None:
    parser = build_parser()
    commands = next(a for a in parser._actions if a.dest == "command").choices
    missing = [c for c in commands if f"lab.cli {c}" not in README]
    assert not missing, f"README 'Operating the lab' does not mention: {missing}"


def test_the_readme_dependency_claim_matches_pyproject() -> None:
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    if deps:
        assert "No dependencies beyond the standard library" not in README
        names = [d.split(">")[0].split("=")[0].strip() for d in deps]
        assert all(n in README for n in names), f"README does not name {names}"


def test_every_adr_is_reachable_from_the_readme_or_architecture() -> None:
    index = README + (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    for adr in sorted((ROOT / "docs" / "decisions").glob("*.md")):
        assert adr.name in index or adr.name.split("-")[0] in index, f"{adr.name} is orphaned"


def test_contributing_does_not_hard_code_the_safety_floor() -> None:
    text = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "fewer than 48" not in text


def test_the_readme_points_at_the_citation_file_and_the_release_steps() -> None:
    assert "CITATION.cff" in README and "ops/release.md" in README
    assert (ROOT / "CITATION.cff").is_file() and (ROOT / "ops" / "release.md").is_file()
