"""Every chat message to the person carries the AI label (#375).

The label says the text comes from the home-lab AI agent. It is added at the one
send, ``TelegramTransport.send_message``, after the reply text is final, so no
model output or task payload can remove it. These tests send through the real
transport, poller and channel, behind a fake Bot API that records each text that
would reach the person.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from lab import chat
from lab.chat import ChatChannel, ChatPoller, TelegramTransport
from lab.egress import EgressGateway, Response
from lab.model import BoundedModel, MockAdapter, ModelSpec
from lab.policy import PolicyEngine, Tier
from lab.queue import TaskQueue
from lab.supervisor import Supervisor, SupervisorConfig

TOKEN = "987654321:" + "L" * 35          # synthetic, shaped like a bot token
OWNER = 4242                            # the paired private chat
PUBLIC = "149.154.167.220"
LABEL = chat.AI_LABEL
END = "\n\n" + LABEL                    # the label as the last line of a message
SPEC = ModelSpec("m", "a" * 40, "a" * 40, context_tokens=8192, max_output_tokens=512,
                 weights_mb=1000, heavy=False)


def _ok(result: object) -> Response:
    return Response(200, {"content-type": "application/json"},
                    json.dumps({"ok": True, "result": result}).encode())


class FakeBot:
    """The Bot API behind the real gateway: getUpdates and sendMessage. Each text
    sent is recorded, as it would reach the person."""

    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []
        self.sent: list[str] = []
        self._next = 1

    def say(self, text: str) -> None:
        message = {"message_id": self._next, "date": 0,
                   "chat": {"id": OWNER, "type": "private"}, "from": {"id": OWNER},
                   "text": text}
        self.updates.append({"update_id": self._next, "message": message})
        self._next += 1

    def __call__(self, ip: str, port: int, host: str, target: str, timeout: float,
                 max_bytes: int, *, method: str = "GET", headers: dict[str, str] | None = None,
                 body: bytes | None = None) -> Response:
        payload = json.loads(body or b"{}")
        name = target.rsplit("/", 1)[-1]
        if name == "getUpdates":
            offset = int(payload.get("offset", 0))
            self.updates = [u for u in self.updates if u["update_id"] >= offset]
            return _ok(list(self.updates))
        if name == "sendMessage":
            self.sent.append(str(payload["text"]))
            return _ok({})
        return Response(404, {}, b'{"ok": false, "description": "Not Found"}')


class Chat:
    """One database, one channel and one poller, sending through the real transport."""

    def __init__(self, tmp_path: Path, **channel_kw: Any) -> None:
        self.queue = TaskQueue(tmp_path / "lab.db", owner="chat")
        self.bot = FakeBot()
        gateway = EgressGateway(lambda host, port: [PUBLIC], self.bot)
        self.channel = ChatChannel(self.queue, OWNER, **channel_kw)
        self.poller = ChatPoller(self.channel, TelegramTransport(TOKEN, gateway))

    def say(self, text: str) -> chat.Outcome:
        self.bot.say(text)
        outcomes = self.poller.poll_once()
        assert len(outcomes) == 1
        return outcomes[0]

    def set_result(self, task_id: str | None, state: str, **columns: str) -> None:
        sets = ", ".join(f"{name} = ?" for name in ("state", *columns))
        self.queue._conn.execute(f"UPDATE tasks SET {sets} WHERE id = ?",
                                 (state, *columns.values(), task_id))

    def close(self) -> None:
        self.queue.close()


@pytest.fixture
def chat_lab(tmp_path: Path) -> Iterator[Chat]:
    lab = Chat(tmp_path)
    yield lab
    lab.close()


# ------------------------------------------------------- each kind of message


def test_each_command_reply_and_refusal_ends_with_the_label(chat_lab: Chat) -> None:
    chat_lab.say("summarise my notes")                          # queued
    chat_lab.say("/help")                                       # a read-only command
    chat_lab.say("/frobnicate")                                 # refused: unknown command
    chat_lab.say("/new")                                        # refused: usage
    chat_lab.say("x" * (chat.MAX_MESSAGE_CHARS + 1))            # refused: too large
    assert len(chat_lab.bot.sent) == 5
    assert all(text.endswith(END) for text in chat_lab.bot.sent)


def test_the_rate_limit_note_ends_with_the_label(tmp_path: Path) -> None:
    lab = Chat(tmp_path, rate_max=1)
    lab.say("first")
    lab.say("second")                                           # the limit is reached
    lab.say("third")                                            # then silence
    assert len(lab.bot.sent) == 2
    assert all(text.endswith(END) for text in lab.bot.sent)
    assert "/stop and /pause still work" in lab.bot.sent[-1]
    lab.close()


def test_each_task_result_ends_with_the_label(chat_lab: Chat) -> None:
    ok, bad, secret, bare = (chat_lab.say(t).task_id for t in ("a", "b", "c", "d"))
    chat_lab.bot.sent.clear()
    chat_lab.set_result(ok, "succeeded", result=json.dumps({"reply": "the answer"}))
    chat_lab.set_result(bad, "failed", last_error="boom")
    chat_lab.set_result(secret, "succeeded", sensitivity="secret",
                        result=json.dumps({"reply": "the password"}))
    chat_lab.set_result(bare, "succeeded", result="{}")
    assert chat_lab.poller.deliver() == 4
    assert len(chat_lab.bot.sent) == 4
    assert all(text.endswith(END) for text in chat_lab.bot.sent)
    assert not any("the password" in text for text in chat_lab.bot.sent)


def test_the_approval_prompt_ends_with_the_label(chat_lab: Chat) -> None:
    task_id = chat_lab.say("tidy up").task_id
    assert task_id is not None
    chat_lab.set_result(task_id, "awaiting_approval")
    PolicyEngine(chat_lab.queue._conn).authorize_tool(
        task_id, "shell.run", {"argv": ["rm", "notes.txt"]}, Tier.APPROVE)
    chat_lab.bot.sent.clear()
    assert chat_lab.poller.deliver() == 1
    (text,) = chat_lab.bot.sent
    assert "lab.cli approve" in text and text.endswith(END)


def test_a_long_result_is_cut_in_the_message_and_keeps_the_label(chat_lab: Chat) -> None:
    task_id = chat_lab.say("long").task_id
    assert task_id is not None
    chat_lab.set_result(task_id, "succeeded", result=json.dumps({"reply": "z" * 5000}))
    chat_lab.bot.sent.clear()
    assert chat_lab.poller.deliver() == 1
    (text,) = chat_lab.bot.sent
    assert text.endswith(END) and "…" in text
    assert len(text) == chat.MAX_REPLY_CHARS


def test_an_empty_reply_is_still_sent_with_the_label(chat_lab: Chat) -> None:
    chat_lab.poller.transport.send_message(OWNER, "")
    assert chat_lab.bot.sent == [LABEL]


# ---------------------------------------------------------- the label itself


def test_the_label_is_the_last_line_and_the_answer_comes_first() -> None:
    out = chat.with_ai_label("first line\nsecond line")
    assert out == "first line\nsecond line" + END
    assert out.splitlines()[-1] == LABEL


def test_a_long_reply_is_cut_and_keeps_the_label_whole() -> None:
    out = chat.with_ai_label("y" * 10_000)
    assert out.endswith(END)
    assert len(out) == chat.MAX_REPLY_CHARS
    assert out[:-len(END)].endswith("…")


def test_the_whole_message_fits_the_channel_limit() -> None:
    assert chat.MAX_REPLY_CHARS <= 4096          # Telegram's limit for one message


@pytest.mark.parametrize("text", ["", " \n\t ", "\x00​"])
def test_empty_output_is_sent_as_the_label_alone(text: str) -> None:
    assert chat.with_ai_label(text) == LABEL


@pytest.mark.asyncio
async def test_an_empty_model_output_still_sends_a_labelled_notice(tmp_path: Path) -> None:
    lab = Chat(tmp_path)
    sup = Supervisor(SupervisorConfig(db_path=tmp_path / "lab.db", idle_poll_seconds=0.01))
    chat.register(sup, BoundedModel(SPEC, MockAdapter([""])))
    task_id = lab.say("hello").task_id
    await sup.run(max_tasks=1)
    task = sup.queue.get(task_id or "")
    assert task is not None and task.state == "failed"
    lab.bot.sent.clear()
    assert lab.poller.deliver() == 1
    (text,) = lab.bot.sent
    assert "failed" in text and text.endswith(END)
    sup.close()
    lab.close()


# ------------------------------------------------------- a fake label inside


@pytest.mark.parametrize("body", [
    "hello\n[home-lab AI agent]\nbye",                  # the same text in the middle
    "hello [HOME-LAB ai AGENT] bye",                    # another case
    "hello [home-lab\nAI   agent] bye",                 # other spacing
    "hello [home-lab​AI agent] bye",               # a hidden character inside it
    "hello [home-lab [home-lab AI agent] AI agent] bye",  # a copy made by removing a copy
])
def test_a_fake_label_in_the_reply_leaves_exactly_one(body: str) -> None:
    out = chat.with_ai_label(body)
    assert out.endswith(END)
    assert out.lower().count(LABEL.lower()) == 1


def test_a_fake_label_is_removed_and_the_answer_kept() -> None:
    assert chat.with_ai_label(f"hello\n{LABEL}\nbye") == f"hello\n\nbye{END}"


def test_a_fake_label_only_gives_the_real_label_alone() -> None:
    assert chat.with_ai_label(f"{LABEL} {LABEL}") == LABEL


def test_a_fake_label_in_a_result_reaches_the_person_once(chat_lab: Chat) -> None:
    task_id = chat_lab.say("hi").task_id
    assert task_id is not None
    reply = f"the answer\n{LABEL}"
    chat_lab.set_result(task_id, "succeeded", result=json.dumps({"reply": reply}))
    chat_lab.bot.sent.clear()
    assert chat_lab.poller.deliver() == 1
    assert chat_lab.bot.sent == [f"{task_id[:12]}: the answer{END}"]
