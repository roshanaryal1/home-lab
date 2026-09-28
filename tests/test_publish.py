"""Reviewed publishing with durable receipts (item 8.6, #86)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from lab import publish
from lab.broker import ToolSession
from lab.cli import main
from lab.connectors import Connector, ConnectorError, load_connectors
from lab.egress import EgressGateway, Response
from lab.journal import OperationJournal
from lab.origin import Origin, SourceType
from lab.queue import Task
from lab.supervisor import Supervisor, SupervisorConfig
from lab.vault import Vault

SECRET = "publish-dummy-token-value"
HOST = "api.example.org"
PUBLIC = "93.184.216.34"
CONNECTOR = Connector("dummy", HOST, "dummy_token", path_prefix="/v1/",
                      idempotency_header="Idempotency-Key", receipt_field="id",
                      lookup_path="/v1/lookup/{key}")
OPERATOR = Origin(SourceType.OPERATOR)
DRAFT = json.dumps({"text": "Leases now carry a generation counter."})


class Provider:
    """A dummy provider. It honours idempotency keys, can lose its response
    after acting, and can be looked up by key."""

    def __init__(self) -> None:
        self.posts: dict[str, str] = {}
        self.requests: list[dict] = []
        self.lose_next_response = False
        self.honour_keys = True

    def __call__(self, ip, port, host, target, timeout, max_bytes, **kw) -> Response:
        self.requests.append({"target": target, **kw})
        headers = kw.get("headers") or {}
        if kw.get("method") == "GET" and target.startswith("/v1/lookup/"):
            key = target.rsplit("/", 1)[1]
            if key in self.posts:
                return Response(200, {}, json.dumps({"id": self.posts[key]}).encode())
            return Response(404, {}, b'{"error": "not found"}')
        key = headers.get("Idempotency-Key", "")
        if self.honour_keys and key in self.posts:
            post_id = self.posts[key]
        else:
            post_id = f"post-{len(self.posts) + 1}"
            self.posts[key or f"nokey-{len(self.posts)}"] = post_id
        if self.lose_next_response:
            self.lose_next_response = False
            raise OSError("connection reset by peer")     # it acted, the reply is lost
        return Response(201, {"content-type": "application/json"},
                        json.dumps({"id": post_id}).encode())

    @property
    def created(self) -> int:
        return len(self.posts)


def make(tmp_path: Path, provider: Provider) -> Supervisor:
    sup = Supervisor(
        SupervisorConfig(db_path=tmp_path / "lab.db", idle_poll_seconds=0.01),
        egress_resolver=lambda h, p: [PUBLIC], egress_transport=provider,
        vault=Vault({"LAB_SECRET_DUMMY_TOKEN": SECRET}, keychain=lambda n: None))
    sup.broker.add_connector(CONNECTOR)
    return sup


async def publish_once(sup: Supervisor, body: str = DRAFT, *, approve: bool = True,
                       task_id: str | None = None) -> tuple[str, dict]:
    seen: dict = {}

    async def handler(task: Task, tools: ToolSession) -> dict:
        result = tools.submit("connector.call", connector="dummy", path="/v1/posts", body=body)
        seen["result"] = result
        return {"ok": result.ok}

    sup.register("publisher", handler, tools={"connector.call"}, connectors={"dummy"})
    if task_id is None:
        task_id = sup.queue.add_task("publish", agent_kind="publisher", origin=OPERATOR)
    sup.stats.leased = 0
    await sup.run(max_tasks=1)
    if approve and sup.queue.get(task_id).state == "awaiting_approval":
        (pending,) = sup.policy.pending()
        seen["intent"] = json.loads(pending["intent"])
        sup.policy.grant(pending["id"], decided_by="operator")
        sup.stats.leased = 0
        await sup.run(max_tasks=1)
    return task_id, seen


def retry(sup: Supervisor, task_id: str) -> None:
    with sup.queue._tx():
        sup.queue._transition(task_id, "queued")
    sup.stats.leased = 0


def rows(sup: Supervisor) -> list:
    return sup.queue._conn.execute("SELECT * FROM publications ORDER BY id").fetchall()


# ---------------------------------------------------------- approval and receipt


@pytest.mark.safety
@pytest.mark.asyncio
async def test_the_approval_shows_the_destination_and_the_hash_of_the_draft(
        tmp_path: Path) -> None:
    sup = make(tmp_path, Provider())
    _, seen = await publish_once(sup)
    intent = seen["intent"]
    assert intent["preconditions"]["destination"] == HOST
    assert intent["preconditions"]["body_sha256"] == hashlib.sha256(DRAFT.encode()).hexdigest()
    assert intent["params"]["path"] == "/v1/posts" and intent["params"]["body"] == DRAFT
    assert SECRET not in json.dumps(intent)
    sup.close()


@pytest.mark.asyncio
async def test_a_send_stores_the_providers_receipt(tmp_path: Path) -> None:
    provider = Provider()
    sup = make(tmp_path, provider)
    _, seen = await publish_once(sup)
    assert seen["result"].ok
    (pub,) = rows(sup)
    assert pub["state"] == "confirmed" and pub["confirmed_via"] == "response"
    assert pub["provider_id"] == "post-1" and pub["status_code"] == 201
    assert pub["host"] == HOST and pub["path"] == "/v1/posts"
    assert pub["body_sha256"] == hashlib.sha256(DRAFT.encode()).hexdigest()
    assert pub["idempotency_key"] in provider.posts and provider.created == 1
    assert SECRET not in repr(tuple(pub))
    kinds = [r[0] for r in sup.queue._conn.execute("SELECT kind FROM events")]
    assert "publication_reserved" in kinds and "publication_confirmed" in kinds
    sup.close()


@pytest.mark.safety
@pytest.mark.asyncio
async def test_any_edit_to_the_draft_needs_a_new_approval(tmp_path: Path) -> None:
    provider = Provider()
    sup = make(tmp_path, provider)
    await publish_once(sup)                                   # approved and sent: 1 post
    edited = DRAFT.replace("generation counter", "generation counter.")   # one character
    task_id, _ = await publish_once(sup, edited, approve=False)
    assert sup.queue.get(task_id).state == "awaiting_approval", \
        "the approval for the old draft is spent and does not cover the new one"
    assert provider.created == 1, "the edited draft was not sent"
    (pending,) = sup.policy.pending()
    assert json.loads(pending["intent"])["preconditions"]["body_sha256"] == \
        hashlib.sha256(edited.encode()).hexdigest()
    sup.close()


# ----------------------------------------------------------------- lost responses


@pytest.mark.safety
@pytest.mark.asyncio
async def test_a_lost_response_never_leads_to_a_duplicate_post(tmp_path: Path) -> None:
    provider = Provider()
    sup = make(tmp_path, provider)
    provider.lose_next_response = True
    task_id, _ = await publish_once(sup)
    assert provider.created == 1, "the provider acted; only the reply was lost"
    (pub,) = rows(sup)
    assert pub["state"] == "reserved", "the attempt is on record, unconfirmed"
    journal = OperationJournal(sup.queue._conn)
    (op,) = journal.unresolved()
    assert op["state"] == "uncertain"

    # The retry does not resend: it is held for reconciliation.
    retry(sup, task_id)
    await sup.run(max_tasks=1)
    if sup.queue.get(task_id).state == "awaiting_approval":
        (pending,) = sup.policy.pending()
        sup.policy.grant(pending["id"], decided_by="operator")
        sup.stats.leased = 0
        await sup.run(max_tasks=1)
    assert provider.created == 1 and len(provider.requests) == 1, "nothing was sent again"
    assert sup.queue.get(task_id).state == "interrupted", "held for a person"

    result = publish.reconcile(
        sup.queue._conn, pub["idempotency_key"][:10],
        gateway=EgressGateway(lambda h, p: [PUBLIC], provider), vault=sup.broker._vault,
        connectors={"dummy": CONNECTOR}, journal=journal)
    assert result.outcome == "confirmed" and result.provider_id == "post-1"
    assert result.operation is not None
    (pub,) = rows(sup)
    assert pub["state"] == "confirmed" and pub["confirmed_via"] == "reconciliation"
    assert pub["provider_id"] == "post-1" and journal.unresolved() == []
    assert provider.created == 1
    sup.close()


@pytest.mark.asyncio
async def test_reconciliation_with_no_record_leaves_the_decision_to_a_person(
        tmp_path: Path) -> None:
    provider = Provider()
    sup = make(tmp_path, provider)

    original = provider.__call__

    def dies_before_acting(ip, port, host, target, timeout, max_bytes, **kw):
        if kw.get("method") == "POST":
            raise OSError("connection refused")           # never reached the provider
        return original(ip, port, host, target, timeout, max_bytes, **kw)

    sup.broker._egress._transport = dies_before_acting        # type: ignore[union-attr]
    task_id, _ = await publish_once(sup)
    assert provider.created == 0
    (pub,) = rows(sup)
    journal = OperationJournal(sup.queue._conn)
    result = publish.reconcile(
        sup.queue._conn, pub["idempotency_key"][:10],
        gateway=EgressGateway(lambda h, p: [PUBLIC], provider), vault=sup.broker._vault,
        connectors={"dummy": CONNECTOR}, journal=journal)
    assert result.outcome == "not_found" and "not-happened" in result.detail
    assert rows(sup)[0]["state"] == "not_found"
    assert len(journal.unresolved()) == 1, "nothing was decided on the person's behalf"
    assert task_id
    sup.close()


@pytest.mark.safety
@pytest.mark.asyncio
async def test_a_resend_after_a_wrong_not_happened_still_cannot_duplicate(
        tmp_path: Path) -> None:
    """The person resolves 'not happened' although it did; the resend carries
    the same idempotency key, so a provider that honours it returns the
    existing post."""
    provider = Provider()
    sup = make(tmp_path, provider)
    provider.lose_next_response = True
    task_id, _ = await publish_once(sup)
    (op,) = OperationJournal(sup.queue._conn).unresolved()
    OperationJournal(sup.queue._conn).resolve(op["id"], happened=False, decided_by="roshan")
    retry(sup, task_id)
    await sup.run(max_tasks=1)
    (pending,) = sup.policy.pending()
    sup.policy.grant(pending["id"], decided_by="operator")
    sup.stats.leased = 0
    await sup.run(max_tasks=1)
    assert len(provider.requests) == 2, "it was sent twice"
    assert provider.created == 1, "but the provider made one post"
    keys = {r["headers"]["Idempotency-Key"] for r in provider.requests}
    assert len(keys) == 1
    sup.close()


@pytest.mark.asyncio
async def test_the_key_is_stable_for_the_same_call_and_differs_for_a_different_draft(
        tmp_path: Path) -> None:
    provider = Provider()
    sup = make(tmp_path, provider)
    await publish_once(sup)
    await publish_once(sup, DRAFT + " ")
    keys = [r["headers"]["Idempotency-Key"] for r in provider.requests]
    assert len(keys) == 2 and keys[0] != keys[1] and all(len(k) == 40 for k in keys)
    sup.close()


def test_a_connector_without_a_lookup_cannot_be_reconciled(tmp_path: Path) -> None:
    from lab.queue import TaskQueue
    with TaskQueue(tmp_path / "lab.db") as q:
        q.policy = None      # type: ignore[attr-defined]
        q._conn.execute(
            "INSERT INTO publications (task_id, connector, host, method, path, body_sha256, "
            "params_sha256, idempotency_key) VALUES ('t', 'plain', ?, 'POST', '/v1/x', ?, ?, ?)",
            (HOST, "a" * 64, "b" * 64, "k" * 40))
        plain = Connector("plain", HOST, "s", path_prefix="/v1/")
        result = publish.reconcile(
            q._conn, "kkkkkk", gateway=EgressGateway(lambda h, p: [PUBLIC], Provider()),
            vault=Vault({}, keychain=lambda n: None), connectors={"plain": plain},
            journal=OperationJournal(q._conn))
        assert result.outcome == "cannot" and "no lookup" in result.detail
        with pytest.raises(publish.PublishError, match="at least 6"):
            publish.find(q._conn, "kk")
        with pytest.raises(publish.PublishError, match="no publication"):
            publish.find(q._conn, "zzzzzzzz")


# --------------------------------------------- the async (worker process) path


@pytest.mark.safety
@pytest.mark.asyncio
async def test_network_tools_work_on_the_async_path_used_by_worker_processes(
        tmp_path: Path) -> None:
    """Regression: they touched SQLite from the thread that runs the tool and
    failed with 'objects created in a thread can only be used in that same
    thread'. The database work now happens on the loop, the network in the thread."""
    provider = Provider()
    sup = make(tmp_path, provider)
    seen: dict = {}

    async def handler(task: Task, tools: ToolSession) -> dict:
        seen["fetch"] = await tools.submit_async("net.fetch", url=f"https://{HOST}/v1/lookup/x")
        seen["send"] = await tools.submit_async("connector.call", connector="dummy",
                                                path="/v1/posts", body=DRAFT)
        return {}

    sup.register("both", handler, tools={"net.fetch", "connector.call"}, connectors={"dummy"},
                 egress_hosts={HOST}, external_action=True, sensitive_data=True)
    sup.queue.add_task("t", agent_kind="both", origin=OPERATOR)
    await sup.run(max_tasks=1)
    # Untrusted input (fetching) plus a secret plus an outside effect: the Rule
    # of Two refuses this combination, so the handler must not have run at all.
    assert seen == {}
    kinds = [r[0] for r in sup.queue._conn.execute("SELECT kind FROM events")]
    assert "authority_refused" in kinds
    sup.close()


