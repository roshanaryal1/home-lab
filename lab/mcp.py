"""MCP servers, spoken to through the broker (M6c, #256).

An MCP server is a program the lab did not write. It describes its own tools,
and it can change those descriptions whenever it is updated. So a server is
reached only on terms the operator signed, and everything it says is data.

The signed config
-----------------

The operator lists each server once: a name, the exact argument list that
starts it (no shell, an absolute program path), the tools a task may call, and
for each allowed tool the SHA-256 of its name, description and input schema as
the server reported them when the operator looked (``lab mcp snapshot``). The
whole entry is signed with the operator key (``lab.operator.sign_action``).
An entry that is unsigned, or whose signature does not verify, cannot be
called. Nor can any entry when no operator public key is configured.

On every call the server is started fresh, asked for its tool list, and the
tool about to be called is compared with the signed snapshot. A tool that is
new (not on the allowlist) or changed (a different description or schema,
the rug pull) is refused until the operator signs again.

How the process runs
--------------------

* Started without a shell, from the signed argument list, with the minimal
  environment ``lab.sandbox`` gives commands, in its own process group.
* Under the Seatbelt profile when it is available. When it is not, the call is
  refused, never run unconfined. Tests inject a launcher instead.
* No network, unless the signed entry names egress hosts and the task's own
  egress list holds every one of them. Seatbelt cannot filter by host name,
  so a server with network has all of it. That is why it needs both the
  operator's signature and the task's grant.
* Bounded: one deadline covers start-up, the tool list and the call, a
  message over ``MAX_MESSAGE_BYTES`` is refused, and the broker caps calls
  per task. At the deadline, on cancel and at the end of every call, the
  whole process group is killed.
* Uncertain after sending: a failure before ``tools/call`` is written is a
  refusal. A failure after it (a timeout, a cancel, a dead server, a broken
  reply) is ``McpOutcomeUnknown``, because the tool may have run.

The client is newline-delimited JSON-RPC 2.0 over stdin and stdout, written
here by hand: ``initialize``, ``tools/list`` and ``tools/call`` are all the
lab needs, and a dependency for three methods is not worth its risk.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import re
import selectors
import signal
import subprocess
import threading
import time
import types
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from lab import operator as operator_keys
from lab import sandbox
from lab.egress import parse_allowlist
from lab.untrusted import clean

PURPOSE = "mcp-server"
PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "home-lab", "version": "1"}

# Hard ceilings. A signed entry may lower the timeout, never raise it past the cap.
MAX_MESSAGE_BYTES = 1024 * 1024        # one JSON-RPC line, either direction
MAX_ARGUMENT_BYTES = 64 * 1024         # the arguments of one call
MAX_CALLS_PER_TASK = 20
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_TIMEOUT_SECONDS = 120.0
MAX_TOOLS = 256
MAX_LIST_PAGES = 8
MAX_STRAY_MESSAGES = 100               # notifications and server requests per reply
MAX_DESCRIPTION_CHARS = 2000           # shown by the snapshot
_POLL_SECONDS = 0.1

_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_TOOL = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_FIELDS = frozenset({"name", "argv", "tools", "egress_hosts", "read_paths",
                     "timeout_seconds", "signed_by", "signature"})


class McpError(RuntimeError):
    """Base for every MCP failure."""


class McpConfigError(McpError, ValueError):
    """A server entry or the config file is malformed."""


class McpRefused(McpError):
    """Refused on the operator's terms: unsigned, not allowed, changed, too large."""


class McpTimeout(McpError):
    """The call's deadline passed. The process group was killed."""


class McpCancelled(McpError):
    """A stop or a revoke ended the call. The process group was killed."""


class McpProtocolError(McpError):
    """The server did not speak the protocol, or closed its pipes."""


class McpRpcError(McpProtocolError):
    """The server answered a request with a JSON-RPC error. An answer, so known."""


class McpOutcomeUnknown(McpError):
    """The call failed after ``tools/call`` was sent, so the tool may have run.

    A timeout, a cancel, a dead server or a broken reply after the request went
    out all land here. The broker holds the task for reconciliation instead of
    reporting an ordinary failure that a retry could repeat.
    """


