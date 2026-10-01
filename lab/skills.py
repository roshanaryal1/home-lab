"""Read-only validator and inventory for skill directories (item 8.7a, #111).

A skill library is a directory of skill directories, each holding a
SKILL.md whose frontmatter names and describes it. This module checks
that shape and lists what is there with a content hash, so a change to a
skill is visible before anything is allowed to promote or sync it.

It never imports, executes or writes anything it scans. Frontmatter is
parsed by a strict subset parser (no YAML dependency): aliases, anchors
and tags are rejected outright rather than interpreted.

Hardening for imported skills (#254). SKILL.md and names may not carry
zero-width or bidi control characters. A name must be plain ASCII, so a
look-alike letter from another script is refused. A second SKILL.md below
the skill root is refused. A name one edit away from another skill in the
library, or from a known name, is reported as a typosquat. An
``allowed-tools`` field may only list tools the broker knows, at or below
the declared and the requested tier.
"""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from lab.broker import TOOL_TIERS
from lab.policy import Tier

MAX_DESCRIPTION = 1024
MAX_NAME = 64
MAX_FILE_BYTES = 1024 * 1024
MAX_SKILL_FILES = 500

NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_KEY_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.-]*):(?:[ \t]+(.*?))?[ \t]*$")
_BLOCK_RE = re.compile(r"^[|>][+-]?\d*$")
_JUNK = {".DS_Store", ".git", "__pycache__"}
# Zero-width, joiner, soft hyphen and bidi control characters. Each one can
# hide or reorder text so a reviewer reads something other than the model.
_INVISIBLE = re.compile(
    "[\u00ad\u061c\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")
_LIST_ITEM_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")
_WORD_RE = re.compile(r"[^\W\d_]{2,}")
TIER_ORDER = tuple(tier.value for tier in Tier)     # least to most restrictive
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
    entries = _entries(text)
    return {key: _scalar(key, value, cont) for key, (value, cont) in entries.items()}


def frontmatter_list(text: str, key: str) -> list[str] | None:
    """A list field, inline (``a, b`` or ``[a, b]``) or as ``- a`` lines.

    None when the key is absent. Each item must be a plain name: anything
    quoted, nested or with arguments is refused, not interpreted.
    """
    entry = _entries(text).get(key)
    if entry is None:
        return None
    value, cont = entry
    items: list[str] = []
    if value:
        if any(cont):
            raise FrontmatterError(f"{key}: give the list inline or as - lines, not both")
        inner = value[1:-1] if value.startswith("[") and value.endswith("]") else value
        items = [part for part in re.split(r"[,\s]+", inner) if part]
    else:
        for line in cont:
            if not line:
                continue
            if not line.startswith("- "):
                raise FrontmatterError(f"{key}: not a - list item: {line!r}")
            items.append(line[2:].strip())
    for item in items:
        if not _LIST_ITEM_RE.match(item):
            raise FrontmatterError(f"{key}: {item!r} is not a plain name")
    return items


def _entries(text: str) -> dict[str, tuple[str, list[str]]]:
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
    return entries


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


