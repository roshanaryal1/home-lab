"""Telegram as the lab's alert channel (#79, H5b).

``lab.alert`` runs one operator-configured command and hands it the message on
stdin. This is that command for Telegram::

    {"command": ["/opt/homelab/.venv/bin/python", "-m", "lab.telegram_alert",
                 "--config", "/etc/homelab/telegram-alert.json"]}

The trust rules, each with a test:

* **A bot of its own, for alerts only.** The lab account can read this token, so
  it must not be the token of any bot that can run commands. This one can only
  send a message to one chat.
* The token and chat id come only from a JSON file that is a regular file, owned
  by the user running it, and closed to group and others (mode 600). It is opened
  without following a symlink and checked on the open descriptor.
* The host is fixed (``https://api.telegram.org``); nothing in the message or the
  config can change it, and a redirect is an error.
* The message is data: control and format characters are dropped, whitespace is
  collapsed and the text is bounded. It goes in the request body, never in the URL.
* The token never appears in an error, a log line or standard output.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lab.untrusted import clean

API = "https://api.telegram.org"
TOKEN = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{30,50}$")
MAX_TEXT = 1000
MAX_STDIN = 8192
TIMEOUT_SECONDS = 15.0

Post = Callable[[str, bytes, float], tuple[int, bytes]]


class TelegramAlertError(RuntimeError):
    """The alert could not be sent, or its configuration is unusable."""


@dataclass(frozen=True)
class Config:
    bot_token: str
    chat_id: int

    def __repr__(self) -> str:                 # never print the token by accident
        return f"Config(chat_id={self.chat_id}, bot_token=<hidden>)"


def load_config(path: str | Path) -> Config:
    path = Path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise TelegramAlertError(f"cannot read {path}: {exc.strerror}") from exc
    with os.fdopen(fd, encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())        # the descriptor, not the path
        if not stat.S_ISREG(info.st_mode):
            raise TelegramAlertError(f"{path} is not a regular file")
        if info.st_uid != os.geteuid():
            raise TelegramAlertError(f"{path} is not owned by the user running the alert")
        if info.st_mode & 0o077:
            raise TelegramAlertError(f"{path} is readable by group or others; chmod 600 it")
        try:
            data: Any = json.loads(stream.read())
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise TelegramAlertError(f"{path} is not valid JSON") from exc
    if not isinstance(data, dict):
        raise TelegramAlertError(f"{path} must hold a JSON object")
    token = data.get("bot_token")
    if not isinstance(token, str) or not TOKEN.match(token):
        raise TelegramAlertError('"bot_token" is missing or not shaped like a bot token')
    chat = data.get("chat_id")
    if isinstance(chat, str) and re.fullmatch(r"-?\d{1,20}", chat):
        chat = int(chat)
    if isinstance(chat, bool) or not isinstance(chat, int) or chat == 0:
        raise TelegramAlertError('"chat_id" must be a non-zero whole number')
    return Config(token, chat)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None                             # a 3xx becomes an HTTPError


def _post(url: str, body: bytes, timeout: float) -> tuple[int, bytes]:
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with opener.open(request, timeout=timeout) as response:  # fixed https host
            return response.status, response.read(65536)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(65536)


def _sanitize(text: str) -> str:
    return " ".join(clean(text).split())[:MAX_TEXT]


def _redact(text: str, config: Config) -> str:
    return text.replace(config.bot_token, "<token>")[:200]


def send(config: Config, message: str, *, post: Post = _post,
         timeout: float = TIMEOUT_SECONDS) -> None:
    text = _sanitize(message)
    if not text:
        raise TelegramAlertError("the message is empty")
    body = urllib.parse.urlencode({"chat_id": config.chat_id, "text": text,
                                   "disable_web_page_preview": "true"}).encode()
    try:
        status, payload = post(f"{API}/bot{config.bot_token}/sendMessage", body, timeout)
    except (OSError, ValueError) as exc:        # the text of these can carry the URL
        raise TelegramAlertError(f"request failed: {type(exc).__name__}") from None
    try:
        reply = json.loads(payload)
    except ValueError:
        reply = {}
    if status != 200 or not (isinstance(reply, dict) and reply.get("ok") is True):
        description = reply.get("description", "") if isinstance(reply, dict) else ""
        raise TelegramAlertError(_redact(f"Telegram answered {status}: {description}", config))


def main(argv: Sequence[str] | None = None, *, post: Post = _post) -> int:
    parser = argparse.ArgumentParser(prog="lab.telegram_alert",
                                     description="send stdin to Telegram as an alert")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        send(config, sys.stdin.read(MAX_STDIN), post=post)
    except TelegramAlertError as exc:
        print(f"telegram_alert: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
