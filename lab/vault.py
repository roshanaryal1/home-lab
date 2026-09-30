"""Secret resolution and redaction (item 4.4, #15).

A credential is resolved at the moment of one outbound call, held in a
local variable, placed into that request's header by the broker, and
never stored: not in a workspace file, not in model context, not in a
task payload, an event, an artifact manifest or a log line.

* Sources: an environment variable ``LAB_SECRET_<NAME>`` (how the
  LaunchDaemon injects them), then the macOS Keychain generic password
  service ``home-lab`` account ``<NAME>``. Nothing else.
* There is no ``list``: a caller can ask for a secret whose name it was
  granted and nothing more, so a worker cannot enumerate what exists.
* Every value that was resolved for a call is scrubbed out of that call's
  result and error before anyone sees them, in the raw form and in
  URL-encoded and base64 forms, because a server that echoes a header
  back would otherwise hand the credential to the model.
"""

from __future__ import annotations

import base64
import os
import platform
import re
import subprocess
import urllib.parse
from collections.abc import Callable, Mapping
from typing import Any

NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
MIN_LENGTH = 8
REDACTED = "[REDACTED]"
KEYCHAIN_SERVICE = "home-lab"


class SecretUnavailable(Exception):
    """The secret is missing, malformed or too weak to redact safely.
    The message never contains the value."""


def _keychain(name: str) -> str | None:
    if platform.system() != "Darwin":
        return None
    try:
        out = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
             "-a", name, "-w"],
            capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.rstrip("\n") if out.returncode == 0 and out.stdout else None


class Vault:
    def __init__(self, env: Mapping[str, str] | None = None,
                 keychain: Callable[[str], str | None] = _keychain) -> None:
        self._env = os.environ if env is None else env
        self._keychain = keychain

    def resolve(self, name: str) -> str:
        if not NAME.match(name):
            raise SecretUnavailable("invalid secret name")
        value = self._env.get(f"LAB_SECRET_{name.upper().replace('-', '_').replace('.', '_')}")
        if not value:
            value = self._keychain(name)
        if not value:
            raise SecretUnavailable(f"secret {name!r} is not available")
        if len(value) < MIN_LENGTH or "\n" in value or "\r" in value or "\0" in value:
            raise SecretUnavailable(f"secret {name!r} is malformed or too short to redact")
        return value


def _forms(secret: str) -> list[str]:
    forms = {secret, urllib.parse.quote(secret, safe=""), urllib.parse.quote_plus(secret),
             base64.b64encode(secret.encode()).decode(),
             base64.urlsafe_b64encode(secret.encode()).decode()}
    forms |= {f.rstrip("=") for f in list(forms)}
    return sorted((f for f in forms if f), key=len, reverse=True)


# What a server can turn one character into when it reflects a header: JSON escapes
# (short forms and \\uXXXX in either case), HTML entities and percent-hex in either
# case. Servers escape per character, not per string (Go escapes & < >, PHP escapes /),
# so the match pattern allows each character to appear in any of its forms (#219).
_NAMED = {'"': ['\\"', "&quot;"], "'": ["&apos;"], "&": ["&amp;"], "<": ["&lt;"],
          ">": ["&gt;"], "/": ["\\/"], "\\": ["\\\\"], " ": ["+"]}


def _alternatives(ch: str) -> list[str]:
    if ch.isascii() and ch.isalnum():
        return [ch]                    # letters and digits are never escaped in practice
    units = ch.encode("utf-16-be")
    unit_hex = [units[i:i + 2].hex() for i in range(0, len(units), 2)]
    utf8 = ch.encode("utf-8")
    forms = [ch, "".join(f"\\u{u}" for u in unit_hex),
             "".join(f"\\u{u.upper()}" for u in unit_hex),
             "".join(f"%{b:02x}" for b in utf8), "".join(f"%{b:02X}" for b in utf8),
             f"&#{ord(ch)};", f"&#x{ord(ch):x};", f"&#x{ord(ch):X};", *_NAMED.get(ch, [])]
    return sorted(dict.fromkeys(forms), key=len, reverse=True)


def _echo_pattern(secret: str) -> re.Pattern[str] | None:
    """A pattern matching the secret with any of its characters escaped any way above."""
    if secret.isascii() and secret.isalnum():
        return None
    parts = []
    for ch in secret:
        alts = _alternatives(ch)
        parts.append(re.escape(alts[0]) if len(alts) == 1
                     else "(?:" + "|".join(re.escape(a) for a in alts) + ")")
    return re.compile("".join(parts))


class Redactor:
    """Scrubs a set of secret values from any nested structure."""

    def __init__(self, secrets: list[str] | None = None) -> None:
        self._needles: list[str] = []
        self._patterns: list[re.Pattern[str]] = []
        for secret in secrets or []:
            self.add(secret)

    def add(self, secret: str) -> None:
        self._needles.extend(f for f in _forms(secret) if f not in self._needles)
        self._needles.sort(key=len, reverse=True)
        pattern = _echo_pattern(secret)
        if pattern is not None:
            self._patterns.append(pattern)

    def scrub(self, value: Any) -> Any:
        if isinstance(value, str):
            for needle in self._needles:
                value = value.replace(needle, REDACTED)
            for pattern in self._patterns:
                value = pattern.sub(REDACTED, value)
            return value
        if isinstance(value, dict):
            return {self.scrub(k): self.scrub(v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [self.scrub(v) for v in value]
        return value
