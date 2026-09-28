"""Read-only validator and inventory for skill directories (item 8.7a, #111).

A skill library is a directory of skill directories, each holding a
SKILL.md whose frontmatter names and describes it. This module checks
that shape and lists what is there with a content hash, so a change to a
skill is visible before anything is allowed to promote or sync it.

It never imports, executes or writes anything it scans. Frontmatter is
parsed by a strict subset parser (no YAML dependency): aliases, anchors
and tags are rejected outright rather than interpreted.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

MAX_DESCRIPTION = 1024
MAX_NAME = 64
MAX_FILE_BYTES = 1024 * 1024
MAX_SKILL_FILES = 500

NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_KEY_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.-]*):(?:[ \t]+(.*?))?[ \t]*$")
_BLOCK_RE = re.compile(r"^[|>][+-]?\d*$")
_JUNK = {".DS_Store", ".git", "__pycache__"}
# Forbidden commands are only searched where they can run: SKILL.md, executable
# files and these directories. Prose elsewhere may legitimately warn about them.
_CODE_DIRS = {"scripts", "bin"}

FORBIDDEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("permission prompts disabled", re.compile(r"--dangerously-skip-permissions")),
    (
        "download piped into a shell",
        re.compile(r"\b(?:curl|wget)\b[^\n|]*\|\s*(?:sudo\s+)?(?:ba|z)?sh\b"),
    ),
)

Report = Callable[[str, str], None]


class FrontmatterError(ValueError):
    pass


@dataclass(frozen=True)
class Problem:
    skill: str
    code: str
    message: str


@dataclass(frozen=True)
class Skill:
    name: str
    path: str
    description: str
    sha256: str
    files: int
    scripts: tuple[str, ...]


@dataclass
class ScanResult:
    skills: list[Skill] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)


def parse_frontmatter(text: str) -> dict[str, str]:
    lines = text.replace("\r\n", "\n").split("\n")
    if lines[0].rstrip() != "---":
        raise FrontmatterError("file does not start with a --- frontmatter block")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].rstrip() == "---")
    except StopIteration:
        raise FrontmatterError("frontmatter block is not closed with ---") from None

    entries: dict[str, tuple[str, list[str]]] = {}
    current: list[str] | None = None
    for raw in lines[1:end]:
        stripped = raw.strip()
        continuation = raw[:1] in (" ", "\t") or raw.startswith("- ")
        if continuation or (stripped == "" and current is not None):
            if current is None:
                raise FrontmatterError(f"unexpected indented line: {raw!r}")
            current.append(stripped)
            continue
        if stripped == "" or stripped.startswith("#"):
            continue
        match = _KEY_RE.match(raw)
        if match is None:
            raise FrontmatterError(f"not a key: value line: {raw!r}")
        key, value = match.group(1), match.group(2) or ""
        if key in entries:
            raise FrontmatterError(f"duplicate key: {key}")
        if value[:1] in ("&", "*", "!"):
            raise FrontmatterError(f"aliases, anchors and tags are not allowed: {key}")
        current = []
        entries[key] = (value, current)

    return {key: _scalar(key, value, cont) for key, (value, cont) in entries.items()}


def _scalar(key: str, value: str, cont: list[str]) -> str:
    while cont and cont[-1] == "":
        cont.pop()
    if _BLOCK_RE.match(value):
        if value[0] == "|":
            return "\n".join(cont).strip()
        return " ".join(part for part in cont if part)
    if value[:1] in ('"', "'"):
        if cont or len(value) < 2 or value[-1] != value[0]:
            raise FrontmatterError(f"unterminated quoted value: {key}")
        return value[1:-1]
    if value == "":
        return ""
    return " ".join([value, *(part for part in cont if part)])


def scan(root: Path) -> ScanResult:
    result = ScanResult()
    if not root.is_dir():
        result.problems.append(Problem("", "no-root", f"{root} is not a directory"))
        return result

    real_root = Path(os.path.realpath(root))
    seen: dict[str, str] = {}
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if entry.name.startswith(".") or not entry.is_dir():
            continue
        _scan_one(entry, real_root, result, seen)
    return result


def _inside(path: Path, real_root: Path) -> bool:
    real = Path(os.path.realpath(path))
    return real == real_root or real_root in real.parents


def _scan_one(directory: Path, real_root: Path, result: ScanResult, seen: dict[str, str]) -> None:
    def problem(code: str, message: str) -> None:
        result.problems.append(Problem(directory.name, code, message))

    if directory.is_symlink() and not _inside(directory, real_root):
        problem("symlink-escape", "skill directory is a symlink leaving the library")
        return

    files, scripts, digest = _walk(directory, real_root, problem)
    if files > MAX_SKILL_FILES:
        problem("too-many-files", f"{files} files, limit {MAX_SKILL_FILES}")

    skill_md = directory / "SKILL.md"
    if not skill_md.is_file():
        problem("missing-skill-md", "no SKILL.md")
        return
    try:
        meta = parse_frontmatter(skill_md.read_text(encoding="utf-8", errors="replace"))
    except (FrontmatterError, OSError) as exc:
        problem("frontmatter", str(exc))
        return

    name = meta.get("name", "")
    description = meta.get("description", "")
    for key, value in (("name", name), ("description", description)):
        if not value:
            problem("missing-field", f"{key} is missing or empty")
    if not name or not description:
        return

    if len(name) > MAX_NAME or not NAME_RE.match(name):
        problem(
            "bad-name",
            f"name {name!r} must be lowercase letters, digits and hyphens, "
            f"at most {MAX_NAME} characters",
        )
    elif name != directory.name:
        problem("name-mismatch", f"name {name!r} does not match directory {directory.name!r}")
    if len(description) > MAX_DESCRIPTION:
        problem("description-too-long", f"{len(description)} characters, limit {MAX_DESCRIPTION}")
    if name in seen:
        problem("duplicate-name", f"name {name!r} is also used by {seen[name]!r}")
    else:
        seen[name] = directory.name

    result.skills.append(
        Skill(
            name=name,
            path=str(directory),
            description=description,
            sha256=digest,
            files=files,
            scripts=tuple(sorted(scripts)),
        )
    )


def _walk(directory: Path, real_root: Path, problem: Report) -> tuple[int, list[str], str]:
    entries: list[tuple[str, Path]] = []
    for current, dirnames, filenames in os.walk(directory, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in _JUNK]
        for name in dirnames + filenames:
            path = Path(current, name)
            if name in _JUNK or (path.is_dir() and not path.is_symlink()):
                continue
            entries.append((path.relative_to(directory).as_posix(), path))

    tree = hashlib.sha256()
    scripts: list[str] = []
    for rel, path in sorted(entries):
        tree.update(rel.encode() + b"\0")
        if path.is_symlink():
            if not _inside(path, real_root):
                problem("symlink-escape", f"{rel} is a symlink leaving the library")
            tree.update(b"L" + os.readlink(path).encode() + b"\0")
            continue
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            problem("file-too-large", f"{rel} is {size} bytes, limit {MAX_FILE_BYTES}")
            tree.update(b"S" + str(size).encode() + b"\0")
            continue
        data = path.read_bytes()
        tree.update(b"F" + hashlib.sha256(data).digest())
        executable = os.access(path, os.X_OK)
        if executable:
            scripts.append(rel)
        if executable or rel == "SKILL.md" or rel.split("/")[0] in _CODE_DIRS:
            text = data.decode("utf-8", errors="replace")
            for label, pattern in FORBIDDEN_PATTERNS:
                if pattern.search(text):
                    problem("forbidden-pattern", f"{rel}: {label}")
    return len(entries), scripts, tree.hexdigest()
