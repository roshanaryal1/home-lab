"""Read-only config presence display never reveals values or unknown names (#428)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lab.cli import main
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


def test_empty_secret_does_not_count_as_set() -> None:
    assert snapshot({"LAB_SECRET_X": ""})["LAB_SECRET_*"] == "unset"


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
