"""Secret broker and per-destination connectors (item 4.4, #15)."""

from __future__ import annotations

import base64
import logging
import urllib.parse
from pathlib import Path

import pytest

from lab.broker import ToolNotAllowed, ToolSession
from lab.connectors import Connector, ConnectorError
from lab.egress import Response
from lab.origin import Origin, SourceType
from lab.queue import Task
from lab.supervisor import Supervisor, SupervisorConfig
from lab.vault import REDACTED, Redactor, SecretUnavailable, Vault

SECRET = "s3cr3t-token/value+with=chars"
PUBLIC = "93.184.216.34"
HOST = "api.example.org"
CONNECTOR = Connector("dummy", HOST, "dummy_token", path_prefix="/v1/")


class Recorder:
    """Fake transport standing in for the destination server."""

    def __init__(self, respond=None) -> None:
        self.calls: list[dict] = []
        self.respond = respond or (lambda kw, target: Response(200, {}, b'{"ok": true}'))

    def __call__(self, ip, port, host, target, timeout, max_bytes, **kw) -> Response:
        self.calls.append({"ip": ip, "host": host, "target": target, **kw})
        return self.respond(kw, target)


def make(tmp_path: Path, recorder: Recorder, env=None) -> Supervisor:
    sup = Supervisor(
        SupervisorConfig(db_path=tmp_path / "lab.db", idle_poll_seconds=0.01),
        egress_resolver=lambda host, port: [PUBLIC], egress_transport=recorder,
        vault=Vault({"LAB_SECRET_DUMMY_TOKEN": SECRET} if env is None else env,
                    keychain=lambda name: None))
    sup.broker.add_connector(CONNECTOR)
    return sup


OPERATOR = Origin(SourceType.OPERATOR)


async def run_publish(sup: Supervisor, call_kwargs: dict, *, origin=OPERATOR) -> dict:
    """Drive a handler through the real approval flow: ask, grant, rerun."""
    seen: dict = {"results": []}

    async def handler(task: Task, tools: ToolSession) -> dict:
        result = tools.submit("connector.call", **call_kwargs)
        seen["results"].append(result)
        return {"ok": result.ok}

    sup.register("publisher", handler, tools={"connector.call"}, connectors={"dummy"})
    task_id = sup.queue.add_task("publish", agent_kind="publisher", origin=origin)
    await sup.run(max_tasks=1)
    seen["task_id"] = task_id
    if sup.queue.get(task_id).state == "awaiting_approval":
        (pending,) = sup.policy.pending()
        seen["intent"] = pending["intent"]
        sup.policy.grant(pending["id"], decided_by="operator")
        sup.stats.leased = 0
        await sup.run(max_tasks=1)
    return seen


def everything_stored(sup: Supervisor) -> str:
    """Every byte the lab wrote to its database, as text."""
    conn = sup.queue._conn
    parts = []
    for table in ("tasks", "events", "approvals", "operations", "artifacts", "leases"):
        for row in conn.execute(f"SELECT * FROM {table}"):  # nosemgrep
            parts.append(repr(tuple(row)))
    return "\n".join(parts)


# ------------------------------------------------------------------ vault


def test_a_secret_resolves_from_the_environment_then_the_keychain() -> None:
    assert Vault({"LAB_SECRET_MY_KEY": SECRET}).resolve("my-key") == SECRET
    assert Vault({}, keychain=lambda n: SECRET if n == "kc" else None).resolve("kc") == SECRET
    assert Vault({"LAB_SECRET_A": "environment-wins"},
                 keychain=lambda n: "keychain-loses").resolve("a") == "environment-wins"


@pytest.mark.parametrize("name", ["", "a b", "../x", "x" * 65, "a;b", "a\nb"])
def test_bad_secret_names_are_refused(name: str) -> None:
    with pytest.raises(SecretUnavailable, match="invalid secret name"):
        Vault({}, keychain=lambda n: None).resolve(name)


@pytest.mark.parametrize("value", ["abc123", "has\nnewline-in-it", "has\0nul-in-it-1"])
def test_weak_or_malformed_secrets_are_refused_without_echoing_them(value: str) -> None:
    with pytest.raises(SecretUnavailable) as caught:
        Vault({"LAB_SECRET_X": value}).resolve("x")
    assert value not in str(caught.value)