def canonical(obj: object) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def tool_fingerprint(tool: Mapping[str, Any]) -> str:
    """SHA-256 of what a model would be shown: name, description, input schema."""
    return hashlib.sha256(canonical({
        "name": tool.get("name"),
        "description": tool.get("description", ""),
        "inputSchema": tool.get("inputSchema", {}),
    })).hexdigest()


def argument_bytes(arguments: object) -> int | None:
    """Size of the arguments as sent, or None if they are not plain JSON."""
    try:
        return len(canonical(arguments))
    except (TypeError, ValueError, RecursionError):
        return None


# ------------------------------------------------------------- the signed entry


@dataclass(frozen=True)
class ServerSpec:
    name: str
    argv: tuple[str, ...]
    tools: Mapping[str, str] = field(default_factory=dict)    # allowed tool -> fingerprint
    egress_hosts: frozenset[str] = frozenset()
    read_paths: tuple[str, ...] = ()
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    signed_by: str = ""
    signature: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _NAME.match(self.name):
            raise McpConfigError("a server name is 1 to 64 of A-Za-z0-9_.-")
        argv = tuple(self.argv)
        if not argv or not all(isinstance(a, str) and a and "\0" not in a for a in argv):
            raise McpConfigError(f"{self.name}: argv must be a non-empty list of strings")
        if not argv[0].startswith("/"):
            raise McpConfigError(f"{self.name}: argv[0] must be an absolute path")
        tools = dict(self.tools)
        for tool, sha in tools.items():
            if not isinstance(tool, str) or not _TOOL.match(tool):
                raise McpConfigError(f"{self.name}: bad tool name {tool!r}")
            if not isinstance(sha, str) or not _SHA.match(sha):
                raise McpConfigError(f"{self.name}: {tool} needs a SHA-256 fingerprint")
        try:
            hosts = parse_allowlist(self.egress_hosts)
        except ValueError as exc:
            raise McpConfigError(f"{self.name}: {exc}") from None
        paths = tuple(self.read_paths)
        if not all(isinstance(p, str) and p.startswith("/") and "\0" not in p for p in paths):
            raise McpConfigError(f"{self.name}: read_paths must be absolute paths")
        timeout = self.timeout_seconds
        if isinstance(timeout, bool) or not isinstance(timeout, int | float) \
                or not 0 < timeout <= MAX_TIMEOUT_SECONDS:
            raise McpConfigError(
                f"{self.name}: timeout_seconds must be above 0 and at most {MAX_TIMEOUT_SECONDS}")
        if not isinstance(self.signed_by, str) or (
                self.signature is not None and not isinstance(self.signature, str)):
            raise McpConfigError(f"{self.name}: signed_by and signature are strings")
        object.__setattr__(self, "argv", argv)
        object.__setattr__(self, "tools", types.MappingProxyType(dict(sorted(tools.items()))))
        object.__setattr__(self, "egress_hosts", hosts)
        object.__setattr__(self, "read_paths", paths)
        object.__setattr__(self, "timeout_seconds", float(timeout))

    def signed_fields(self) -> dict[str, object]:
        """Exactly what the signature covers. Everything but the signature."""
        return {"name": self.name, "argv": list(self.argv), "tools": dict(self.tools),
                "egress_hosts": sorted(self.egress_hosts), "read_paths": list(self.read_paths),
                "timeout_seconds": self.timeout_seconds, "by": self.signed_by}

    def digest(self) -> str:
        return hashlib.sha256(canonical(self.signed_fields())).hexdigest()

    def as_entry(self) -> dict[str, object]:
        """The entry as it is written in the config file."""
        entry = self.signed_fields()
        entry["signed_by"] = entry.pop("by")
        entry["signature"] = self.signature
        return entry


