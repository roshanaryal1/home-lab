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
# An ATX heading, without the optional closing run of #s. The linked pages use no setext
# headings and no HTML anchors, so these are the only anchors they have.
HEADING_RE = re.compile(r" {0,3}#{1,6}[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*")
FENCE_RE = re.compile(r" {0,3}(`{3,}|~{3,})")
FENCED_BLOCK_RE = re.compile(r"^```[^\n]*\n(.*?)^```", flags=re.DOTALL | re.MULTILINE)
INLINE_CODE_RE = re.compile(r"`([^`]+)`")
DEPLOYED_PYTHON = "/opt/homelab/.venv/bin/python"


def _link_destination(raw: str) -> tuple[str, str]:
    """The path and the fragment of a Markdown link destination, without its title.

    Handles ``(path "Title")``, ``(path 'Title')`` and ``(<path with spaces>)``.
    """
    raw = raw.strip()
    if raw.startswith("<") and ">" in raw:
        destination = raw[1:raw.index(">")]
    else:
        destination = raw.split(None, 1)[0] if raw else ""
    path, _, fragment = destination.partition("#")
    return path, fragment


def _is_external(path: str) -> bool:
    return "://" in path or path.startswith(("mailto:", "/"))


def _local_targets(page: Path) -> list[tuple[str, Path]]:
    """Every relative link to another file on ``page`` as (raw text, resolved path)."""
    targets = []
    for raw in LINK_RE.findall(page.read_text(encoding="utf-8")):
        path, _fragment = _link_destination(raw)
        if not path or _is_external(path):
            continue
        targets.append((raw, (page.parent / path).resolve()))
    return targets