def test_a_missing_secret_says_which_name_and_nothing_else() -> None:
    with pytest.raises(SecretUnavailable, match="'nope' is not available"):
        Vault({}, keychain=lambda n: None).resolve("nope")


@pytest.mark.safety
def test_a_worker_cannot_enumerate_secrets() -> None:
    forbidden = {"list", "names", "keys", "all", "items", "enumerate", "dump"}
    assert not forbidden & {n.lower() for n in dir(Vault) if not n.startswith("_")}
    assert not hasattr(ToolSession, "vault") and not hasattr(ToolSession, "secrets")


# ---------------------------------------------------------------- redaction


def test_redactor_scrubs_every_encoding_and_nested_structure() -> None:
    forms = [SECRET, urllib.parse.quote(SECRET, safe=""), urllib.parse.quote_plus(SECRET),
             base64.b64encode(SECRET.encode()).decode(),
             base64.urlsafe_b64encode(SECRET.encode()).decode(),
             base64.b64encode(SECRET.encode()).decode().rstrip("=")]
    value = {"a": [f"x {f} y" for f in forms], "b": {"c": f"Bearer {SECRET}"}, "n": 5}
    out = Redactor([SECRET]).scrub(value)
    text = repr(out)
    assert all(f not in text for f in forms) and REDACTED in text and out["n"] == 5


# --------------------------------------------------------------- connectors


@pytest.mark.parametrize("kwargs", [
    {"host": "*.example.org"}, {"host": "127.0.0.1"}, {"host": "localhost"},
    {"name": "bad name"}, {"secret": "../x"}, {"header": "Bad Header"},
    {"header": "X\r\nInjected"}, {"methods": frozenset()}, {"methods": frozenset({"TRACE"})},
    {"path_prefix": "no-slash"}, {"path_prefix": "/a b"},
])
def test_bad_connector_definitions_are_refused(kwargs) -> None:
    base = {"name": "c", "host": HOST, "secret": "s"}
    with pytest.raises(ConnectorError):
        Connector(**{**base, **kwargs})


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/v1/x", None),              # method not allowed
    ("POST", "/v2/x", None),             # outside the prefix
    ("POST", "/v1/../admin", None),      # traversal
    ("POST", "/v1//x", None),
    ("POST", "/v1/x y", None),
    ("POST", "v1/x", None),
    ("POST", "/v1/x", "b" * (64 * 1024 + 1)),
])
def test_calls_outside_the_connectors_scope_are_refused(method, path, body) -> None:
    with pytest.raises(ConnectorError):
        CONNECTOR.check_call(method, path, body)
    CONNECTOR.check_call("POST", "/v1/messages?draft=1", '{"a": 1}')


# --------------------------------------------------------- through the broker


@pytest.mark.safety
@pytest.mark.asyncio
async def test_the_call_needs_an_approval_that_shows_the_destination_but_not_the_secret(
        tmp_path: Path) -> None:
    rec = Recorder()
    sup = make(tmp_path, rec)
    seen = await run_publish(sup, {"connector": "dummy", "path": "/v1/post",
                                   "body": '{"text": "hi"}'})
    assert '"connector":"dummy"' in seen["intent"].replace(" ", "")
    assert "/v1/post" in seen["intent"] and SECRET not in seen["intent"]
    assert seen["results"][-1].ok
    (call,) = rec.calls
    assert call["headers"]["Authorization"] == f"Bearer {SECRET}"
    assert call["host"] == HOST and call["ip"] == PUBLIC and call["target"] == "/v1/post"
    assert call["method"] == "POST" and call["body"] == b'{"text": "hi"}'
    sup.close()


