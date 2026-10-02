"""Repository sources the operator signed, and the record of each acquisition (ADR 0008).

A repository enters a task's workspace only through the broker tool
``workspace.acquire``, and only from a source on this list. The rule from
ADR 0008: a repository never appears in a workspace by a shell command or a
file copy, and every acquisition can later answer "which repository, which
revision, in which workspace, under which task".

The signed list
---------------

Each entry names a source and the absolute path of a git repository the
operator keeps on this machine (a mirror the operator updates). The whole
entry is signed with the operator key (``lab.operator.sign_action``), like an
MCP server entry. An entry that is unsigned, or whose signature does not
verify, cannot be acquired from. Nor can any entry when no operator public key
is configured.

Only local sources for now. Fetching from the network would need a git
transport through the egress gateway, which does not exist. The operator
updates the mirror; the lab copies from it at an exact revision.

The record
----------

``workspace_acquisitions`` holds one row per acquisition: the task, the source
entry and its digest and signer, the exact revision and its tree, and where in
which workspace it was placed. Rows are written by the broker and never
updated. ``lab repo acquired`` prints them.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from lab import operator as operator_keys

PURPOSE = "repo-source"

_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
# An exact commit, as git names it with SHA-1. Never a branch or a tag, which
# can move between the approval and the copy.
REVISION = re.compile(r"^[0-9a-f]{40}$")
_FIELDS = frozenset({"name", "path", "signed_by", "signature"})


class SourceError(RuntimeError):
    """Base for every source failure."""


class SourceConfigError(SourceError, ValueError):
    """A source entry or the sources file is malformed."""


class SourceRefused(SourceError):
    """Refused on the operator's terms: unknown, unsigned or not verifying."""


def canonical(obj: object) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


@dataclass(frozen=True)
class SourceSpec:
    name: str
    path: str
    signed_by: str = ""
    signature: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _NAME.match(self.name):
            raise SourceConfigError("a source name is 1 to 64 of A-Za-z0-9_.-")
        path = self.path
        if (not isinstance(path, str) or not path.startswith("/") or "\0" in path
                or os.path.normpath(path) != path or ".." in Path(path).parts):
            raise SourceConfigError(
                f"{self.name}: path must be a plain absolute path, with no '..'")
        if not isinstance(self.signed_by, str) or (
                self.signature is not None and not isinstance(self.signature, str)):
            raise SourceConfigError(f"{self.name}: signed_by and signature are strings")

    def signed_fields(self) -> dict[str, object]:
        """Exactly what the signature covers. Everything but the signature."""
        return {"name": self.name, "path": self.path, "by": self.signed_by}

    def digest(self) -> str:
        return hashlib.sha256(canonical(self.signed_fields())).hexdigest()

    def as_entry(self) -> dict[str, object]:
        """The entry as it is written in the sources file."""
        return {"name": self.name, "path": self.path, "signed_by": self.signed_by,
                "signature": self.signature}


def sign_source(key: Ed25519PrivateKey, spec: SourceSpec, by: str) -> SourceSpec:
    if not by.strip():
        raise SourceConfigError("say who is signing")
    unsigned = dataclasses.replace(spec, signed_by=by.strip(), signature=None)
    signature = operator_keys.sign_action(key, PURPOSE, **unsigned.signed_fields())
    return dataclasses.replace(unsigned, signature=signature)


def verify_source(key: Ed25519PublicKey, spec: SourceSpec) -> bool:
    return bool(spec.signed_by.strip()) and operator_keys.verify_action(
        key, spec.signature, PURPOSE, **spec.signed_fields())


def load_sources(path: Path) -> dict[str, SourceSpec]:
    """Source entries from a JSON list, strictly: an unknown key is refused,
    so a typo cannot quietly change what was meant."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SourceConfigError(f"cannot read the sources file {path}: {exc}") from None
    if not isinstance(raw, list):
        raise SourceConfigError("the sources file must be a JSON list")
    out: dict[str, SourceSpec] = {}
    for entry in raw:
        if not isinstance(entry, dict) or set(entry) - _FIELDS or "name" not in entry \
                or "path" not in entry:
            raise SourceConfigError(f"bad source entry (allowed keys: {sorted(_FIELDS)})")
        spec = SourceSpec(name=entry["name"], path=entry["path"],
                          signed_by=entry.get("signed_by", ""),
                          signature=entry.get("signature"))
        if spec.name in out:
            raise SourceConfigError(f"duplicate source {spec.name!r}")
        out[spec.name] = spec
    return out


class SourceRegistry:
    """The operator's sources and the key that must have signed them.

    Built by trusted code and handed to the broker. Nothing here is reachable
    from a task except through ``workspace.acquire``.
    """

    def __init__(self, sources: Mapping[str, SourceSpec],
                 public_key: Ed25519PublicKey | None) -> None:
        self._sources = dict(sources)
        self._key = public_key

    @property
    def names(self) -> list[str]:
        return sorted(self._sources)

    def spec(self, name: str) -> SourceSpec:
        try:
            return self._sources[name]
        except KeyError:
            raise SourceRefused(f"no repository source named {name!r}") from None

    def state(self, name: str) -> str:
        spec = self._sources.get(name)
        if spec is None:
            return "unknown"
        if self._key is None:
            return "no operator key"
        if not spec.signature:
            return "unsigned"
        return "signed" if verify_source(self._key, spec) else "bad signature"

    def verified(self, name: str) -> SourceSpec:
        state = self.state(name)
        if state != "signed":
            raise SourceRefused(f"repository source {name!r} is {state}. "
                                "The operator must sign it")
        return self._sources[name]


# ---------------------------------------------------------------- provenance


def record(conn: sqlite3.Connection, task_id: str, spec: SourceSpec, revision: str,
           tree: str, directory: str, workspace: str) -> int:
    """One row per acquisition. Written once, never updated."""
    cur = conn.execute(
        "INSERT INTO workspace_acquisitions (task_id, source, source_path, source_sha256, "
        "signed_by, revision, tree, directory, workspace) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (task_id, spec.name, spec.path, spec.digest(), spec.signed_by, revision, tree,
         directory, workspace))
    return int(cur.lastrowid or 0)


def acquisitions(conn: sqlite3.Connection, task_id: str | None = None) -> list[sqlite3.Row]:
    if task_id is None:
        return conn.execute("SELECT * FROM workspace_acquisitions ORDER BY id").fetchall()
    return conn.execute("SELECT * FROM workspace_acquisitions WHERE task_id = ? ORDER BY id",
                        (task_id,)).fetchall()