def sign_server(key: Ed25519PrivateKey, spec: ServerSpec, by: str) -> ServerSpec:
    if not by.strip():
        raise McpConfigError("say who is signing")
    unsigned = dataclasses.replace(spec, signed_by=by.strip(), signature=None)
    signature = operator_keys.sign_action(key, PURPOSE, **unsigned.signed_fields())
    return dataclasses.replace(unsigned, signature=signature)


def verify_server(key: Ed25519PublicKey, spec: ServerSpec) -> bool:
    return bool(spec.signed_by.strip()) and operator_keys.verify_action(
        key, spec.signature, PURPOSE, **spec.signed_fields())


def load_servers(path: Path) -> dict[str, ServerSpec]:
    """Server entries from a JSON list, strictly: an unknown key is refused,
    so a typo cannot quietly change what was meant."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise McpConfigError(f"cannot read MCP servers file {path}: {exc}") from None
    if not isinstance(raw, list):
        raise McpConfigError("the MCP servers file must be a JSON list")
    out: dict[str, ServerSpec] = {}
    for entry in raw:
        if not isinstance(entry, dict) or set(entry) - _FIELDS or "name" not in entry \
                or "argv" not in entry:
            raise McpConfigError(f"bad MCP server entry (allowed keys: {sorted(_FIELDS)})")
        if not isinstance(entry["argv"], list) or not isinstance(entry.get("tools", {}), dict) \
                or not isinstance(entry.get("egress_hosts", []), list) \
                or not isinstance(entry.get("read_paths", []), list):
            raise McpConfigError(f"bad MCP server entry {entry.get('name')!r}: wrong types")
        spec = ServerSpec(
            name=entry["name"], argv=tuple(entry["argv"]), tools=entry.get("tools", {}),
            egress_hosts=frozenset(entry.get("egress_hosts", [])),
            read_paths=tuple(entry.get("read_paths", [])),
            timeout_seconds=entry.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
            signed_by=entry.get("signed_by", ""), signature=entry.get("signature"))
        if spec.name in out:
            raise McpConfigError(f"duplicate MCP server {spec.name!r}")
        out[spec.name] = spec
    return out


# ------------------------------------------------------------------ processes

# (argv, workspace, allow_network, extra readable paths) -> a started process
# with pipes on stdin and stdout, in its own process group.
Launcher = Callable[[Sequence[str], Path, bool, tuple[Path, ...]], "subprocess.Popen[bytes]"]


def start_process(cmd: Sequence[str], workspace: Path) -> subprocess.Popen[bytes]:
    """No shell, the sandbox's minimal environment, its own process group."""
    return subprocess.Popen(
        list(cmd), cwd=str(workspace), env=sandbox.command_environment(workspace),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        start_new_session=True, close_fds=True)


def seatbelt_launcher(argv: Sequence[str], workspace: Path, allow_network: bool,
                      readable: tuple[Path, ...]) -> subprocess.Popen[bytes]:
    """The production launcher: the server under the Seatbelt profile, or nothing."""
    if not sandbox.available():
        raise sandbox.SandboxUnavailable(
            f"no OS-level sandbox here, so {argv[0]!r} is not started unconfined")
    root = Path(workspace).resolve()
    profile = sandbox.build_profile(root, allow_network=allow_network, extra_readable=readable)
    return start_process([sandbox.SANDBOX_EXEC, "-p", profile, *argv], root)



def _reject_constant(name: str) -> object:
    """NaN and Infinity are not JSON. Refuse them where the message is parsed,
    so they never reach a fingerprint or a result."""
    raise ValueError(f"non-standard JSON constant {name}")

