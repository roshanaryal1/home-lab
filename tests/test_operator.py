"""Operator-signed approvals and a hardened approval CLI (item 4.5, #70)."""

from __future__ import annotations

import os
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from lab import operator as op
from lab.cli import _escape, main
from lab.policy import Decision, PolicyEngine, Tier
from lab.queue import TaskQueue


@pytest.fixture()
def keys(tmp_path: Path) -> tuple[Path, Path]:
    return op.generate(tmp_path / "operator-keys")


@pytest.fixture()
def q(tmp_path: Path) -> TaskQueue:
    with TaskQueue(tmp_path / "lab.db") as queue:
        queue._conn.execute("INSERT INTO tasks (id, title) VALUES ('t1', 't1')")
        yield queue


def ask(policy: PolicyEngine) -> str:
    """The gate opens a request for an approve-tier call; return its id."""
    result = policy.authorize_tool("t1", "fs.delete", {"path": "x"}, Tier.APPROVE)
    assert result.decision is Decision.NEEDS_APPROVAL and result.approval_id
    return result.approval_id


def call(policy: PolicyEngine) -> Decision:
    return policy.authorize_tool("t1", "fs.delete", {"path": "x"}, Tier.APPROVE).decision


# ------------------------------------------------------------------ keys


def test_keys_are_created_private_and_never_overwritten(tmp_path: Path) -> None:
    private, public = op.generate(tmp_path / "k")
    assert oct(private.stat().st_mode & 0o777) == "0o600"
    op.load_private(private), op.load_public(public)
    with pytest.raises(op.OperatorKeyError, match="already exists"):
        op.generate(tmp_path / "k")


def test_a_group_readable_private_key_is_refused(keys) -> None:
    os.chmod(keys[0], 0o640)
    with pytest.raises(op.OperatorKeyError, match="readable by group"):
        op.load_private(keys[0])


def test_garbage_keys_are_refused(tmp_path: Path) -> None:
    bad = tmp_path / "bad.key"
    bad.write_text("not a key")
    os.chmod(bad, 0o600)
    with pytest.raises(op.OperatorKeyError):
        op.load_private(bad)
    with pytest.raises(op.OperatorKeyError):
        op.load_public(bad)
    with pytest.raises(op.OperatorKeyError):
        op.load_public(tmp_path / "missing")


# ----------------------------------------------------- enforcement in policy


@pytest.mark.safety
def test_an_approval_the_agent_wrote_itself_is_not_honoured(q, keys) -> None:
    policy = PolicyEngine(q._conn, op.load_public(keys[1]))
    approval = ask(policy)
    # What the agent account can do: write the row directly, or call grant unsigned.
    q._conn.execute("UPDATE approvals SET state = 'granted', decided_by = 'me', "
                    "expires_at = strftime('%Y-%m-%d %H:%M:%f', 'now', '+1 hour') "
                    "WHERE id = ?", (approval,))
    assert call(policy) is Decision.NEEDS_APPROVAL
    kinds = [r[0] for r in q._conn.execute("SELECT kind FROM events")]
    assert "approval_rejected" in kinds


@pytest.mark.safety
def test_an_unsigned_grant_through_the_api_is_not_honoured(q, keys) -> None:
    policy = PolicyEngine(q._conn, op.load_public(keys[1]))
    approval = ask(policy)
    policy.grant(approval, decided_by="agent")          # no operator key
    assert call(policy) is Decision.NEEDS_APPROVAL


def test_a_signed_grant_is_honoured_once(q, keys) -> None:
    policy = PolicyEngine(q._conn, op.load_public(keys[1]))
    approval = ask(policy)
    policy.grant(approval, decided_by="roshan", signer=op.load_private(keys[0]))
    assert call(policy) is Decision.ALLOW
    assert call(policy) is Decision.NEEDS_APPROVAL, "single use"


