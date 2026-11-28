"""Read-only config presence display never reveals values or unknown names (#428)."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Iterator, Mapping, MutableMapping
from pathlib import Path

import pytest

from lab import cli
from lab.cli import CONFIG_ARGS_ERROR, main
from lab.config_view import CONFIG_NAMES, snapshot


def test_snapshot_is_a_sorted_allowlist_of_set_unset_statuses() -> None:
    actual = snapshot({"LAB_MODEL_URL": "http://127.0.0.1:8080/v1",
                       "LAB_LOG_LEVEL": "",
                       "LAB_UNKNOWN_VARIABLE": "never print me"})
    assert list(actual) == sorted(actual)
    assert actual["LAB_MODEL_URL"] == "set"
    assert actual["LAB_LOG_LEVEL"] == "set"
    assert actual["LAB_SECRET_*"] == "unset"
    assert set(actual) == set(CONFIG_NAMES) | {"LAB_SECRET_*"}
    assert "LAB_UNKNOWN_VARIABLE" not in actual


def test_secret_names_and_values_are_not_exposed() -> None:
    secret = "canary-private-value-7fa2"
    actual = snapshot({"LAB_OPERATOR_KEY": secret,
                       "LAB_SECRET_PRIVATE_CLIENT_NAME": secret,
                       "LAB_MODEL_NAME": secret,
                       "LAB_NEW_UNKNOWN": secret})
    rendered = json.dumps(actual)
    assert secret not in rendered
    assert "PRIVATE_CLIENT_NAME" not in rendered
    assert "LAB_NEW_UNKNOWN" not in rendered
    assert actual["LAB_OPERATOR_KEY"] == "set"
    assert actual["LAB_SECRET_*"] == "set"
    assert actual["LAB_MODEL_NAME"] == "set"


def test_empty_secret_is_still_a_configured_name() -> None:
    assert snapshot({"LAB_SECRET_X": ""})["LAB_SECRET_*"] == "set"


def test_config_does_not_access_environment_values() -> None:
    class NamesOnly(dict[str, str]):
        def __getitem__(self, key: str) -> str:
            raise AssertionError(f"value access forbidden: {key}")

        def __iter__(self):
            # A writer mutates the dictionary after the key iterator is
            # created. A stable keys snapshot keeps this harmless.
            names = tuple(super().__iter__())
            self["LAB_SECRET_LATE"] = "canary"
            return iter(names)

    values = NamesOnly({"LAB_MODEL_URL": "hidden"})
    result = snapshot(values)
    assert result["LAB_MODEL_URL"] == "set"
    assert result["LAB_SECRET_*"] == "unset"


@pytest.mark.parametrize("as_json", [False, True])
def test_cli_works_without_database_and_never_prints_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    as_json: bool,
) -> None:
    missing_db = tmp_path / "missing" / "lab.db"
    secret = "do-not-display-this-token-95c9"
    monkeypatch.setenv("LAB_MODEL_URL", secret + "\x1b[31m")
    monkeypatch.setenv("LAB_CHAT_ID", "123456\n" + secret)
    monkeypatch.setenv("LAB_SECRET_CLIENT", secret)
    monkeypatch.setenv("LAB_UNKNOWN_SECRET", secret)
    argv = ["--db", str(missing_db), "config", "show"]
    if as_json:
        argv.append("--json")
    assert main(argv) == 0
    captured = capsys.readouterr()
    assert not captured.err
    assert secret not in captured.out
    assert "123456" not in captured.out
    assert "\x1b" not in captured.out
    assert "LAB_SECRET_CLIENT" not in captured.out
    assert "LAB_UNKNOWN_SECRET" not in captured.out
    assert not missing_db.exists()
    assert not missing_db.parent.exists()
    if as_json:
        data = json.loads(captured.out)
        assert data["LAB_MODEL_URL"] == "set"
        assert data["LAB_CHAT_ID"] == "set"
        assert data["LAB_SECRET_*"] == "set"
    else:
        assert "LAB_MODEL_URL: set" in captured.out
        assert "LAB_CHAT_ID: set" in captured.out
        assert "LAB_SECRET_*: set" in captured.out


def test_cli_rejects_config_without_subcommand() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["config"])
    assert exc.value.code == 2


# A secret pasted into a malformed invocation, say `lab config show
# --json="$LAB_SECRET_TOKEN"`, must not come back on stderr: argparse quotes the
# word it rejects, so every config argument error prints fixed text instead.
CANARY = "canary-5d1f-not-for-stderr"
MALFORMED = [
    # Rejected by the config parsers themselves.
    ["config", "show", f"--json={CANARY}"],
    ["config", "show", f"--js={CANARY}"],
    ["config", "show", f"--help={CANARY}"],
    ["config", f"--help={CANARY}", "show"],
    ["config", CANARY],
    ["config", CANARY, "show"],
    ["config"],
    # Left over after `config show`, which the root parser reports.
    ["config", "show", CANARY],
    ["config", "show", f"--{CANARY}"],
    ["config", "show", "--", CANARY],
    ["config", "show", "--json", CANARY],
    ["config", f"--{CANARY}", "show"],
    ["config", "show", "--db", CANARY],
    ["config", "show", f"-x{CANARY}"],
    # A root option rejected before the command is chosen.
    [f"--debug={CANARY}", "config", "show"],
    [f"--version={CANARY}", "config", "show"],
    [f"--d={CANARY}", "config", "show"],
    [f"--{CANARY}", "config", "show"],
    ["--db", "unused.db", f"--debug={CANARY}", "config", "show"],
]


@pytest.mark.parametrize("argv", MALFORMED, ids=" ".join)
def test_a_malformed_config_invocation_never_repeats_its_arguments(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for name in (*CONFIG_NAMES, "LAB_SECRET_TOKEN"):
        monkeypatch.setenv(name, CANARY)
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert CANARY not in captured.out
    assert CANARY not in captured.err
    assert captured.out == ""
    assert captured.err == CONFIG_ARGS_ERROR


def test_other_commands_keep_the_argparse_message(capsys: pytest.CaptureFixture[str]) -> None:
    """The fixed text is for config invocations, not a change to every error."""
    with pytest.raises(SystemExit) as exc:
        main(["tasks", "--no-such-option"])
    assert exc.value.code == 2
    assert "unrecognized arguments: --no-such-option" in capsys.readouterr().err


class NamesOnlyEnviron(MutableMapping[str, str]):
    """Stands in for os.environ: names can be listed, but reading any LAB_ value fails.

    Mapping.get, items and values all go through __getitem__, so none of them gets
    past this, unlike a dict subclass, whose get ignores an overridden __getitem__.
    """

    def __init__(self, data: Mapping[str, str]) -> None:
        self._data = dict(data)

    def __getitem__(self, key: str) -> str:
        if key.startswith("LAB_"):
            raise AssertionError(f"read the value of {key}")
        return self._data[key]

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def __iter__(self) -> Iterator[str]:
        return iter(tuple(self._data))

    def __len__(self) -> int:
        return len(self._data)

    def __setitem__(self, key: str, value: str) -> None:
        self._data[key] = value

    def __delitem__(self, key: str) -> None:
        del self._data[key]


@pytest.mark.parametrize("argv, code", [
    (["config", "show"], 0),
    (["config", "show", "--json"], 0),
    (["config", "show", f"--json={CANARY}"], 2),
    ([f"--debug={CANARY}", "config", "show"], 2),
], ids=lambda v: " ".join(v) if isinstance(v, list) else str(v))
def test_main_reads_no_lab_value_for_config_show(
    argv: list[str],
    code: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Through main, not just snapshot: building the parser must not read the values
    other commands take their defaults from (LAB_OPERATOR_PUBKEY and the rest)."""
    real = {k: v for k, v in os.environ.items() if not k.startswith("LAB_")}
    guard = NamesOnlyEnviron({**real, **{name: CANARY for name in CONFIG_NAMES},
                              "LAB_SECRET_TOKEN": CANARY})
    with monkeypatch.context() as m:
        m.setattr(os, "environ", guard)
        with pytest.raises(AssertionError):
            os.environ.get("LAB_OPERATOR_PUBKEY")   # the guard is live
        try:
            result: object = main(argv)
        except SystemExit as exc:
            result = exc.code
    assert result == code
    captured = capsys.readouterr()
    assert CANARY not in captured.out + captured.err
    if code == 0:
        assert "LAB_OPERATOR_PUBKEY: set" in captured.out or (
            json.loads(captured.out)["LAB_OPERATOR_PUBKEY"] == "set")


