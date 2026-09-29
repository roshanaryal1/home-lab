"""The read-only dashboard (H7): loopback only, no controls, everything escaped."""

from __future__ import annotations

import http.client
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from lab import dashboard
from lab.cli import main
from lab.queue import TaskQueue

HOSTILE = '<script>alert("x")</script>&"\''


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    path = tmp_path / "lab.db"
    with TaskQueue(path) as queue:
        queue._conn.execute("UPDATE control SET reason = ?, set_by = ? WHERE id = 1",
                            (HOSTILE, HOSTILE))
        queue._conn.commit()
    return path


@contextmanager
def serving(db: Path) -> Iterator[tuple[str, int]]:
    server = dashboard.make_server(db, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[0], server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def fetch(addr: tuple[str, int], path: str = "/", method: str = "GET",
          host: str | None = None) -> http.client.HTTPResponse:
    conn = http.client.HTTPConnection(addr[0], addr[1], timeout=5)
    conn.putrequest(method, path, skip_host=host is not None)
    if host is not None:
        conn.putheader("Host", host)
    conn.endheaders()
    return conn.getresponse()


@pytest.mark.safety
@pytest.mark.parametrize("host", ["0.0.0.0", "", "192.168.1.5", "example.com", "::", "10.0.0.1"])
def test_it_refuses_to_bind_anywhere_but_loopback(db: Path, host: str) -> None:
    with pytest.raises(dashboard.DashboardError):
        dashboard.make_server(db, host=host, port=0)


def test_it_binds_to_loopback_by_default(db: Path) -> None:
    server = dashboard.make_server(db, port=0)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


@pytest.mark.safety
def test_every_stored_string_is_escaped(db: Path) -> None:
    page = dashboard.render_page(db)
    assert "&lt;script&gt;" in page and "<script" not in page.lower()


def test_the_page_shows_health_mode_and_queue(db: Path) -> None:
    page = dashboard.render_page(db)
    for word in ("health", "mode", "queued", "needs a person"):
        assert word in page.lower()


@pytest.mark.safety
def test_the_response_forbids_scripts_framing_and_caching(db: Path) -> None:
    with serving(db) as addr:
        resp = fetch(addr)
        body = resp.read().decode()
    assert resp.status == 200 and resp.getheader("Content-Type", "").startswith("text/html")
    csp = resp.getheader("Content-Security-Policy", "")
    assert "default-src 'none'" in csp and "script" not in csp.replace("default-src", "")
    assert resp.getheader("X-Content-Type-Options") == "nosniff"
    assert "no-store" in (resp.getheader("Cache-Control") or "")
    assert resp.getheader("X-Frame-Options") == "DENY"
    assert "<form" not in body.lower() and "<script" not in body.lower()


@pytest.mark.safety
@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_only_get_is_served(db: Path, method: str) -> None:
    with serving(db) as addr:
        resp = fetch(addr, method=method)
        assert resp.status == 405
        assert "GET" in (resp.getheader("Allow") or "")


@pytest.mark.safety
def test_a_foreign_host_header_is_refused_against_dns_rebinding(db: Path) -> None:
    with serving(db) as addr:
        assert fetch(addr, host="evil.example.com").status == 403
        assert fetch(addr, host=f"127.0.0.1:{addr[1]}").status == 200
        assert fetch(addr, host=f"localhost:{addr[1]}").status == 200


def test_the_json_view_matches_status_and_unknown_paths_are_404(db: Path) -> None:
    import json
    with serving(db) as addr:
        resp = fetch(addr, "/status.json")
        data = json.loads(resp.read())
        assert resp.status == 200 and data["health"] in {"ok", "idle", "attention", "unhealthy"}
        assert fetch(addr, "/admin").status == 404
        assert fetch(addr, "/../../etc/passwd").status == 404


@pytest.mark.safety
def test_serving_never_writes_to_the_database(db: Path) -> None:
    before = db.read_bytes()
    with serving(db) as addr:
        fetch(addr).read()
    assert db.read_bytes() == before


def test_a_missing_database_is_reported_not_crashed(tmp_path: Path) -> None:
    with pytest.raises(dashboard.DashboardError):
        dashboard.make_server(tmp_path / "none.db", port=0)


def test_cli_refuses_a_non_loopback_host(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--db", str(db), "dashboard", "--host", "0.0.0.0"]) == 1
    assert "loopback" in capsys.readouterr().err