def _github_anchor(heading: str) -> str:
    """The anchor GitHub gives a heading: markup removed, lower case, punctuation other
    than hyphens and underscores dropped, and each space a hyphen."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)     # a link keeps its text
    text = re.sub(r"(?<!\w)_+|_+(?!\w)", "", text.replace("`", ""))  # _emphasis_, not a_b
    text = re.sub(r"[^\w\- ]", "", text.strip().lower())
    return text.replace(" ", "-")


def _anchors(page: Path) -> set[str]:
    """The anchor of every heading on ``page`` outside fenced code. A repeated one gets
    -1, -2 and so on, as GitHub numbers them."""
    anchors: set[str] = set()
    seen: dict[str, int] = {}
    fence = ""
    for line in page.read_text(encoding="utf-8").splitlines():
        marker = FENCE_RE.match(line)
        if marker:
            run = marker.group(1)
            if not fence:
                fence = run
            elif run[0] == fence[0] and len(run) >= len(fence) and not line.strip().strip(run[0]):
                fence = ""
            continue
        heading = None if fence else HEADING_RE.fullmatch(line)
        if heading is None:
            continue
        anchor = _github_anchor(heading.group(1))
        count = seen.get(anchor, 0)
        seen[anchor] = count + 1
        anchors.add(f"{anchor}-{count}" if count else anchor)
    return anchors


def _link_problems(page: Path, root: Path = ROOT) -> list[str]:
    """Every relative link on ``page`` that leaves ``root``, names no file, or names a
    heading its Markdown target does not have."""
    problems = []
    for raw in LINK_RE.findall(page.read_text(encoding="utf-8")):
        path, fragment = _link_destination(raw)
        if _is_external(path) or not (path or fragment):
            continue
        target = (page.parent / path).resolve() if path else page.resolve()
        if not target.is_relative_to(root.resolve()):
            problems.append(f"escapes the repository: {raw}")
        elif not target.is_file():
            problems.append(f"no such file: {raw}")
        elif fragment and target.suffix == ".md" and fragment not in _anchors(target):
            problems.append(f"no such heading: {raw}")
    return problems


def _commands(page: Path) -> list[str]:
    """Each line of the page's fenced code, continuation lines joined, and each inline
    code span, with its whitespace collapsed."""
    text = page.read_text(encoding="utf-8")
    commands = []
    for block in FENCED_BLOCK_RE.findall(text):
        commands += block.replace("\\\n", " ").splitlines()
    commands += INLINE_CODE_RE.findall(FENCED_BLOCK_RE.sub("", text))
    return [" ".join(command.split()) for command in commands]


@pytest.mark.parametrize(("raw", "expected"), [
    ("../ops/runbook.md", ("../ops/runbook.md", "")),
    ('../ops/runbook.md "Runbook"', ("../ops/runbook.md", "")),
    ("../ops/runbook.md 'Runbook'", ("../ops/runbook.md", "")),
    ("INSTALL.md#troubleshooting", ("INSTALL.md", "troubleshooting")),
    ("<docs/a file.md> \"Title\"", ("docs/a file.md", "")),
    ("#start-here", ("", "start-here")),
])
def test_link_destination_drops_titles_and_keeps_fragments(
        raw: str, expected: tuple[str, str]) -> None:
    assert _link_destination(raw) == expected


@pytest.mark.parametrize(("heading", "anchor"), [
    ("Step 5. Start the services, one at a time (sudo)",
     "step-5-start-the-services-one-at-a-time-sudo"),
    ("Moving the backup's Full Disk Access to its launcher (#287, once)",
     "moving-the-backups-full-disk-access-to-its-launcher-287-once"),
    ("Step 3. Deploy the code to /opt/homelab (sudo)", "step-3-deploy-the-code-to-opthomelab-sudo"),
    ("`uv sync --locked` fails", "uv-sync---locked-fails"),
    ("This'll be a _Helpful_ Section", "thisll-be-a-helpful-section"),
    ("The `lab_account` check", "the-lab_account-check"),
])
def test_github_anchor_matches_githubs_rules(heading: str, anchor: str) -> None:
    assert _github_anchor(heading) == anchor


def test_link_check_finds_a_missing_file_or_heading(tmp_path: Path) -> None:
    """A renamed or mistyped heading must fail, not only a missing file."""
    (tmp_path / "runbook.md").write_text(
        "# Runbook\n\n## Step 2. The lab account (sudo)\n\n"
        "```sh\n## Not a heading\n```\n\n## Step 2. The lab account (sudo)\n",
        encoding="utf-8")
    page = tmp_path / "docs" / "guide.md"
    page.parent.mkdir()
    page.write_text(
        "# Guide\n\n"
        "[ok](../runbook.md#step-2-the-lab-account-sudo) "
        "[second](../runbook.md#step-2-the-lab-account-sudo-1) "
        "[self](#guide) [file](../runbook.md) "
        "[renamed](../runbook.md#step-3-the-lab-account-sudo) "
        "[fenced](../runbook.md#not-a-heading) [here](#nowhere) "
        "[missing](missing.md) [outside](../../outside.md)\n",
        encoding="utf-8")
    assert _link_problems(page, root=tmp_path) == [
        "no such heading: ../runbook.md#step-3-the-lab-account-sudo",
        "no such heading: ../runbook.md#not-a-heading",
        "no such heading: #nowhere",
        "no such file: missing.md",
        "escapes the repository: ../../outside.md",
    ]


@pytest.mark.parametrize("page", PAGES)
def test_install_documents_have_no_broken_local_links(page: Path) -> None:
    """A wrong ../ path or a renamed heading must fail CI, not strand a user mid-install."""
    assert _link_problems(page) == []


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


def test_troubleshooting_runs_the_deployed_build_only_as_lab() -> None:
    """As the owner, code the lab account may have changed could read operator.key."""
    for command in _commands(TROUBLESHOOTING):
        start = command.find(DEPLOYED_PYTHON + " ")
        if start != -1:
            assert "sudo -u lab " in command[:start], command


def test_troubleshooting_diagnosis_never_resyncs_or_hides_errors() -> None:
    commands = _commands(TROUBLESHOOTING)
    # uv run locks and syncs the project first, changing what is being diagnosed.
    assert not [c for c in commands if re.match(r"uv run \S", c)]
    # A setting that cannot be read must stop the diagnosis, not become an empty value.
    assert not [c for c in commands if "plutil" in c and "2>/dev/null" in c]
    # Each job that talks to the model is diagnosed with its own settings.
    assert {"lab_diagnose supervisor", "lab_diagnose tick"} <= set(commands)


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
        assert TROUBLESHOOTING.resolve() in resolved, (
            f"{page.name}: no working link to TROUBLESHOOTING.md")
        assert VALIDATION.resolve() in resolved, (
            f"{page.name}: no working link to the validation template")