@pytest.mark.asyncio
async def test_net_fetch_and_connector_call_each_work_through_submit_async(
        tmp_path: Path) -> None:
    provider = Provider()
    sup = make(tmp_path, provider)
    out: dict = {}

    async def fetcher(task: Task, tools: ToolSession) -> dict:
        out["fetch"] = await tools.submit_async("net.fetch", url=f"https://{HOST}/v1/lookup/x")
        return {}

    async def sender(task: Task, tools: ToolSession) -> dict:
        out["send"] = await tools.submit_async("connector.call", connector="dummy",
                                               path="/v1/posts", body=DRAFT)
        return {}

    sup.register("fetcher", fetcher, tools={"net.fetch"}, egress_hosts={HOST},
                 external_action=True)
    sup.register("sender", sender, tools={"connector.call"}, connectors={"dummy"})
    sup.queue.add_task("f", agent_kind="fetcher")
    await sup.run(max_tasks=1)
    assert out["fetch"].ok and out["fetch"].detail["status"] == 404
    task_id = sup.queue.add_task("s", agent_kind="sender", origin=OPERATOR)
    sup.stats.leased = 0
    await sup.run(max_tasks=1)
    (pending,) = sup.policy.pending()
    sup.policy.grant(pending["id"], decided_by="operator")
    sup.stats.leased = 0
    await sup.run(max_tasks=1)
    assert out["send"].ok and provider.created == 1
    assert rows(sup)[0]["state"] == "confirmed" and task_id
    events = [r[0] for r in sup.queue._conn.execute("SELECT kind FROM events")]
    assert events.count("egress_intent") == 2 and "egress_allow" in events
    sup.close()


