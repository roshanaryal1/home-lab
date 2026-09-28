"""CITATION.cff stays valid and in step with the package (item 8.2, #83).

A DOI lets a reviewer cite the exact code and data, so the file must parse,
carry what the Citation File Format 1.2.0 requires, and not drift from
pyproject.toml. There is no YAML dependency in this project, so the file is
read with a strict reader that supports exactly the shapes it uses.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEXT = (ROOT / "CITATION.cff").read_text(encoding="utf-8")


def read_top_level(text: str) -> dict[str, object]:
    """Top-level keys with scalar, folded (>-) or list values. Nothing else."""
    out: dict[str, object] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.startswith("#"):
            i += 1
            continue
        match = re.match(r"^([A-Za-z][A-Za-z0-9-]*):(?:\s+(.*))?$", line)
        assert match, f"unsupported line: {line!r}"
        key, value = match.group(1), (match.group(2) or "").strip()
        assert key not in out, f"duplicate key {key}"
        i += 1
        if value == ">-":
            block = []
            while i < len(lines) and lines[i].startswith("  "):
                block.append(lines[i].strip())
                i += 1
            out[key] = " ".join(block)
        elif value == "":
            items: list[str] = []
            while i < len(lines) and lines[i].startswith("  "):
                items.append(lines[i].strip())
                i += 1
            out[key] = items
        else:
            out[key] = value.strip("\"'")
    return out


DATA = read_top_level(TEXT)


def test_required_keys_of_cff_1_2_0_are_present() -> None:
    assert DATA["cff-version"] == "1.2.0"
    for key in ("message", "title", "authors"):
        assert DATA.get(key), f"{key} is required"


def test_every_author_has_a_name() -> None:
    authors = DATA["authors"]
    assert isinstance(authors, list)
    assert any("family-names: Aryal" in a for a in authors)
    assert any("given-names: Roshan" in a for a in authors)


def test_type_is_software_and_the_date_is_iso() -> None:
    assert DATA["type"] in ("software", "dataset")
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(DATA["date-released"]))


def test_version_and_license_match_the_package() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert DATA["version"] == pyproject["version"]
    assert DATA["license"] == "MIT" and "MIT License" in (ROOT / "LICENSE").read_text()


def test_the_repository_url_is_this_repository() -> None:
    assert DATA["repository-code"] == "https://github.com/roshanaryal1/home-lab"


def test_no_credentials_or_personal_data_beyond_the_author_name() -> None:
    lowered = TEXT.lower()
    for forbidden in ("orcid", "email", "@", "token", "secret", "password"):
        assert forbidden not in lowered