# Commands whose option defaults come from the environment, which main now
# reads only after the command is chosen. Each must get what it got before.
PUBKEY_COMMANDS = [
    ["approve", "abc", "--by", "op"],
    ["mcp", "list"],
    ["repo", "list"],
    ["control", "show"],
]


def _parsed(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> argparse.Namespace:
    seen: list[argparse.Namespace] = []
    monkeypatch.setattr(cli, "_dispatch", lambda args: seen.append(args) or 0)
    assert main(argv) == 0
    return seen[0]


@pytest.mark.parametrize("argv", PUBKEY_COMMANDS, ids=" ".join)
def test_the_operator_pubkey_default_still_comes_from_the_environment(
    argv: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = tmp_path / "operator.pub"
    monkeypatch.setenv("LAB_OPERATOR_PUBKEY", str(key))
    assert _parsed(argv, monkeypatch).operator_pubkey == key
    explicit = tmp_path / "other.pub"
    # Right after the command word: mcp and repo take it before their subcommand.
    flagged = [argv[0], "--operator-pubkey", str(explicit), *argv[1:]]
    assert _parsed(flagged, monkeypatch).operator_pubkey == explicit
    monkeypatch.setenv("LAB_OPERATOR_PUBKEY", "")
    assert _parsed(argv, monkeypatch).operator_pubkey is None
    monkeypatch.delenv("LAB_OPERATOR_PUBKEY")
    assert _parsed(argv, monkeypatch).operator_pubkey is None


@pytest.mark.parametrize("argv, dest, env, fallback", [
    (["mcp", "list"], "servers", "LAB_MCP_SERVERS", "/etc/homelab/mcp.json"),
    (["repo", "list"], "sources", "LAB_REPO_SOURCES", "/etc/homelab/sources.json"),
], ids=["mcp", "repo"])
def test_the_list_file_default_still_comes_from_the_environment(
    argv: list[str], dest: str, env: str, fallback: str,
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    listed = tmp_path / "list.json"
    monkeypatch.setenv(env, str(listed))
    assert getattr(_parsed(argv, monkeypatch), dest) == listed
    monkeypatch.setenv(env, "")
    assert getattr(_parsed(argv, monkeypatch), dest) == Path("")
    monkeypatch.delenv(env)
    assert getattr(_parsed(argv, monkeypatch), dest) == Path(fallback)


def test_a_command_without_an_environment_default_gets_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """setup-plan's --operator-pubkey has no environment default and must not gain one."""
    monkeypatch.setenv("LAB_OPERATOR_PUBKEY", str(tmp_path / "operator.pub"))
    assert _parsed(["setup-plan", "--user", "lab"], monkeypatch).operator_pubkey is None