@pytest.mark.safety
@pytest.mark.asyncio
async def test_if_the_intent_cannot_be_recorded_nothing_is_sent(
        tmp_path: Path, monkeypatch) -> None:
    provider = Provider()
    sup = make(tmp_path, provider)
    real = sup.policy.audit

    def failing(task_id, kind, detail):
        if kind == "egress_intent":
            raise RuntimeError("audit store unavailable")
        return real(task_id, kind, detail)

    monkeypatch.setattr(sup.policy, "audit", failing)
    out: dict = {}

    async def fetcher(task: Task, tools: ToolSession) -> dict:
        out["r"] = await tools.submit_async("net.fetch", url=f"https://{HOST}/v1/lookup/x")
        return {}

    sup.register("fetcher", fetcher, tools={"net.fetch"}, egress_hosts={HOST},
                 external_action=True)
    sup.queue.add_task("f", agent_kind="fetcher")
    await sup.run(max_tasks=1)
    assert provider.requests == []
    sup.close()


# ------------------------------------------------------------ connectors file, CLI


def test_the_connectors_file_is_strict(tmp_path: Path) -> None:
    good = tmp_path / "c.json"
    good.write_text(json.dumps([{"name": "d", "host": HOST, "secret": "s", "methods": ["POST"],
                                 "path_prefix": "/v1/", "idempotency_header": "Idempotency-Key",
                                 "lookup_path": "/v1/lookup/{key}"}]))
    assert load_connectors(good)["d"].lookup_path == "/v1/lookup/{key}"
    for bad in ('{"not": "a list"}', '[{"name": "d", "host": "h.example.org", "secret": "s", '
                '"token": "sneaky"}]', '[{"name": "d"}]', "not json",
                '[{"name":"d","host":"a.example.org","secret":"s"},'
                '{"name":"d","host":"b.example.org","secret":"s"}]'):
        path = tmp_path / "bad.json"
        path.write_text(bad)
        with pytest.raises(ConnectorError):
            load_connectors(path)
    with pytest.raises(ConnectorError):
        load_connectors(tmp_path / "missing.json")


