"""The dead-man switch ping (#79).

A local HTTP server stands in for the outside service. It sits behind the
real ``EgressGateway``: the resolver answers a public address so the policy
checks run unchanged, and only the last step, the socket, is pointed at the
local server.
"""

from __future__ import annotations

import json
import logging
import plistlib
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from lab import deadman, service
from lab.cli import main
from lab.egress import EgressGateway, Response, socket_transport
from lab.queue import TaskQueue

SECRET = "check-0000-only-in-tests"
URL = f"https://hc-ping.example.com/{SECRET}"
PUBLIC = "93.184.216.34"


@dataclass
class Fake:
    port: int
    requests: list[tuple[str, str]] = field(default_factory=list)
    status: int = 200
    audit: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    resolve_to: str = PUBLIC

    def gateway(self) -> EgressGateway:
        def resolver(host: str, port: int) -> list[str]:
            return [self.resolve_to]

        def transport(ip: str, port: int, host: str, target: str, timeout: float,
                      max_bytes: int, **kw: Any) -> Response:
            assert ip == self.resolve_to and port == 443
            return socket_transport("127.0.0.1", self.port, host, target, timeout,
                                    max_bytes, tls=False, **kw)

        return EgressGateway(resolver, transport,
                             audit=lambda kind, d: self.audit.append((kind, d)))


@pytest.fixture()
def fake() -> Iterator[Fake]:
    state: dict[str, Fake] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            f = state["fake"]
            f.requests.append((self.headers["Host"], self.path))
            body = b"OK"
            self.send_response(f.status)
            if f.status in (301, 302):
                self.send_header("Location", "https://elsewhere.example.net/")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    state["fake"] = Fake(httpd.server_address[1])
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield state["fake"]
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture()
def healthy(tmp_path: Path) -> Path:
    db = tmp_path / "lab.db"
    TaskQueue(db, owner="t").close()
    return db


@pytest.fixture()
def unhealthy(tmp_path: Path) -> Path:
    db = tmp_path / "stuck.db"
    with TaskQueue(db, owner="t") as q:
        q.add_task("t")
        leased = q.lease(ttl_seconds=1)
        assert leased is not None and leased.lease is not None
        q.start(leased.lease)
        q._conn.execute("UPDATE leases SET expires_at = '2000-01-01 00:00:00.000'")
    return db


def _url_file(tmp_path: Path, url: str = URL, mode: int = 0o600) -> Path:
    path = tmp_path / "heartbeat-url"
    path.write_text(url + "\n")
    path.chmod(mode)
    return path


# ------------------------------------------------------------------ pinging


def test_a_healthy_lab_pings_once_through_the_gateway(tmp_path: Path, healthy: Path,
                                                      fake: Fake) -> None:
    outcome = deadman.run(healthy, _url_file(tmp_path), gateway=fake.gateway())
    assert outcome.code == 0, outcome.message
    assert fake.requests == [("hc-ping.example.com", f"/{SECRET}")]
    assert [kind for kind, _ in fake.audit] == ["egress_allow"]
    assert fake.audit[0][1]["host"] == "hc-ping.example.com"


@pytest.mark.safety
def test_an_unhealthy_lab_does_not_ping(tmp_path: Path, unhealthy: Path, fake: Fake) -> None:
    outcome = deadman.run(unhealthy, _url_file(tmp_path), gateway=fake.gateway())
    assert outcome.code == 2 and "unhealthy" in outcome.message
    assert fake.requests == []


def test_a_missing_or_damaged_database_does_not_ping(tmp_path: Path, fake: Fake) -> None:
    url_file = _url_file(tmp_path)
    assert deadman.run(tmp_path / "none.db", url_file, gateway=fake.gateway()).code == 2
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database" * 200)
    assert deadman.run(bad, url_file, gateway=fake.gateway()).code == 2
    assert fake.requests == []


@pytest.mark.safety
@pytest.mark.parametrize("url", [
    f"http://hc-ping.example.com/{SECRET}",
    f"ftp://hc-ping.example.com/{SECRET}",
    f"//hc-ping.example.com/{SECRET}",
    "https:///no-host",
])
def test_only_an_https_url_is_accepted(tmp_path: Path, healthy: Path, fake: Fake,
                                       url: str) -> None:
    outcome = deadman.run(healthy, _url_file(tmp_path, url), gateway=fake.gateway())
    assert outcome.code == 1
    assert fake.requests == []
    assert SECRET not in outcome.message


@pytest.mark.safety
@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o660, 0o606])
def test_a_file_others_can_read_or_write_is_refused(tmp_path: Path, healthy: Path,
                                                    fake: Fake, mode: int) -> None:
    outcome = deadman.run(healthy, _url_file(tmp_path, mode=mode), gateway=fake.gateway())
    assert outcome.code == 1 and "chmod 600" in outcome.message
    assert fake.requests == []


