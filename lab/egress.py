"""Network egress control: the fetch gateway (item 4.3, #14).

Nothing in the lab makes an outbound request except through ``fetch``
here. The rules, each of which has a test:

* **Default deny.** A task has an allowed-host list, set by trusted
  registration and never by the task. No list means no network.
* **Only https, only port 443, only DNS names.** IP literals in any
  spelling (dotted, decimal, hex, IPv6, IPv4-mapped) never match a
  DNS-name allowlist entry, so they are refused without a special case.
  URLs carrying credentials are refused.
* **Resolve, check, pin.** The name is resolved once per hop. Every
  address it returns must be globally routable: loopback, private,
  link-local (including the 169.254.169.254 metadata address), CGNAT,
  multicast and reserved ranges are refused, and if any address is bad
  the whole answer is refused. The connection then goes to the
  validated address itself, with the original name only for TLS and the
  Host header. There is no second lookup for a rebinding DNS server to
  answer differently.
* **Redirects are re-validated.** They are never followed by the
  transport. Each hop passes the full check above, so a permitted host
  cannot bounce the request to a metadata address or a host off the
  list. At most ``MAX_REDIRECTS`` hops.
* **Bounded.** Timeout, response size cap, no content encoding accepted.
* **Untrusted by construction.** What comes back is returned as
  fixed-schema ``Evidence`` (``lab.untrusted``), not raw text.
* **Audited.** Every attempt, allowed or denied, is recorded with the
  host, a hash of the URL and the reason. Query strings are never logged.
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import re
import socket
import ssl
import urllib.parse
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from lab.untrusted import Evidence, extract_evidence

MAX_REDIRECTS = 3
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
TIMEOUT_SECONDS = 15.0
MAX_URL_LENGTH = 2048

_LABEL = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
# A DNS name with a non-numeric last label, so no spelling of an IP matches.
_HOSTNAME = re.compile(rf"^(?=.{{1,253}}$)({_LABEL}\.)+[a-z][a-z0-9-]{{0,61}}[a-z0-9]$")
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})


class EgressDenied(Exception):
    """The request was refused before, or instead of, going out."""


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes


Resolver = Callable[[str, int], list[str]]
Transport = Callable[[str, int, str, str, float, int], Response]
"""(pinned ip, port, hostname for TLS/Host, path+query, timeout, max bytes) -> Response"""


def system_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def socket_transport(ip: str, port: int, host: str, target: str, timeout: float,
                     max_bytes: int, *, tls: bool = True) -> Response:
    """Connect to ``ip`` itself; the name is used only for TLS and Host."""
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    raw = socket.socket(family, socket.SOCK_STREAM)
    raw.settimeout(timeout)
    try:
        raw.connect((ip, port))
        stream: Any = raw
        if tls:
            stream = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.sock = stream
        conn.request("GET", target, headers={
            "Host": host, "Accept-Encoding": "identity", "Connection": "close",
            "User-Agent": "home-lab-fetch/1"})
        resp = conn.getresponse()
        body = resp.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise EgressDenied(f"response larger than {max_bytes} bytes")
        return Response(resp.status, {k.lower(): v for k, v in resp.getheaders()}, body)
    finally:
        raw.close()


def _normalise_host(host: str) -> str:
    try:
        return host.strip().rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise EgressDenied("host is not a valid name") from None


def parse_allowlist(hosts: Iterable[str]) -> frozenset[str]:
    """Validate allowlist entries: DNS names only, optionally ``*.suffix``."""
    out = set()
    for entry in hosts:
        base = entry[2:] if entry.startswith("*.") else entry
        norm = _normalise_host(base)
        if not _HOSTNAME.match(norm):
            raise ValueError(f"{entry!r} is not a DNS name (IP addresses cannot be allowlisted)")
        out.add(("*." if entry.startswith("*.") else "") + norm)
    return frozenset(out)


def _host_allowed(host: str, allowed: frozenset[str]) -> bool:
    if host in allowed:
        return True
    return any(a.startswith("*.") and host.endswith(a[1:]) and host != a[2:]
               for a in allowed)


def _check_address(text: str) -> None:
    ip = ipaddress.ip_address(text)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if not ip.is_global or ip.is_multicast:
        raise EgressDenied(f"{text} is not a public address")


@dataclass(frozen=True)
class Validated:
    host: str
    ip: str
    target: str          # path + query


def validate(url: str, allowed: frozenset[str], resolver: Resolver) -> Validated:
    if not allowed:
        raise EgressDenied("this task has no allowed hosts, so no network")
    if len(url) > MAX_URL_LENGTH or any(ord(c) < 33 or ord(c) == 127 for c in url):
        raise EgressDenied("malformed url")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https":
        raise EgressDenied("only https is allowed")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise EgressDenied("urls with credentials are refused")
    try:
        port = parts.port
    except ValueError:
        raise EgressDenied("malformed port") from None
    if port not in (None, 443):
        raise EgressDenied("only port 443 is allowed")
    if not parts.hostname:
        raise EgressDenied("no host")
    host = _normalise_host(parts.hostname)
    if not _host_allowed(host, allowed):
        raise EgressDenied(f"{host} is not on this task's allowed list")
    try:
        addresses = resolver(host, 443)
    except OSError as exc:
        raise EgressDenied(f"cannot resolve {host}: {exc}") from None
    if not addresses:
        raise EgressDenied(f"{host} did not resolve")
    for address in addresses:
        _check_address(address)              # any bad answer refuses the whole set
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    return Validated(host, addresses[0], target)


@dataclass(frozen=True)
class FetchResult:
    url: str
    status: int
    evidence: Evidence
    content_type: str
    hops: int


class EgressGateway:
    def __init__(self, resolver: Resolver = system_resolver,
                 transport: Transport = socket_transport,
                 audit: Callable[[str, dict[str, Any]], None] | None = None) -> None:
        self._resolver = resolver
        self._transport = transport
        self._audit = audit or (lambda kind, detail: None)

    def fetch(self, url: str, allowed: frozenset[str], task_id: str = "") -> FetchResult:
        current = url
        for hop in range(MAX_REDIRECTS + 1):
            try:
                v = validate(current, allowed, self._resolver)
            except EgressDenied as exc:
                self._record("egress_deny", current, task_id, str(exc), hop)
                raise
            self._record("egress_allow", current, task_id, "on the allowed list", hop)
            response = self._transport(v.ip, 443, v.host, v.target, TIMEOUT_SECONDS,
                                       MAX_RESPONSE_BYTES)
            if response.status in _REDIRECT_CODES:
                location = response.headers.get("location")
                if not location:
                    raise EgressDenied("redirect without a location")
                current = urllib.parse.urljoin(current, location)
                continue
            text = response.body.decode("utf-8", errors="replace")
            evidence = extract_evidence(text, source_type="web", source_id=url)
            return FetchResult(url, response.status, evidence,
                               response.headers.get("content-type", ""), hop)
        self._record("egress_deny", current, task_id, "too many redirects", MAX_REDIRECTS)
        raise EgressDenied(f"more than {MAX_REDIRECTS} redirects")

    def _record(self, kind: str, url: str, task_id: str, reason: str, hop: int) -> None:
        parts = urllib.parse.urlsplit(url)
        self._audit(kind, {
            "task_id": task_id, "host": (parts.hostname or "")[:255],
            "url_sha256": hashlib.sha256(url.encode("utf-8", "replace")).hexdigest(),
            "reason": reason, "hop": hop,
        })
