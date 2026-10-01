"""Every relative link in the docs must point at something that exists (#233)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP = {".venv", "node_modules", ".git", ".pytest_cache", ".mypy_cache", ".ruff_cache",
        ".hypothesis"}
LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def broken_links(text: str, directory: Path) -> list[str]:
    """Relative link targets in ``text`` (outside code blocks) that do not exist."""
    prose = re.sub(r"```.*?```", "", text, flags=re.S)
    prose = re.sub(r"`[^`\n]*`", "", prose)
    bad = []
    for match in LINK.finditer(prose):
        target = match.group(1)
        if re.match(r"^(https?:|mailto:|#)", target):
            continue
        path = target.split("#", 1)[0].split("?", 1)[0]
        if path and not (directory / path).exists():
            bad.append(target)
    return bad


def test_the_detector_flags_missing_files_and_ignores_the_rest(tmp_path: Path) -> None:
    (tmp_path / "there.md").write_text("x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "img.png").write_text("x")
    text = "\n".join([
        "[ok](there.md) [ok anchor](there.md#part) [ok folder](sub/img.png)",
        "[gone](missing.md) [gone anchor](nowhere.md#part) ![img](sub/nope.png)",
        "[web](https://example.com/x) [mail](mailto:a@b.c) [same page](#top)",
        "`[in code](not-checked.md)`",
        "```",
        "[in a block](also-not-checked.md)",
        "```",
        '[with title](there.md "a title")',
    ])
    assert broken_links(text, tmp_path) == ["missing.md", "nowhere.md#part", "sub/nope.png"]


def test_every_relative_link_in_the_docs_resolves() -> None:
    problems = []
    for doc in sorted(ROOT.rglob("*.md")):
        if SKIP & set(doc.relative_to(ROOT).parts):
            continue
        problems += [f"{doc.relative_to(ROOT)} -> {target}"
                     for target in broken_links(doc.read_text(), doc.parent)]
    assert not problems, "links that point at nothing:\n" + "\n".join(problems)
