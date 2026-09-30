"""The Telegram alert command (#79): it must never leak the token or reach another host."""

from __future__ import annotations

import http.server
import io
import json
import threading
from pathlib import Path
from urllib.parse import parse_qs, unquote_plus

import pytest

from lab import telegram_alert as ta

TOKEN = "123456789:" + "A" * 35        # synthetic, shaped like a bot token


def _config(tmp_path: Path, mode: int = 0o600, **overrides: object) -> Path:
    data = {"bot_token": TOKEN, "chat_id": 424242, **overrides}
    path = tmp_path / "telegram-alert.json"
    path.write_text(json.dumps(data))
    path.chmod(mode)
    return path


class Fake:
    """A stand-in for the network: records the request, answers as configured."""

    def __init__(self, status: int = 200, reply: object = None, error: Exception | None = None):
        self.calls: list[tuple[str, bytes, float]] = []
        self.status, self.error = status, error
        self.reply = {"ok": True} if reply is None else reply

    def __call__(self, url: str, body: bytes, timeout: float) -> tuple[int, bytes]:
        self.calls.append((url, body, timeout))
        if self.error:
            raise self.error
        return self.status, json.dumps(self.reply).encode()


def test_a_private_config_loads_and_its_repr_hides_the_token(tmp_path: Path) -> None:
    config = ta.load_config(_config(tmp_path))
    assert config.chat_id == 424242 and config.bot_token == TOKEN
    assert TOKEN not in repr(config) and TOKEN not in str(config)


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o660])
def test_a_config_others_can_read_is_refused(tmp_path: Path, mode: int) -> None:
    with pytest.raises(ta.TelegramAlertError, match="readable by group or others"):
        ta.load_config(_config(tmp_path, mode=mode))


def test_a_symlinked_config_is_refused(tmp_path: Path) -> None:
    real = _config(tmp_path)
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(ta.TelegramAlertError, match="cannot read"):
        ta.load_config(link)


def test_a_missing_config_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ta.TelegramAlertError, match="cannot read"):
        ta.load_config(tmp_path / "nope.json")


@pytest.mark.parametrize("overrides", [
    {"bot_token": "not-a-token"}, {"bot_token": 5}, {"bot_token": "12:short"},
    {"chat_id": 0}, {"chat_id": True}, {"chat_id": "abc"}, {"chat_id": 1.5},
    {"chat_id": None}])
def test_malformed_values_are_refused_without_echoing_them(
        tmp_path: Path, overrides: dict) -> None:
    with pytest.raises(ta.TelegramAlertError) as caught:
        ta.load_config(_config(tmp_path, **overrides))
    assert TOKEN not in str(caught.value)


def test_a_numeric_chat_id_written_as_text_is_accepted(tmp_path: Path) -> None:
    assert ta.load_config(_config(tmp_path, chat_id="-1001234567890")).chat_id == -1001234567890


def test_a_config_that_is_not_an_object_or_not_json_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    for content in ("[1, 2]", "not json"):
        path.write_text(content)
        path.chmod(0o600)
        with pytest.raises(ta.TelegramAlertError):
            ta.load_config(path)


def test_the_request_goes_to_the_fixed_host_with_the_token_only_in_the_path(
        tmp_path: Path) -> None:
    fake = Fake()
    ta.send(ta.load_config(_config(tmp_path)), "status: unhealthy", post=fake)
    (url, body, timeout), = fake.calls
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    form = parse_qs(body.decode())
    assert set(form) == {"chat_id", "text", "disable_web_page_preview"}
    assert form["chat_id"] == ["424242"] and form["text"] == ["status: unhealthy"]
    assert TOKEN not in unquote_plus(body.decode()) and TOKEN.encode() not in body
    assert timeout == ta.TIMEOUT_SECONDS


def test_the_message_is_cleaned_collapsed_and_bounded(tmp_path: Path) -> None:
    fake = Fake()
    nasty = "boom\x1b[31m red ‮evil‬\n\n" + "x" * 5000
    ta.send(ta.load_config(_config(tmp_path)), nasty, post=fake)
    sent = parse_qs(fake.calls[0][1].decode())["text"][0]
    assert "\x1b" not in sent and "‮" not in sent and "\n" not in sent
    assert len(sent) <= ta.MAX_TEXT


def test_an_empty_message_is_refused_and_nothing_is_sent(tmp_path: Path) -> None:
    fake = Fake()
    with pytest.raises(ta.TelegramAlertError, match="empty"):
        ta.send(ta.load_config(_config(tmp_path)), " \x1b \n ", post=fake)
    assert fake.calls == []


def test_a_network_error_never_carries_the_url_or_token(tmp_path: Path) -> None:
    fake = Fake(error=OSError(f"failed for https://api.telegram.org/bot{TOKEN}/sendMessage"))
    with pytest.raises(ta.TelegramAlertError) as caught:
        ta.send(ta.load_config(_config(tmp_path)), "x", post=fake)
    assert TOKEN not in str(caught.value) and "OSError" in str(caught.value)


def test_a_telegram_refusal_is_reported_with_the_token_redacted(tmp_path: Path) -> None:
    fake = Fake(status=401, reply={"ok": False, "description": f"Unauthorized {TOKEN}"})
    with pytest.raises(ta.TelegramAlertError) as caught:
        ta.send(ta.load_config(_config(tmp_path)), "x", post=fake)
    assert "401" in str(caught.value) and TOKEN not in str(caught.value)
    assert "<token>" in str(caught.value)


def test_a_200_that_is_not_ok_is_still_a_failure(tmp_path: Path) -> None:
    for reply in ({"ok": False}, {"result": {}}, ["ok"]):
        with pytest.raises(ta.TelegramAlertError):
            ta.send(ta.load_config(_config(tmp_path)), "x", post=Fake(reply=reply))


def test_main_sends_stdin_and_returns_zero(tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    fake = Fake()
    monkeypatch.setattr("sys.stdin", io.StringIO("selftest: failed\n"))
    assert ta.main(["--config", str(_config(tmp_path))], post=fake) == 0
    assert len(fake.calls) == 1


def test_main_fails_closed_and_prints_no_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    fake = Fake(status=500, reply={"ok": False, "description": TOKEN})
    monkeypatch.setattr("sys.stdin", io.StringIO("x"))
    assert ta.main(["--config", str(_config(tmp_path))], post=fake) == 1
    monkeypatch.setattr("sys.stdin", io.StringIO("x"))
    assert ta.main(["--config", str(_config(tmp_path, mode=0o644))], post=fake) == 1
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err and "telegram_alert:" in captured.err


def test_a_redirect_is_not_followed() -> None:
    hits: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            hits.append(self.path)
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/elsewhere")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _ = ta._post(f"http://127.0.0.1:{server.server_port}/start", b"x=1", 5.0)
    finally:
        server.shutdown()
        thread.join(5)
    assert status == 302 and hits == ["/start"]