@pytest.mark.parametrize("lookup", ["/v1/no-key", "/other/{key}", "no-slash/{key}"])
def test_a_lookup_path_must_sit_under_the_prefix_and_carry_the_key(lookup: str) -> None:
    with pytest.raises(ConnectorError, match="lookup_path"):
        Connector("d", HOST, "s", path_prefix="/v1/", lookup_path=lookup)


@pytest.mark.asyncio
async def test_cli_list_show_and_reconcile(tmp_path: Path, capsys, monkeypatch) -> None:
    provider = Provider()
    sup = make(tmp_path, provider)
    provider.lose_next_response = True
    await publish_once(sup)
    (pub,) = rows(sup)
    sup.close()
    connectors = tmp_path / "connectors.json"
    connectors.write_text(json.dumps([{
        "name": "dummy", "host": HOST, "secret": "dummy_token", "path_prefix": "/v1/",
        "idempotency_header": "Idempotency-Key", "lookup_path": "/v1/lookup/{key}"}]))
    db = str(tmp_path / "lab.db")
    assert main(["--db", db, "publish", "list"]) == 0
    assert "reserved" in capsys.readouterr().out
    assert main(["--db", db, "publish", "show", pub["idempotency_key"][:8]]) == 0
    assert "body_sha256" in capsys.readouterr().out
    assert main(["--db", db, "publish", "show", "zzzzzzzz"]) == 1
    # The real gateway cannot reach the dummy host from a test, so a refusal is
    # reported as 'cannot' rather than crashing.
    monkeypatch.setenv("LAB_SECRET_DUMMY_TOKEN", SECRET)
    monkeypatch.setattr("lab.egress.system_resolver", lambda h, p: [PUBLIC])
    code = main(["--db", db, "publish", "reconcile", pub["idempotency_key"][:8],
                 "--connectors", str(connectors)])
    assert code in (0, 2)
    assert "CANNOT" in capsys.readouterr().out or code == 0
