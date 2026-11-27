"""The fresh-Mac instructions must be discoverable and internally linked (#435)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TROUBLESHOOTING = ROOT / "docs" / "TROUBLESHOOTING.md"
VALIDATION = ROOT / "ops" / "install-validation-template.md"
PAGES = (TROUBLESHOOTING, VALIDATION)
LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)", flags=re.DOTALL)


def _link_destination(raw: str) -> str:
    """The path part of a Markdown link destination, without title or fragment.

    Handles ``(path "Title")``, ``(path 'Title')`` and ``(<path with spaces>)``.
    """
    raw = raw.strip()
    if raw.startswith("<") and ">" in raw:
        destination = raw[1:raw.index(">")]
    else:
        destination = raw.split(None, 1)[0] if raw else ""
    return destination.split("#", 1)[0]


def _local_targets(page: Path) -> list[tuple[str, Path]]:
    """Every relative link on ``page`` as (raw text, resolved path)."""
    targets = []
    for raw in LINK_RE.findall(page.read_text(encoding="utf-8")):
        destination = _link_destination(raw)
        if not destination or "://" in destination or destination.startswith(("mailto:", "/")):
            continue
        targets.append((raw, (page.parent / destination).resolve()))
    return targets


@pytest.mark.parametrize(("raw", "expected"), [
    ("../ops/runbook.md", "../ops/runbook.md"),
    ('../ops/runbook.md "Runbook"', "../ops/runbook.md"),
    ("../ops/runbook.md 'Runbook'", "../ops/runbook.md"),
    ("INSTALL.md#troubleshooting", "INSTALL.md"),
    ("<docs/a file.md> \"Title\"", "docs/a file.md"),
])
def test_link_destination_drops_titles_and_fragments(raw: str, expected: str) -> None:
    assert _link_destination(raw) == expected


@pytest.mark.parametrize("page", PAGES)
def test_install_documents_have_no_missing_local_links(page: Path) -> None:
    """A wrong ../ path must fail CI, not make users discover it during install."""
    for raw_target, resolved in _local_targets(page):
        assert resolved.is_relative_to(ROOT), f"{page.name}: link escapes repository"
        assert resolved.is_file(), f"{page.name}: link target missing: {raw_target}"


def test_troubleshooting_covers_every_doctor_failure_area() -> None:
    text = TROUBLESHOOTING.read_text(encoding="utf-8")
    for check in (
        "database",
        "operator_key",
        "model",
        "disk",
        "backup",
        "selftest",
        "audit_chain",
        "services",
        "permissions",
        "lab_account",
        "versions",
        "loopback",
    ):
        assert check in text, f"missing diagnostic: {check}"
    for token in ("FAIL", "skip", "read-only", "FileVault", "SECURITY.md", "#188"):
        assert token in text
    assert "Do not edit the" in text
    assert "not proof" in text or "not demonstrate" in text


def test_fresh_account_template_remains_unexecuted_until_measured() -> None:
    text = VALIDATION.read_text(encoding="utf-8")
    for step in range(1, 10):
        assert f"| {step} |" in text, f"missing step {step}"
    assert "NOT RUN" in text
    assert "PASS" in text and "FAIL" in text and "SKIP" in text
    assert "never paste" in text.lower() or "never attach" in text.lower()
    assert "- [ ] An actual fresh-account or fresh-Mac test was run." in text


def test_install_docs_link_troubleshooting_and_validation() -> None:
    # The links themselves must resolve to the two pages; the filename appearing
    # as visible text next to a mistyped target is not enough.
    for page in (ROOT / "docs" / "INSTALL.md", ROOT / "README.md",
                 ROOT / "ops" / "install-validation.md"):
        resolved = {path for _raw, path in _local_targets(page)}
        assert TROUBLESHOOTING.resolve() in resolved, f"{page.name}: no working link to TROUBLESHOOTING.md"
        assert VALIDATION.resolve() in resolved, f"{page.name}: no working link to the validation template"