@pytest.mark.safety
def test_a_signature_from_another_key_is_not_honoured(q, keys, tmp_path) -> None:
    other_private, _ = op.generate(tmp_path / "other")
    policy = PolicyEngine(q._conn, op.load_public(keys[1]))
    approval = ask(policy)
    policy.grant(approval, decided_by="mallory", signer=op.load_private(other_private))
    assert call(policy) is Decision.NEEDS_APPROVAL


@pytest.mark.safety
@pytest.mark.parametrize("edit", [
    "expires_at = strftime('%Y-%m-%d %H:%M:%f', 'now', '+30 days')",   # longer window
    "decided_by = 'someone else'",
    "action_hash = '" + "0" * 64 + "'",
])
def test_editing_a_signed_approval_voids_it(q, keys, edit) -> None:
    policy = PolicyEngine(q._conn, op.load_public(keys[1]))
    approval = ask(policy)
    policy.grant(approval, decided_by="roshan", signer=op.load_private(keys[0]))
    q._conn.execute(f"UPDATE approvals SET {edit} WHERE id = ?", (approval,))  # nosemgrep
    assert call(policy) is Decision.NEEDS_APPROVAL


def test_a_forged_row_does_not_shadow_a_genuine_approval(q, keys) -> None:
    policy = PolicyEngine(q._conn, op.load_public(keys[1]))
    forged = ask(policy)
    q._conn.execute("UPDATE approvals SET state = 'granted', decided_by = 'x', "
                    "expires_at = strftime('%Y-%m-%d %H:%M:%f', 'now', '+1 hour'), "
                    "requested_at = '2000-01-01 00:00:00.000' WHERE id = ?", (forged,))
    genuine = ask(policy)          # a fresh request, since the first is no longer pending
    policy.grant(genuine, decided_by="roshan", signer=op.load_private(keys[0]))
    assert call(policy) is Decision.ALLOW


def test_without_a_configured_key_signatures_are_not_checked(q) -> None:
    policy = PolicyEngine(q._conn)
    assert not policy.enforces_operator_signatures
    approval = ask(policy)
    policy.grant(approval, decided_by="anyone")
    assert call(policy) is Decision.ALLOW


def test_task_level_approvals_are_checked_too(q, keys) -> None:
    from lab.queue import Task
    policy = PolicyEngine(q._conn, op.load_public(keys[1]))
    task_id = q.add_task("needs approval", capability_tier="approve")
    task = q.get(task_id)
    assert isinstance(task, Task)
    assert policy.authorize(task).decision is Decision.NEEDS_APPROVAL
    approval_id = policy.request_approval(task, "review")
    policy.grant(approval_id, decided_by="agent")
    assert policy.authorize(task).decision is Decision.NEEDS_APPROVAL
    approval_id2 = policy.request_approval(task, "review again")
    policy.grant(approval_id2, decided_by="roshan", signer=op.load_private(keys[0]),
                 valid_for=timedelta(minutes=5))
    assert policy.authorize(task).decision is Decision.ALLOW


# --------------------------------------------------------------------- CLI


def run(db: Path, *argv: str) -> int:
    return main(["--db", str(db), *argv])


def make_pending(db: Path, n: int = 1) -> list[str]:
    ids = []
    with TaskQueue(db) as queue:
        policy = PolicyEngine(queue._conn)
        for i in range(n):
            queue._conn.execute("INSERT INTO tasks (id, title) VALUES (?, ?)",
                                (f"task{i}", f"title {i}"))
            res = policy.authorize_tool(f"task{i}", "fs.delete", {"path": str(i)}, Tier.APPROVE)
            ids.append(res.approval_id)
    return [i for i in ids if i]