class StdioClient:
    """Newline-delimited JSON-RPC 2.0 over a child's stdin and stdout.

    Every read and write waits at most ``_POLL_SECONDS`` at a time, so the
    deadline and the cancel flag are checked about ten times a second. A
    child that writes without a newline cannot grow memory past the cap.
    """

    def __init__(self, proc: subprocess.Popen[bytes], *, deadline: float,
                 cancel: threading.Event | None = None,
                 max_bytes: int = MAX_MESSAGE_BYTES) -> None:
        assert proc.stdin is not None and proc.stdout is not None
        self._proc = proc
        self._deadline = deadline
        self._cancel = cancel
        self._max = max_bytes
        self._in = proc.stdin.fileno()
        self._out = proc.stdout.fileno()
        os.set_blocking(self._in, False)
        self._buffer = bytearray()
        self._next_id = 0
        self.bytes_sent = 0      # how much reached the server's input so far
        self._reader = selectors.DefaultSelector()
        self._reader.register(self._out, selectors.EVENT_READ)

    def _wait(self) -> float:
        if self._cancel is not None and self._cancel.is_set():
            raise McpCancelled("the call was cancelled")
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise McpTimeout("the MCP server did not answer in time")
        return min(remaining, _POLL_SECONDS)

    def send(self, message: Mapping[str, Any]) -> None:
        data = canonical(message) + b"\n"
        if len(data) > self._max:
            raise McpRefused(f"an outgoing message is over {self._max} bytes")
        view = memoryview(data)
        with selectors.DefaultSelector() as writer:
            writer.register(self._in, selectors.EVENT_WRITE)
            while view:
                if not writer.select(self._wait()):
                    continue
                try:
                    written = os.write(self._in, view)
                except BlockingIOError:
                    continue
                except (BrokenPipeError, ConnectionResetError):
                    raise McpProtocolError("the server closed its input") from None
                self.bytes_sent += written
                view = view[written:]

    def receive(self) -> dict[str, Any]:
        while True:
            end = self._buffer.find(b"\n")
            if end > self._max - 1 or (end < 0 and len(self._buffer) >= self._max):
                raise McpRefused(f"a message from the server is over {self._max} bytes")
            if end >= 0:
                line = bytes(self._buffer[:end])
                del self._buffer[:end + 1]
                if not line.strip():
                    continue
                try:
                    message = json.loads(line, parse_constant=_reject_constant)
                except (ValueError, RecursionError):
                    raise McpProtocolError("the server sent a line that is not JSON") from None
                if not isinstance(message, dict):
                    raise McpProtocolError("the server sent JSON that is not an object")
                return message
            if not self._reader.select(self._wait()):
                continue
            chunk = os.read(self._out, 65536)
            if not chunk:
                raise McpProtocolError("the server closed its output")
            self._buffer.extend(chunk)

    def request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        wanted = self._next_id
        self.send({"jsonrpc": "2.0", "id": wanted, "method": method, "params": dict(params)})
        for _ in range(MAX_STRAY_MESSAGES + 1):
            message = self.receive()
            if "method" not in message and message.get("id") == wanted:
                if "error" in message:
                    # The server's own words are not passed on: only the code.
                    error = message["error"]
                    code = error.get("code") if isinstance(error, dict) else None
                    code = code if isinstance(code, int) and not isinstance(code, bool) else None
                    raise McpRpcError(f"{method} failed with JSON-RPC error {code}")
                result = message.get("result")
                if not isinstance(result, dict):
                    raise McpProtocolError(f"{method} returned no result object")
                return result
            if "method" in message and "id" in message:
                # A request from the server (sampling, roots): the lab offers none.
                self.send({"jsonrpc": "2.0", "id": message["id"],
                           "error": {"code": -32601, "message": "not supported"}})
        raise McpProtocolError(f"more than {MAX_STRAY_MESSAGES} unrelated messages")

    def notify(self, method: str) -> None:
        self.send({"jsonrpc": "2.0", "method": method})

    def close(self) -> None:
        """Kill the whole process group and reap the child. Always."""
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self._proc.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            self._proc.wait(timeout=5)
        for stream in (self._proc.stdin, self._proc.stdout):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()
        self._reader.close()


