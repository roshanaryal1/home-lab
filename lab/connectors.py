"""Per-destination connectors (item 4.4, #15).

A connector is one destination with one credential: a single host, the
name of one secret, the header the secret goes into, and the methods and
path prefix a caller may use. It is defined by trusted code and granted
to an agent kind at registration, so a task cannot invent a destination,
widen a path or pick a different secret. The tool that uses it is
``connector.call`` (approve tier: a person sees the exact destination,
path and body before anything is sent).

The broker resolves the secret at the moment of the call, puts it in the
header, sends through the egress gateway, and scrubs the value out of the
result and any error before returning. A credentialed request never
follows a redirect.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from lab.egress import parse_allowlist
from lab.vault import NAME

MAX_BODY_BYTES = 64 * 1024
_PATH = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*$")
_HEADER = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,63}$")
_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})


class ConnectorError(ValueError):
    """A connector definition or a call to one is not acceptable."""


@dataclass(frozen=True)
class Connector:
    name: str
    host: str
    secret: str                      # the vault name, never the value
    header: str = "Authorization"
    scheme: str | None = "Bearer"    # None puts the bare secret in the header
    methods: frozenset[str] = frozenset({"POST"})
    path_prefix: str = "/"
    # Publishing (item 8.6). The header carries a key derived from the exact
    # call, so a provider that honours it cannot post the same thing twice
    # even if the lab resends. ``receipt_field`` names the JSON field holding
    # the provider's id for what was created. ``lookup_path`` is a GET that
    # finds a creation by its key, used to reconcile a lost response.
    idempotency_header: str | None = None
    receipt_field: str | None = "id"
    lookup_path: str | None = None

    def __post_init__(self) -> None:
        if not NAME.match(self.name) or not NAME.match(self.secret):
            raise ConnectorError("connector and secret names are 1 to 64 of A-Za-z0-9_.-")
        if self.host.startswith("*"):
            raise ConnectorError("a connector names exactly one host, no wildcard")
        try:
            parse_allowlist([self.host])
        except ValueError as exc:
            raise ConnectorError(str(exc)) from None
        if not _HEADER.match(self.header):
            raise ConnectorError("bad header name")
        if not self.methods or not self.methods <= _METHODS:
            raise ConnectorError(f"methods must be a non-empty subset of {sorted(_METHODS)}")
        if not _PATH.match(self.path_prefix):
            raise ConnectorError("bad path prefix")
        if self.idempotency_header is not None and not _HEADER.match(self.idempotency_header):
            raise ConnectorError("bad idempotency header name")
        if self.lookup_path is not None and (
                "{key}" not in self.lookup_path or not self.lookup_path.startswith(self.path_prefix)
                or not _PATH.match(self.lookup_path.replace("{key}", "k"))):
            raise ConnectorError("lookup_path must sit under the prefix and contain {key}")

    def header_value(self, secret: str) -> str:
        return f"{self.scheme} {secret}" if self.scheme else secret

    def check_call(self, method: str, path: str, body: str | None) -> None:
        if method not in self.methods:
            raise ConnectorError(f"{self.name} does not allow {method}")
        if not _PATH.match(path) or ".." in path.split("?")[0].split("/") \
                or "//" in path.split("?")[0]:
            raise ConnectorError("bad path")
        if not path.split("?")[0].startswith(self.path_prefix):
            raise ConnectorError(f"path is outside {self.path_prefix}")
        if body is not None and len(body.encode("utf-8")) > MAX_BODY_BYTES:
            raise ConnectorError("body too large")


_FIELDS = {"name", "host", "secret", "header", "scheme", "methods", "path_prefix",
           "idempotency_header", "receipt_field", "lookup_path"}


def load_connectors(path: Path) -> dict[str, Connector]:
    """Connectors from a JSON list, strictly: unknown keys are refused, so a
    typo cannot silently loosen a definition. The file holds names of secrets,
    never their values."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConnectorError(f"cannot read connectors file {path}: {exc}") from None
    if not isinstance(raw, list):
        raise ConnectorError("the connectors file must be a JSON list")
    out: dict[str, Connector] = {}
    for entry in raw:
        if not isinstance(entry, dict) or set(entry) - _FIELDS:
            raise ConnectorError(f"bad connector entry (allowed keys: {sorted(_FIELDS)})")
        entry = dict(entry)
        if "methods" in entry:
            entry["methods"] = frozenset(entry["methods"])
        try:
            connector = Connector(**entry)
        except TypeError as exc:
            raise ConnectorError(f"bad connector entry: {exc}") from None
        if connector.name in out:
            raise ConnectorError(f"duplicate connector {connector.name!r}")
        out[connector.name] = connector
    return out