@pytest.mark.safety
def test_a_symlinked_url_file_is_refused(tmp_path: Path, healthy: Path, fake: Fake) -> None:
    real = _url_file(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(real)
    assert deadman.run(healthy, link, gateway=fake.gateway()).code == 1
    assert fake.requests == []


@pytest.mark.parametrize("content", ["", f"{URL}\n{URL}", "é"])
def test_a_file_that_is_not_one_url_is_refused(tmp_path: Path, content: str) -> None:
    path = tmp_path / "u"
    path.write_text(content)
    path.chmod(0o600)
    with pytest.raises(deadman.PingConfigError):
        deadman.load_url(path)
    with pytest.raises(deadman.PingConfigError):
        deadman.load_url(tmp_path / "missing")


def test_a_redirect_is_not_followed(tmp_path: Path, healthy: Path, fake: Fake) -> None:
    fake.status = 302
    outcome = deadman.run(healthy, _url_file(tmp_path), gateway=fake.gateway())
    assert outcome.code == 1 and "redirect" in outcome.message
    assert len(fake.requests) == 1


def test_a_private_address_is_refused_by_the_gateway(tmp_path: Path, healthy: Path,
                                                     fake: Fake) -> None:
    fake.resolve_to = "127.0.0.1"
    outcome = deadman.run(healthy, _url_file(tmp_path), gateway=fake.gateway())
    assert outcome.code == 1 and "not a public address" in outcome.message
    assert fake.requests == []


def test_an_error_status_is_a_failed_ping(tmp_path: Path, healthy: Path, fake: Fake) -> None:
    fake.status = 500
    outcome = deadman.run(healthy, _url_file(tmp_path), gateway=fake.gateway())
    assert outcome.code == 1 and "HTTP 500" in outcome.message


def test_a_network_failure_is_a_failed_ping_not_a_crash(tmp_path: Path, healthy: Path) -> None:
    def down(*a: Any, **k: Any) -> Response:
        raise ConnectionRefusedError(f"cannot reach {URL}")

    gw = EgressGateway(lambda h, p: [PUBLIC], down)
    outcome = deadman.run(healthy, _url_file(tmp_path), gateway=gw)
    assert outcome.code == 1 and "ConnectionRefusedError" in outcome.message
    assert SECRET not in outcome.message


# --------------------------------------------------------- the URL is secret


@pytest.mark.safety
def test_the_url_never_appears_in_output_logs_or_audit(
        tmp_path: Path, healthy: Path, unhealthy: Path, fake: Fake,
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
        caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(deadman, "EgressGateway", lambda **kw: fake.gateway())
    url_file = _url_file(tmp_path)
    codes = [main(["--db", str(healthy), "heartbeat", "--url-file", str(url_file)]),
             main(["--db", str(unhealthy), "heartbeat", "--url-file", str(url_file)])]
    fake.status = 500
    codes.append(main(["--db", str(healthy), "heartbeat", "--url-file", str(url_file)]))
    fake.status = 302
    codes.append(main(["--db", str(healthy), "heartbeat", "--url-file", str(url_file)]))
    url_file.chmod(0o644)
    codes.append(main(["--db", str(healthy), "heartbeat", "--url-file", str(url_file)]))
    assert codes == [0, 2, 1, 1, 1]
    captured = capsys.readouterr()
    assert "pinged hc-ping.example.com" in captured.out
    everything = captured.out + captured.err + caplog.text + json.dumps(fake.audit)
    assert SECRET not in everything and URL not in everything
    assert "url_sha256" in json.dumps(fake.audit), "the gateway still audits a hash"


def test_the_default_gateway_audit_stores_nothing(tmp_path: Path, healthy: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """Without an injected gateway the job builds the real one; its audit sink
    must not write to the database, or the log would never go silent."""
    built: dict[str, Any] = {}

    def capture(**kw: Any) -> EgressGateway:
        built.update(kw)
        return EgressGateway(lambda h, p: [PUBLIC],
                             lambda *a, **k: Response(200, {}, b"OK"), **kw)

    monkeypatch.setattr(deadman, "EgressGateway", capture)
    with TaskQueue(healthy, owner="t") as q:
        before = q._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert deadman.run(healthy, _url_file(tmp_path)).code == 0
    built["audit"]("egress_allow", {"host": "x"})
    with TaskQueue(healthy, owner="t") as q:
        assert q._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before


# ------------------------------------------------------------------ launchd


def test_heartbeat_plist_runs_every_five_minutes_as_lab() -> None:
    root = Path(__file__).resolve().parent.parent / "ops" / "launchd"
    generated = service.heartbeat_plist(
        user="lab", python="/opt/homelab/.venv/bin/python", workdir="/opt/homelab",
        db="/var/homelab/lab.db", url_file="/etc/homelab/heartbeat-url")
    assert (root / "com.homelab.heartbeat.plist").read_bytes() == generated
    data = plistlib.loads(generated)
    assert data["Label"] == service.DEADMAN_LABEL and data["UserName"] == "lab"
    assert data["StartInterval"] == 300 and "KeepAlive" not in data
    assert data["ProgramArguments"][-3:] == ["heartbeat", "--url-file",
                                             "/etc/homelab/heartbeat-url"]
    assert "hc-ping" not in generated.decode() and "https://" not in generated.decode()
