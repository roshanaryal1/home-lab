"""Operator-signed approvals (item 4.5, #70).

The agent and the approval command used to share one database and one OS
user, so the agent could write ``state = 'granted'`` itself. A database
column cannot tell who wrote it. A signature can, if the key that makes it
is somewhere the agent cannot read.

* The **operator** holds an Ed25519 private key (``operator.key``, mode
  0600, in the operator's home). ``lab approve`` signs the decision with it.
* The **supervisor** is given only the public key (``operator.pub``) and,
  once configured with it, consumes an approval only if its signature
  verifies over the exact approval id, action hash, expiry and decider.
  An approval an agent wrote, or one edited after signing (a longer window,
  a different action), is ignored.

Asymmetric on purpose: with a shared secret the verifier could also forge.
This is only a boundary if the private key is unreadable to the agent's OS
account, which is the parked separate-account setup on the Mac mini. Until
then the code is correct and tested, and the key file's location is the
part that needs the machine. ``--by`` remains a label for the audit log;
identity is the key.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

VERSION = 1
PRIVATE_NAME = "operator.key"
PUBLIC_NAME = "operator.pub"


class OperatorKeyError(RuntimeError):
    """A key file is missing, unreadable, malformed or too permissive."""


def generate(directory: Path) -> tuple[Path, Path]:
    """Create a keypair. Refuses to overwrite an existing private key."""
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_path, public_path = directory / PRIVATE_NAME, directory / PUBLIC_NAME
    if private_path.exists():
        raise OperatorKeyError(f"{private_path} already exists; refusing to overwrite it")
    key = Ed25519PrivateKey.generate()
    fd = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))
    public_path.write_bytes(key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    return private_path, public_path


def load_private(path: Path) -> Ed25519PrivateKey:
    path = Path(path)
    try:
        mode = path.stat().st_mode & 0o077
        data = path.read_bytes()
    except OSError as exc:
        raise OperatorKeyError(f"cannot read operator key {path}: {exc.strerror}") from exc
    if mode:
        raise OperatorKeyError(
            f"{path} is readable by group or others; chmod 600 it before use")
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except (ValueError, TypeError) as exc:
        raise OperatorKeyError(f"{path} is not a private key: {exc}") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise OperatorKeyError(f"{path} is not an Ed25519 key")
    return key


def load_public(path: Path) -> Ed25519PublicKey:
    try:
        data = Path(path).read_bytes()
        key = serialization.load_pem_public_key(data)
    except (OSError, ValueError) as exc:
        raise OperatorKeyError(f"cannot load operator public key {path}: {exc}") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise OperatorKeyError(f"{path} is not an Ed25519 key")
    return key


def message(approval_id: str, action_hash: str, expires_at: str, decided_by: str) -> bytes:
    body: dict[str, Any] = {
        "v": VERSION, "approval": approval_id, "action_hash": action_hash,
        "decision": "granted", "expires_at": expires_at, "decided_by": decided_by,
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign(key: Ed25519PrivateKey, approval_id: str, action_hash: str, expires_at: str,
         decided_by: str) -> str:
    return key.sign(message(approval_id, action_hash, expires_at, decided_by)).hex()


def verify(key: Ed25519PublicKey, signature: str | None, approval_id: str,
           action_hash: str, expires_at: str, decided_by: str) -> bool:
    if not signature:
        return False
    try:
        key.verify(bytes.fromhex(signature),
                   message(approval_id, action_hash, expires_at, decided_by))
    except (InvalidSignature, ValueError):
        return False
    return True


def sign_action(key: Ed25519PrivateKey, purpose: str, **fields: object) -> str:
    """Sign any operator action: the purpose and its exact fields."""
    return key.sign(_action_message(purpose, fields)).hex()


def verify_action(key: Ed25519PublicKey, signature: str | None, purpose: str,
                  **fields: object) -> bool:
    if not signature:
        return False
    try:
        key.verify(bytes.fromhex(signature), _action_message(purpose, fields))
    except (InvalidSignature, ValueError):
        return False
    return True


def _action_message(purpose: str, fields: dict[str, object]) -> bytes:
    return json.dumps({"v": VERSION, "purpose": purpose, "fields": fields},
                      sort_keys=True, separators=(",", ":")).encode("utf-8")