def test_cli_operator_init_and_signed_approve(tmp_path: Path, capsys) -> None:
    db = tmp_path / "lab.db"
    (approval,) = make_pending(db)
    assert run(db, "operator", "init", "--dir", str(tmp_path / "k")) == 0
    assert run(db, "operator", "init", "--dir", str(tmp_path / "k")) == 1
    capsys.readouterr()
    assert run(db, "approve", approval[:8], "--by", "roshan",
               "--key", str(tmp_path / "k" / "operator.key")) == 0
    assert "signed" in capsys.readouterr().out
    with TaskQueue(db) as queue:
        policy = PolicyEngine(queue._conn, op.load_public(tmp_path / "k" / "operator.pub"))
        assert policy.authorize_tool("task0", "fs.delete", {"path": "0"},
                                     Tier.APPROVE).decision is Decision.ALLOW


def test_cli_unsigned_approve_warns(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.delenv("LAB_OPERATOR_KEY", raising=False)
    db = tmp_path / "lab.db"
    (approval,) = make_pending(db)
    assert run(db, "approve", approval[:8], "--by", "roshan") == 0
    captured = capsys.readouterr()
    assert "UNSIGNED" in captured.err and "UNSIGNED" in captured.out


def test_cli_refuses_a_wrong_or_open_key(tmp_path: Path, capsys) -> None:
    db = tmp_path / "lab.db"
    (approval,) = make_pending(db)
    private, _ = op.generate(tmp_path / "k")
    os.chmod(private, 0o644)
    assert run(db, "approve", approval[:8], "--by", "r", "--key", str(private)) == 1
    assert "readable by group" in capsys.readouterr().err


@pytest.mark.safety
def test_cli_refuses_an_ambiguous_prefix(tmp_path: Path, capsys) -> None:
    db = tmp_path / "lab.db"
    ids = make_pending(db, 2)
    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute("UPDATE approvals SET id = 'abcd0001' || substr(id, 9) WHERE id = ?", (ids[0],))
    conn.execute("UPDATE approvals SET id = 'abcd0002' || substr(id, 9) WHERE id = ?", (ids[1],))
    conn.close()
    for cmd in ("show", "approve", "deny"):
        argv = [cmd, "abcd", *(["--by", "r"] if cmd != "show" else [])]
        assert run(db, *argv) == 1
        assert "2 approvals match" in capsys.readouterr().err
    assert run(db, "show", "abcd0001") == 0


@pytest.mark.parametrize("prefix", ["%", "a%", "_", "ab", "zzzz", "abcd%"])
def test_sql_wildcards_and_short_prefixes_are_not_prefixes(tmp_path: Path, capsys, prefix) -> None:
    db = tmp_path / "lab.db"
    make_pending(db)
    assert run(db, "show", prefix) == 1
    err = capsys.readouterr().err
    assert "hex characters" in err or "No approval matching" in err


def test_expect_hash_binds_the_decision_to_what_was_reviewed(tmp_path: Path, capsys) -> None:
    db = tmp_path / "lab.db"
    (approval,) = make_pending(db)
    assert run(db, "approve", approval[:8], "--by", "r", "--expect-hash", "deadbeef") == 1
    assert "differs" in capsys.readouterr().err
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT state FROM approvals").fetchone()[0] == "pending"


def test_control_characters_cannot_rewrite_the_preview(tmp_path: Path, capsys) -> None:
    db = tmp_path / "lab.db"
    with TaskQueue(db) as queue:
        queue._conn.execute("INSERT INTO tasks (id, title) VALUES ('t', ?)",
                            ("harmless\x1b[2J\r‮delete everything",))
        policy = PolicyEngine(queue._conn)
        res = policy.authorize_tool("t", "fs.delete", {"path": "a\x1b[31mb"}, Tier.APPROVE)
    assert run(db, "show", res.approval_id[:8]) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out and "‮" not in out and "\r" not in out
    assert "\\u001b" in out


def test_escape_keeps_ordinary_text() -> None:
    assert _escape("plain text, unicode é") == "plain text, unicode é"
    assert _escape("a\nb") == "a\\u000ab"
