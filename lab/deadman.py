"""The dead-man switch ping (item 6.3, #79).

Every alert the lab sends needs the lab to be running. A Mac mini that lost
power, network or its supervisor sends nothing, and silence looks like
health. So an outside service (healthchecks.io or any service like it; the
operator picks one) expects a ping every few minutes and alerts the phone
when the pings stop. ``lab heartbeat`` sends that ping.

The rules, each with a test:

* **It pings only a healthy lab.** The ping goes out only when ``lab status``
  would not report unhealthy, so a lab that is up but stuck also goes quiet
  and the outside service raises the alarm.
* **The URL is a secret.** Whoever holds it can keep the switch quiet. It is
  read from a file owned by the user running the job that no one else can
  read or write, opened once without following a symlink, with owner and
  mode read from the open descriptor. It is never printed: errors name the
  file, not its contents, and the gateway's audit records only the host and
  a hash of the URL.
* **One way out.** The request goes through ``lab.egress.EgressGateway`` with
  an allowlist of just the URL's own host: https on port 443, a public
  address, and no redirect is followed.
* **It writes nothing to the database.** A ping every five minutes in the
  event log would keep the log from ever going silent, and a silent log is
  one of the signals ``lab status`` uses to call the lab unhealthy.
"""

from __future__ import annotations

import http.client
import os
import sqlite3
import stat
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lab import metrics
from lab.db import connect_readonly
from lab.egress import EgressDenied, EgressGateway, parse_allowlist

MAX_URL_FILE_BYTES = 4096


class PingConfigError(ValueError):
    """The URL file is missing, unsafe or malformed. The message never holds the URL."""


def load_url(path: str | Path) -> str:
    path = Path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise PingConfigError(f"cannot read the ping URL file {path}: {exc.strerror}") from exc
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())        # the descriptor, not the path
        if not stat.S_ISREG(info.st_mode):
            raise PingConfigError(f"{path} is not a regular file")
        if info.st_uid != os.geteuid():
            raise PingConfigError(f"{path} is not owned by the user running the lab")
        if info.st_mode & 0o077:
            raise PingConfigError(f"{path} is readable or writable by group or others; "
                                  "chmod 600 it")
        raw = stream.read(MAX_URL_FILE_BYTES + 1)
    if len(raw) > MAX_URL_FILE_BYTES:
        raise PingConfigError(f"{path} is too large to be one URL")
    try:
        lines = [line.strip() for line in raw.decode("ascii").splitlines() if line.strip()]
    except UnicodeDecodeError:
        raise PingConfigError(f"{path} must hold one ASCII URL") from None
    if len(lines) != 1:
        raise PingConfigError(f"{path} must hold exactly one URL on one line")
    url = lines[0]
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https":
        raise PingConfigError(f"the URL in {path} must start with https://")
    if not parts.hostname:
        raise PingConfigError(f"the URL in {path} has no host")
    return url


def lab_health(db: Path) -> tuple[bool, str]:
    """(healthy, health word) the way ``lab status`` judges it, read-only."""
    if not Path(db).exists():
        return False, "no database"
    try:
        conn = connect_readonly(db)
        try:
            report = metrics.collect(conn)
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return False, "database unreadable"
    return report.health != "unhealthy", report.health


@dataclass(frozen=True)
class Outcome:
    code: int          # 0 pinged, 1 could not ping, 2 unhealthy so not pinged
    message: str       # safe to print: never holds the URL


def _scrub(text: str, url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    secret_parts = [url, parts.path, parts.query, parts.fragment]
    secret_parts += [p for p in parts.path.split("/") if len(p) >= 4]
    for part in sorted({p for p in secret_parts if p and p != "/"}, key=len, reverse=True):
        text = text.replace(part, "[redacted]")
    return text


def run(db: Path, url_file: Path, *, gateway: EgressGateway | None = None) -> Outcome:
    try:
        url = load_url(url_file)
    except PingConfigError as exc:
        return Outcome(1, str(exc))
    host = urllib.parse.urlsplit(url).hostname or ""
    healthy, health = lab_health(db)
    if not healthy:
        return Outcome(2, f"not pinged: the lab is {health}")
    try:
        allowed = parse_allowlist([host])
    except ValueError:
        return Outcome(1, "the URL's host is not a DNS name")

    def no_audit(kind: str, detail: dict[str, Any]) -> None:
        """The gateway reports only host and URL hash; nothing is stored, see above."""

    gw = gateway if gateway is not None else EgressGateway(audit=no_audit)
    try:
        result = gw.fetch(url, allowed, "heartbeat", follow_redirects=False)
    except EgressDenied as exc:
        return Outcome(1, _scrub(f"ping to {host} refused: {exc}", url))
    except (OSError, ValueError, http.client.HTTPException) as exc:   # network, TLS, junk
        return Outcome(1, _scrub(f"ping to {host} failed: {type(exc).__name__}: {exc}", url))
    if not 200 <= result.status < 300:
        return Outcome(1, f"ping to {host} answered HTTP {result.status}")
    return Outcome(0, f"pinged {host}, HTTP {result.status} (lab {health})")
