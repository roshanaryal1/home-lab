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


@pytest.mark.parametrize("page", PAGES)
def test_install_documents_have_no_missing_local_links(page: Path) -> None:
    """A wrong ../ path must fail CI, not make users discover it during install."""
    source = page.read_text(encoding="utf-8")
    for raw_target in LINK_RE.findall(source):
        target = raw_target.strip().split("#", 1)[0]
        if not target or "://" in target or target.startswith(("mailto:", "/")):
            continue
        resolved = (page.parent / target).resolve()
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
    install = (ROOT / "docs" / "INSTALL.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    validation = (ROOT / "ops" / "install-validation.md").read_text(encoding="utf-8")
    for text in (install, readme, validation):
        assert "TROUBLESHOOTING.md" in text
        assert "install-validation-template.md" in text
