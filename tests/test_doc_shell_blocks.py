"""Shell blocks in the docs get pasted into macOS's default zsh (#208).

That shell does not treat ``#`` as a comment when you paste, unless
``interactivecomments`` is set, so ``MODEL_REV="abc"   # note`` leaves the
variable empty and ``cat file   # expect: denied`` passes extra arguments. A doc
that has such comments in a shell block has to say so, once, before the block.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP = {".venv", "node_modules", ".git", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
SHELLS = {"sh", "bash", "zsh", "shell"}


def inline_comment_lines(text: str) -> list[int]:
    """Line numbers, inside fenced shell blocks, with a ``#`` after a command."""
    found: list[int] = []
    inside, language = False, ""
    for number, line in enumerate(text.splitlines(), 1):
        fence = re.match(r"^\s*```(\w*)", line)
        if fence:
            inside = not inside
            language = fence.group(1) if inside else ""
            continue
        if not (inside and language in SHELLS):
            continue
        body = line.strip()
        if not body or body.startswith("#"):
            continue
        outside_quotes = re.sub(r'"[^"]*"|\'[^\']*\'', '""', body)
        if re.search(r"\s#(\s|$)", outside_quotes):
            found.append(number)
    return found


def test_the_detector_finds_the_cases_that_bit_us_and_ignores_the_rest() -> None:
    text = "\n".join([
        "```sh",
        'MODEL_REV="abc"   # the build now served',
        "sudo -u lab /bin/cat key      # expect Permission denied",
        "# a whole-line comment is fine",
        'echo "a # b inside quotes is fine"',
        "echo 'also # fine'",
        "echo done",
        "```",
        "```python",
        "x = 1  # not a shell block",
        "```",
        "outside a block  # not code",
    ])
    assert inline_comment_lines(text) == [2, 3]


def test_a_doc_with_inline_comments_in_shell_blocks_carries_the_zsh_note() -> None:
    missing = []
    for doc in sorted(ROOT.rglob("*.md")):
        if SKIP & set(doc.relative_to(ROOT).parts):
            continue
        text = doc.read_text()
        if inline_comment_lines(text) and "interactivecomments" not in text:
            missing.append(str(doc.relative_to(ROOT)))
    assert not missing, (
        "these docs have inline # comments in shell blocks but do not tell the reader "
        f"to run `setopt interactivecomments` first (see #208): {missing}")