def list_tools(client: StdioClient) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    cursor: object = None
    for _ in range(MAX_LIST_PAGES):
        result = client.request("tools/list", {} if cursor is None else {"cursor": cursor})
        page = result.get("tools")
        if not isinstance(page, list) or not all(
                isinstance(t, dict) and isinstance(t.get("name"), str) for t in page):
            raise McpProtocolError("tools/list returned no list of named tools")
        tools.extend(page)
        if len(tools) > MAX_TOOLS:
            raise McpRefused(f"the server offers more than {MAX_TOOLS} tools")
        cursor = result.get("nextCursor")
        if cursor is None:
            names = [t["name"] for t in tools]
            if len(set(names)) != len(names):
                raise McpRefused("the server lists a tool name twice")
            return tools
        if not isinstance(cursor, str):
            raise McpProtocolError("tools/list returned a cursor that is not a string")
    raise McpRefused(f"the tool list runs past {MAX_LIST_PAGES} pages")


def result_text(result: Mapping[str, Any]) -> str:
    """The text of a tools/call result. Anything but text is named, not shown."""
    parts: list[str] = []
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" \
                    and isinstance(item.get("text"), str):
                parts.append(item["text"])
            else:
                kind = item.get("type") if isinstance(item, dict) else None
                parts.append(f"[{kind if isinstance(kind, str) else 'unknown'} content "
                             "not shown]")
    if not parts and "structuredContent" in result:
        with contextlib.suppress(TypeError, ValueError, RecursionError):
            parts.append(canonical(result["structuredContent"]).decode("utf-8"))
    return "\n".join(parts)


@dataclass(frozen=True)
class CallOutcome:
    server: str
    tool: str
    text: str            # untrusted: the caller turns it into Evidence
    is_error: bool
    description: str     # untrusted too


@dataclass(frozen=True)
class Drift:
    changed: tuple[str, ...]      # allowed, offered, but not as signed
    missing: tuple[str, ...]      # allowed, no longer offered
    unsigned: tuple[str, ...]     # offered, not on the allowlist

    @property
    def ok(self) -> bool:
        return not self.changed and not self.missing


