"""Skills and executors as versioned artifacts, with a rollback path (item 8.7, #87).

Learned or adopted skills can quietly widen what an agent does. So a skill
is never edited in place:

* Each submission is a new immutable **version**: its files go into the
  content-addressed artifact store, its manifest and hash into the table,
  and it records the version it derives from and where it came from.
* A new version is a **candidate**. Only a promotion makes it active, and
  a promotion is an operator action: signed with the operator's key when
  one is configured, and never by whoever submitted it.
* A skill **cannot lower its own permission tier**. The tier is set by the
  submitter and the promoter, never by the skill: if SKILL.md declares a
  ``tier``, the stricter of the two wins, and a promotion may not be less
  restrictive than the version it replaces unless the operator says so in
  the signed request. A skill with executable files is never below
  ``approve``.
* A version can be marked **known good** (with the evidence), and a
  **rollback** is one step back to the newest known-good version, with the
  current one recorded as rolled back rather than deleted.
* Anything installed is re-verified against its recorded hashes on the way
  out, so a tampered store entry cannot be installed.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from lab import operator as operator_keys
from lab import skills
from lab.artifacts import ArtifactError, ArtifactStore
from lab.audit import append_event

TIER_ORDER = ("autonomous", "notify", "approve", "never")     # least to most restrictive
MAX_VERSION_FILES = 500


class SkillStoreError(ValueError):
    """The skill store refused an operation."""


def _rank(tier: str) -> int:
    try:
        return TIER_ORDER.index(tier)
    except ValueError:
        raise SkillStoreError(f"unknown tier {tier!r}") from None


def stricter(a: str, b: str) -> str:
    return a if _rank(a) >= _rank(b) else b


@dataclass(frozen=True)
class Installed:
    name: str
    version: int
    path: Path
    tier: str


class SkillStore:
    def __init__(self, conn: sqlite3.Connection, store: ArtifactStore,
                 operator_public_key: Ed25519PublicKey | None = None) -> None:
        self._conn = conn
        self._store = store
        self._key = operator_public_key

    # ---------------------------------------------------------------- reading

    def get(self, version_id: int) -> sqlite3.Row:
        row: sqlite3.Row | None = self._conn.execute(
            "SELECT * FROM skill_versions WHERE id = ?", (version_id,)).fetchone()
        if row is None:
            raise SkillStoreError(f"no skill version {version_id}")
        return row

    def active(self, name: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._conn.execute(
            "SELECT * FROM skill_versions WHERE name = ? AND state = 'active'",
            (name,)).fetchone()
        return row

    def history(self, name: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM skill_versions WHERE name = ? ORDER BY version", (name,)).fetchall()

    # ---------------------------------------------------------------- submitting

    def submit(self, directory: Path, requested_tier: str, submitted_by: str, *,
               derived_from: str | None = None) -> int:
        """Store a skill directory as a new candidate version."""
        _rank(requested_tier)
        if not submitted_by.strip():
            raise SkillStoreError("say who is submitting")
        directory = Path(directory)
        scan = skills.scan(directory.parent) if directory.is_dir() else None
        mine = [s for s in (scan.skills if scan else []) if Path(s.path).name == directory.name]
        problems = [p for p in (scan.problems if scan else []) if p.skill == directory.name]
        if scan is None or not mine or problems:
            detail = "; ".join(f"{p.code}: {p.message}" for p in problems) or "not a skill"
            raise SkillStoreError(f"the validator refuses this skill ({detail})")
        skill = mine[0]

        manifest, has_scripts = self._ingest(directory)
        declared = self._declared_tier(directory / "SKILL.md")
        tier = stricter(requested_tier, declared) if declared else requested_tier
        if has_scripts:
            tier = stricter(tier, "approve")
        parent = self.active(skill.name)
        number = int(self._conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM skill_versions WHERE name = ?",
            (skill.name,)).fetchone()[0])
        cur = self._conn.execute(
            "INSERT INTO skill_versions (name, version, tier, declared_tier, content_sha256, "
            "manifest, has_scripts, parent_id, derived_from, submitted_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (skill.name, number, tier, declared, skill.sha256,
             json.dumps(manifest, sort_keys=True), int(has_scripts),
             parent["id"] if parent else None, derived_from, submitted_by.strip()))
        version_id = int(cur.lastrowid or 0)
        append_event(self._conn, None, "skill_submitted", detail={
            "skill": skill.name, "version": number, "tier": tier, "declared_tier": declared,
            "content_sha256": skill.sha256, "by": submitted_by, "parent": parent["id"]
            if parent else None})
        return version_id

    def _ingest(self, directory: Path) -> tuple[dict[str, list[object]], bool]:
        manifest: dict[str, list[object]] = {}
        has_scripts = False
        for current, dirnames, filenames in os.walk(directory, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if d not in (".git", "__pycache__"))
            for fname in sorted(filenames):
                path = Path(current, fname)
                if fname in (".DS_Store",):
                    continue
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    raise SkillStoreError(f"{path.relative_to(directory)} is not a plain file")
                if len(manifest) >= MAX_VERSION_FILES:
                    raise SkillStoreError("too many files")
                sha, _ = self._store.put_bytes(path.read_bytes())
                executable = bool(info.st_mode & stat.S_IXUSR)
                has_scripts = has_scripts or executable
                manifest[path.relative_to(directory).as_posix()] = [sha, 0o755 if executable
                                                                   else 0o644]
        return manifest, has_scripts

    @staticmethod
    def _declared_tier(skill_md: Path) -> str | None:
        meta = skills.parse_frontmatter(skill_md.read_text(encoding="utf-8", errors="replace"))
        value = meta.get("tier")
        if value is None:
            return None
        if value not in TIER_ORDER:
            raise SkillStoreError(f"SKILL.md declares an unknown tier {value!r}")
        return value

    # ----------------------------------------------------- signed operator actions

    def _authorise(self, purpose: str, by: str, signature: str | None, **fields: object) -> None:
        if not by.strip():
            raise SkillStoreError("say who is doing this")
        if self._key is not None and not operator_keys.verify_action(
                self._key, signature, purpose, by=by.strip(), **fields):
            raise SkillStoreError(f"{purpose} needs a valid operator signature")

    def promote(self, version_id: int, promoted_by: str, *, signature: str | None = None,
                allow_loosen: bool = False) -> None:
        """Make a candidate the active version. Operator only."""
        row = self.get(version_id)
        if row["state"] != "candidate":
            raise SkillStoreError(f"version {version_id} is {row['state']}, not a candidate")
        if promoted_by.strip() == row["submitted_by"]:
            raise SkillStoreError("a version cannot be promoted by whoever submitted it")
        self._authorise("skill-promote", promoted_by, signature, skill=row["name"],
                        version=row["version"], content_sha256=row["content_sha256"],
                        tier=row["tier"], allow_loosen=allow_loosen)
        current = self.active(row["name"])
        if current is not None and _rank(row["tier"]) < _rank(current["tier"]) \
                and not allow_loosen:
            raise SkillStoreError(
                f"tier {row['tier']!r} is less restrictive than the active version's "
                f"{current['tier']!r}; a skill cannot lower its own tier (the operator "
                "must say so explicitly)")
        problems = self.verify_version(version_id)
        if problems:
            raise SkillStoreError("stored files no longer match: " + "; ".join(problems))
        if current is not None:
            self._conn.execute("UPDATE skill_versions SET state = 'superseded' WHERE id = ?",
                               (current["id"],))
        self._conn.execute(
            "UPDATE skill_versions SET state = 'active', promoted_by = ?, "
            "promoted_at = strftime('%Y-%m-%d %H:%M:%f', 'now') WHERE id = ?",
            (promoted_by.strip(), version_id))
        append_event(self._conn, None, "skill_promoted", detail={
            "skill": row["name"], "version": row["version"], "tier": row["tier"],
            "by": promoted_by, "replaces": current["version"] if current else None})

    def reject(self, version_id: int, by: str, reason: str) -> None:
        row = self.get(version_id)
        if row["state"] != "candidate":
            raise SkillStoreError(f"version {version_id} is {row['state']}, not a candidate")
        self._conn.execute("UPDATE skill_versions SET state = 'rejected' WHERE id = ?",
                           (version_id,))
        append_event(self._conn, None, "skill_rejected", detail={
            "skill": row["name"], "version": row["version"], "by": by, "reason": reason})

    def mark_known_good(self, version_id: int, by: str, evidence: str, *,
                        signature: str | None = None) -> None:
        """Record that a version passed whatever check you trust (an eval record
        hash, a test run). Only an active version can be marked."""
        row = self.get(version_id)
        if row["state"] != "active":
            raise SkillStoreError("only the active version can be marked known good")
        if not evidence.strip():
            raise SkillStoreError("known good needs evidence: an eval record, a test run")
        self._authorise("skill-known-good", by, signature, skill=row["name"],
                        version=row["version"], evidence=evidence.strip())
        self._conn.execute(
            "UPDATE skill_versions SET known_good = 1, known_good_by = ?, "
            "known_good_evidence = ? WHERE id = ?", (by.strip(), evidence.strip(), version_id))
        append_event(self._conn, None, "skill_known_good", detail={
            "skill": row["name"], "version": row["version"], "by": by,
            "evidence": evidence.strip()})

    def rollback(self, name: str, by: str, *, signature: str | None = None,
                 allow_loosen: bool = False) -> int:
        """One step back to the newest known-good version before the active one."""
        current = self.active(name)
        if current is None:
            raise SkillStoreError(f"{name!r} has no active version")
        target = self._conn.execute(
            "SELECT * FROM skill_versions WHERE name = ? AND known_good = 1 AND id != ? "
            "AND version < ? ORDER BY version DESC LIMIT 1",
            (name, current["id"], current["version"])).fetchone()
        if target is None:
            raise SkillStoreError(f"{name!r} has no earlier known-good version to return to")
        self._authorise("skill-rollback", by, signature, skill=name,
                        to_version=target["version"], allow_loosen=allow_loosen)
        if _rank(target["tier"]) < _rank(current["tier"]) and not allow_loosen:
            raise SkillStoreError("the known-good version has a less restrictive tier; "
                                  "rolling back must not lower it without an explicit say-so")
        problems = self.verify_version(target["id"])
        if problems:
            raise SkillStoreError("the known-good version no longer verifies: "
                                  + "; ".join(problems))
        self._conn.execute("UPDATE skill_versions SET state = 'rolled_back' WHERE id = ?",
                           (current["id"],))
        self._conn.execute("UPDATE skill_versions SET state = 'active' WHERE id = ?",
                           (target["id"],))
        append_event(self._conn, None, "skill_rolled_back", detail={
            "skill": name, "from": current["version"], "to": target["version"], "by": by})
        return int(target["version"])

    # ------------------------------------------------------------ verify, install

    def verify_version(self, version_id: int) -> list[str]:
        row = self.get(version_id)
        problems = []
        for rel, (sha, _mode) in json.loads(row["manifest"]).items():
            try:
                self._store.read(sha)
            except ArtifactError as exc:
                problems.append(f"{rel}: {exc}")
        return problems

    def install(self, name: str, dest_root: Path) -> Installed:
        """Write the active version to ``dest_root/name``, replacing the previous
        install in one rename, after re-verifying every file."""
        row = self.active(name)
        if row is None:
            raise SkillStoreError(f"{name!r} has no active version")
        dest_root = Path(dest_root)
        dest_root.mkdir(parents=True, exist_ok=True)
        staging_parent = Path(tempfile.mkdtemp(prefix=".stage-", dir=dest_root))
        staging = staging_parent / name
        staging.mkdir()
        try:
            for rel, (sha, mode) in json.loads(row["manifest"]).items():
                target = staging / rel
                if not target.resolve().is_relative_to(staging.resolve()):
                    raise SkillStoreError(f"{rel} escapes the skill directory")
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    target.write_bytes(self._store.read(sha))
                except ArtifactError as exc:
                    raise SkillStoreError(f"{rel}: {exc}") from None
                os.chmod(target, int(mode))
            check = skills.scan(staging_parent)
            built = [s for s in check.skills if Path(s.path) == staging]
            if not built or check.problems or built[0].sha256 != row["content_sha256"]:
                raise SkillStoreError("installed files do not match the recorded content hash")
            final = dest_root / name
            backup = dest_root / f".{name}.previous"
            if final.exists():
                shutil.rmtree(backup, ignore_errors=True)
                os.replace(final, backup)
            os.replace(staging, final)
            shutil.rmtree(backup, ignore_errors=True)
            shutil.rmtree(staging_parent, ignore_errors=True)
        except BaseException:
            shutil.rmtree(staging_parent, ignore_errors=True)
            raise
        return Installed(name, int(row["version"]), final, row["tier"])