def one_edit_apart(a: str, b: str) -> bool:
    """True when one insertion, deletion, substitution or adjacent swap
    turns ``a`` into ``b``."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diff = [i for i in range(len(a)) if a[i] != b[i]]
        if len(diff) == 1:
            return True
        return (len(diff) == 2 and diff[1] == diff[0] + 1
                and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]])
    short, long = (a, b) if len(a) < len(b) else (b, a)
    i = 0
    while i < len(short) and short[i] == long[i]:
        i += 1
    return short[i:] == long[i + 1:]


def _scripts(text: str) -> set[str]:
    return {unicodedata.name(ch, "UNKNOWN").split()[0] for ch in text if ch.isalpha()}


def _name_problem(label: str, value: str) -> tuple[str, str] | None:
    if _INVISIBLE.search(value):
        return ("invisible-character",
                f"{label} {value!r} contains a zero-width or bidi control character")
    if not value.isascii():
        return ("homoglyph-name",
                f"{label} {value!r} is not plain ASCII (scripts: "
                f"{', '.join(sorted(_scripts(value)))}), a look-alike letter is refused")
    return None


def _rank(tier: str) -> int:
    return TIER_ORDER.index(tier)


def scan(root: Path, *, tier: str | None = None,
         known_names: Iterable[str] = ()) -> ScanResult:
    """Check every skill directory under ``root``.

    ``tier`` is the tier a submitter asks for: an ``allowed-tools`` list must
    fit it as well as any tier SKILL.md declares. ``known_names`` are names
    already in use elsewhere (a skill store), checked for typosquats along
    with the other skills in this library.
    """
    result = ScanResult()
    if not root.is_dir():
        result.problems.append(Problem("", "no-root", f"{root} is not a directory"))
        return result

    real_root = Path(os.path.realpath(root))
    seen: dict[str, str] = {}
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if entry.name.startswith(".") or not entry.is_dir():
            continue
        _scan_one(entry, real_root, result, seen, tier)
    _typosquats(result, set(known_names))
    return result


def _typosquats(result: ScanResult, known: set[str]) -> None:
    names = {skill.name for skill in result.skills} | known
    for skill in result.skills:
        near = sorted(other for other in names - {skill.name}
                      if one_edit_apart(skill.name, other))
        if near:
            result.problems.append(Problem(
                Path(skill.path).name, "typosquat",
                f"name {skill.name!r} is one edit away from {', '.join(map(repr, near))}"))


def _inside(path: Path, real_root: Path) -> bool:
    real = Path(os.path.realpath(path))
    return real == real_root or real_root in real.parents


def _scan_one(directory: Path, real_root: Path, result: ScanResult, seen: dict[str, str],
              tier: str | None) -> None:
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
    found = _name_problem("directory", directory.name)
    if found:
        problem(*found)
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
        meta = parse_frontmatter(text)
    except (FrontmatterError, OSError) as exc:
        problem("frontmatter", str(exc))
        return
    _check_text(text, problem)

    name = meta.get("name", "")
    description = meta.get("description", "")
    for key, value in (("name", name), ("description", description)):
        if not value:
            problem("missing-field", f"{key} is missing or empty")
    if not name or not description:
        return

    found = _name_problem("name", name)
    if found:
        problem(*found)
    elif len(name) > MAX_NAME or not NAME_RE.match(name):
        problem(
            "bad-name",
            f"name {name!r} must be lowercase letters, digits and hyphens, "
            f"at most {MAX_NAME} characters",
        )
    elif name != directory.name:
        problem("name-mismatch", f"name {name!r} does not match directory {directory.name!r}")
    if len(description) > MAX_DESCRIPTION:
        problem("description-too-long", f"{len(description)} characters, limit {MAX_DESCRIPTION}")
    _check_tools(text, meta.get("tier"), tier, problem)
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


def _check_text(text: str, problem: Report) -> None:
    for number, line in enumerate(text.splitlines(), 1):
        hidden = _INVISIBLE.search(line)
        if hidden:
            problem("invisible-character",
                    f"SKILL.md line {number} contains U+{ord(hidden.group()):04X}, "
                    "a zero-width or bidi control character")
            break
    for word in _WORD_RE.findall(text):
        scripts = _scripts(word)
        if len(scripts) > 1:
            problem("mixed-script", f"SKILL.md word {word!r} mixes scripts "
                    f"({', '.join(sorted(scripts))}), a look-alike spelling")
            break


def _check_tools(text: str, declared: str | None, requested: str | None,
                 problem: Report) -> None:
    try:
        tools = frontmatter_list(text, "allowed-tools")
    except FrontmatterError as exc:
        problem("frontmatter", str(exc))
        return
    if tools is None:
        return
    unknown = sorted(set(tools) - set(TOOL_TIERS))
    if unknown:
        problem("unknown-tool",
                f"allowed-tools lists tools the broker does not know: {', '.join(unknown)}")
    for label, tier in (("declared", declared), ("requested", requested)):
        if tier is None:
            continue
        if tier not in TIER_ORDER:
            problem("bad-tier", f"the {label} tier {tier!r} is not one of {', '.join(TIER_ORDER)}")
            continue
        over = sorted({tool for tool in tools
                       if tool in TOOL_TIERS and _rank(TOOL_TIERS[tool]) > _rank(tier)})
        if over:
            problem("tools-exceed-tier",
                    f"allowed-tools lists {', '.join(over)}, which the {label} tier "
                    f"{tier!r} does not permit")


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
        if "/" in rel and path.name.lower() == "skill.md":
            problem("nested-skill", f"{rel} is a SKILL.md below the skill root")
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