class McpRegistry:
    """The operator's servers, the key that must have signed them, and a launcher.

    Built by trusted code and handed to the broker. Nothing here is reachable
    from a task except through ``mcp.call``.
    """

    def __init__(self, servers: Mapping[str, ServerSpec],
                 public_key: Ed25519PublicKey | None, *,
                 launcher: Launcher = seatbelt_launcher) -> None:
        self._servers = dict(servers)
        self._key = public_key
        self._launcher = launcher

    @property
    def names(self) -> list[str]:
        return sorted(self._servers)

    def spec(self, name: str) -> ServerSpec:
        try:
            return self._servers[name]
        except KeyError:
            raise McpRefused(f"no MCP server named {name!r}") from None

    def state(self, name: str) -> str:
        spec = self._servers.get(name)
        if spec is None:
            return "unknown"
        if self._key is None:
            return "no operator key"
        if not spec.signature:
            return "unsigned"
        return "signed" if verify_server(self._key, spec) else "bad signature"

    def verified(self, name: str) -> ServerSpec:
        state = self.state(name)
        if state != "signed":
            raise McpRefused(f"MCP server {name!r} is {state}. The operator must sign it")
        return self._servers[name]

    def check_call(self, name: str, tool: str, arguments: object,
                   egress_hosts: frozenset[str] = frozenset()) -> ServerSpec:
        """Everything that can be refused before a process starts or a person is asked."""
        spec = self.verified(name)
        if tool not in spec.tools:
            raise McpRefused(f"{tool!r} is not on the signed allowlist of {name!r}")
        size = argument_bytes(arguments)
        if not isinstance(arguments, dict) or size is None:
            raise McpRefused("arguments must be a JSON object")
        if size > MAX_ARGUMENT_BYTES:
            raise McpRefused(f"arguments are over {MAX_ARGUMENT_BYTES} bytes")
        missing = spec.egress_hosts - egress_hosts
        if missing:
            raise McpRefused(f"{name!r} needs network to {sorted(missing)}, which this "
                             "task's egress list does not allow")
        return spec

    @contextlib.contextmanager
    def _connect(self, spec: ServerSpec, workspace: Path, *, allow_network: bool,
                 cancel: threading.Event | None) -> Iterator[StdioClient]:
        deadline = time.monotonic() + spec.timeout_seconds
        try:
            proc = self._launcher(spec.argv, Path(workspace), allow_network,
                                  tuple(Path(p) for p in spec.read_paths))
        except sandbox.SandboxUnavailable as exc:
            raise McpRefused(f"refusing to run unconfined: {exc}") from None
        except OSError as exc:
            raise McpError(f"cannot start {spec.name!r}: {exc.strerror}") from None
        client = StdioClient(proc, deadline=deadline, cancel=cancel)
        try:
            result = client.request("initialize", {
                "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                "clientInfo": CLIENT_INFO})
            if not isinstance(result.get("protocolVersion"), str):
                raise McpProtocolError("initialize returned no protocol version")
            client.notify("notifications/initialized")
            yield client
        finally:
            client.close()

    def call(self, name: str, tool: str, arguments: Mapping[str, Any], workspace: Path, *,
             egress_hosts: frozenset[str] = frozenset(),
             cancel: threading.Event | None = None) -> CallOutcome:
        spec = self.check_call(name, tool, arguments, egress_hosts)
        with self._connect(spec, workspace, allow_network=bool(spec.egress_hosts),
                           cancel=cancel) as client:
            offered = {t["name"]: t for t in list_tools(client)}.get(tool)
            if offered is None:
                raise McpRefused(f"{name!r} no longer offers {tool!r}")
            if tool_fingerprint(offered) != spec.tools[tool]:
                raise McpRefused(f"{name}/{tool} changed since the operator signed it. It is "
                                 "refused until the operator signs again")
            sent = client.bytes_sent
            try:
                result = client.request("tools/call",
                                        {"name": tool, "arguments": dict(arguments)})
            except McpRpcError:
                raise                    # the server answered: the outcome is known
            except (McpError, OSError) as exc:
                if client.bytes_sent == sent:
                    raise                # nothing reached the server: a refusal
                reason = str(exc) if isinstance(exc, McpError) else type(exc).__name__
                raise McpOutcomeUnknown(
                    f"{name}/{tool} was sent and then failed ({type(exc).__name__}: "
                    f"{reason}). It may have run") from exc
        description = offered.get("description", "")
        return CallOutcome(name, tool, result_text(result), result.get("isError") is True,
                           description if isinstance(description, str) else "")

    def snapshot(self, name: str, workspace: Path) -> list[dict[str, str]]:
        """What the server offers now, for the operator to read and sign. No network."""
        spec = self.spec(name)
        with self._connect(spec, workspace, allow_network=False, cancel=None) as client:
            tools = list_tools(client)
        out = []
        for tool in sorted(tools, key=lambda t: str(t["name"])):
            description = tool.get("description", "")
            out.append({"name": clean(str(tool["name"])), "sha256": tool_fingerprint(tool),
                        "description": clean(description if isinstance(description, str)
                                             else "")[:MAX_DESCRIPTION_CHARS]})
        return out

    def drift(self, name: str, workspace: Path) -> Drift:
        """Compare a signed server's live tool list with its signed snapshot."""
        spec = self.verified(name)
        with self._connect(spec, workspace, allow_network=False, cancel=None) as client:
            live = {t["name"]: tool_fingerprint(t) for t in list_tools(client)}
        return Drift(
            changed=tuple(t for t in spec.tools if t in live and live[t] != spec.tools[t]),
            missing=tuple(t for t in spec.tools if t not in live),
            unsigned=tuple(sorted(t for t in live if t not in spec.tools)))


def proposed(spec: ServerSpec, offered: Sequence[Mapping[str, str]],
             allow: Sequence[str] | None) -> ServerSpec:
    """The entry the operator would sign: the snapshot's fingerprints for the
    allowed tools (all offered tools when ``allow`` is None), unsigned."""
    by_name = {t["name"]: t["sha256"] for t in offered}
    names = list(by_name) if allow is None else list(allow)
    unknown = [n for n in names if n not in by_name]
    if unknown:
        raise McpConfigError(f"the server does not offer {unknown}")
    return dataclasses.replace(spec, tools={n: by_name[n] for n in names},
                               signed_by="", signature=None)
