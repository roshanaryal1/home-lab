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

import re
from dataclasses import dataclass

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