@pytest.mark.safety
@pytest.mark.asyncio
async def test_the_credential_appears_nowhere_the_lab_stores_or_logs(
        tmp_path: Path, caplog) -> None:
    """The server echoes the header back in every form it can; the lab's
    database, results, events, approvals and logs must still not hold it."""
    def echo(kw, target):
        header = kw["headers"]["Authorization"]
        body = (f"you sent {header} / {urllib.parse.quote(SECRET, safe='')} / "
                f"{base64.b64encode(SECRET.encode()).decode()}")
        return Response(200, {"content-type": "text/plain", "x-echo": header}, body.encode())

    caplog.set_level(logging.DEBUG)
    sup = make(tmp_path, Recorder(echo))
    seen = await run_publish(sup, {"connector": "dummy", "path": "/v1/x"})
    result = seen["results"][-1]
    assert result.ok and REDACTED in repr(result.detail)
    blob = everything_stored(sup) + repr(result) + caplog.text
    for form in (SECRET, urllib.parse.quote(SECRET, safe=""),
                 base64.b64encode(SECRET.encode()).decode()):
        assert form not in blob
    sup.close()


@pytest.mark.safety
@pytest.mark.asyncio
async def test_a_credentialed_request_never_follows_a_redirect(tmp_path: Path) -> None:
    rec = Recorder(lambda kw, target: Response(
        302, {"location": "https://evil.example.com/steal"}, b""))
    sup = make(tmp_path, rec)
    seen = await run_publish(sup, {"connector": "dummy", "path": "/v1/x"})
    result = seen["results"][-1]
    assert not result.ok and "redirect" in result.error
    assert len(rec.calls) == 1, "the credential went to one host only"
    assert SECRET not in repr(result)
    sup.close()


@pytest.mark.asyncio
async def test_a_task_without_the_grant_cannot_use_the_connector(tmp_path: Path) -> None:
    sup = make(tmp_path, Recorder())
    seen: dict = {}

    async def handler(task: Task, tools: ToolSession) -> dict:
        seen["r"] = tools.submit("connector.call", connector="dummy", path="/v1/x")
        return {}

    sup.register("nogrant", handler, tools={"connector.call"})
    sup.queue.add_task("t", agent_kind="nogrant", origin=OPERATOR)
    await sup.run(max_tasks=1)
    assert not seen["r"].ok and "ToolNotAllowed" in seen["r"].error
    assert sup.policy.pending() == [], "nobody was asked to approve it"
    sup.close()


@pytest.mark.asyncio
async def test_a_call_outside_the_connector_scope_is_refused_before_anyone_is_asked(
        tmp_path: Path) -> None:
    rec = Recorder()
    sup = make(tmp_path, rec)
    seen = await run_publish(sup, {"connector": "dummy", "path": "/admin/delete"})
    assert not seen["results"][-1].ok and "InvalidParams" in seen["results"][-1].error
    assert rec.calls == [] and sup.policy.pending() == []
    sup.close()


def test_registering_an_unknown_connector_for_a_task_is_refused(tmp_path: Path) -> None:
    sup = make(tmp_path, Recorder())
    with pytest.raises(ToolNotAllowed, match="unknown connectors"):
        sup.broker.open_workspace("t", {"connector.call"}, connectors={"invented"})
    sup.close()


@pytest.mark.asyncio
async def test_a_missing_secret_fails_the_call_without_leaking_anything(tmp_path: Path) -> None:
    rec = Recorder()
    sup = make(tmp_path, rec, env={})
    seen = await run_publish(sup, {"connector": "dummy", "path": "/v1/x"})
    result = seen["results"][-1]
    assert not result.ok and "SecretUnavailable" in result.error and rec.calls == []
    sup.close()


@pytest.mark.safety
@pytest.mark.asyncio
async def test_untrusted_input_can_never_reach_a_credentialed_connector(tmp_path: Path) -> None:
    """Rule of Two: connector.call holds a secret and an outside effect, so a
    task whose input is untrusted is refused before its handler runs."""
    rec = Recorder()
    sup = make(tmp_path, rec)
    ran: list[str] = []

    async def handler(task: Task, tools: ToolSession) -> dict:
        ran.append(task.id)
        return {}

    sup.register("publisher", handler, tools={"connector.call"}, connectors={"dummy"})
    task_id = sup.queue.add_task("from the web", agent_kind="publisher",
                                 origin=Origin(SourceType.WEB, "https://x"))
    await sup.run(max_tasks=1)
    assert ran == [] and sup.queue.get(task_id).state == "cancelled" and rec.calls == []
    sup.close()
