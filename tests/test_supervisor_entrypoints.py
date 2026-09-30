"""Every place that builds a Supervisor is a decision about approval signatures (#190, #200).

A Supervisor built without an operator key does not check approval
signatures. That is right for tests and for a throwaway database, and wrong
for anything that can run against the lab's real database. The first test does
not judge the code; it fails when a construction site appears or disappears
so that whoever adds one has to write down which case it is. The rest check
that the command-line ways to skip the key are refused on a deployed machine.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from lab import supervisor
from lab.cli import main as cli_main
from lab.queue import TaskQueue

LAB = Path(__file__).resolve().parent.parent / "lab"

# file (relative to lab/) -> (number of construction sites, why that is right)
KNOWN_SITES = {
    "supervisor.py": (1, "the daemon entry point: require_operator_key unless --allow-unsigned"),
    "loop.py": (1, "lab tick: the CLI requires the key unless --allow-unsigned or a mock reply"),
    "attacks.py": (1, "the attack harness: a temporary database and a dummy secret only"),
}


def _sites() -> dict[str, int]:
    found: dict[str, int] = {}
    for path in sorted(LAB.rglob("*.py")):
        calls = 0
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call):
                func = node.func
                name = (func.id if isinstance(func, ast.Name)
                        else func.attr if isinstance(func, ast.Attribute) else None)
                calls += name == "Supervisor"
        if calls:
            found[path.relative_to(LAB).as_posix()] = calls
    return found


def test_no_new_supervisor_construction_site_without_a_recorded_reason() -> None:
    expected = {name: count for name, (count, _) in KNOWN_SITES.items()}
    assert _sites() == expected, (
        "the places that build a Supervisor changed. If a new one can run against a real "
        "database it must set require_operator_key; record it in KNOWN_SITES with the "
        f"reason. Found: {_sites()}")


def test_the_service_entry_points_ask_for_the_operator_key() -> None:
    for name in ("supervisor.py", "loop.py"):
        assert "require_operator_key" in (LAB / name).read_text(), name


@pytest.fixture
def deployed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    key = tmp_path / "operator.pub"
    key.write_text("public key")
    monkeypatch.setattr(supervisor, "DEPLOYED_OPERATOR_KEY", key)
    return key


def test_unsigned_mode_is_refused_where_the_deployed_key_exists(deployed: Path) -> None:
    with pytest.raises(supervisor.MissingOperatorKey, match="unsigned mode is refused"):
        supervisor.refuse_unsigned_when_deployed()
    deployed.unlink()
    supervisor.refuse_unsigned_when_deployed()          # a dev machine: allowed


def test_the_daemon_refuses_allow_unsigned_on_a_deployed_machine(
        deployed: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("LAB_OPERATOR_PUBKEY", raising=False)
    monkeypatch.delenv("LAB_LOG_DIR", raising=False)

    async def returns_at_once(self: supervisor.Supervisor, *args: object, **kwargs: object) -> None:
        return None

    # If the refusal were missing, main() would start a real supervisor; this makes
    # that fail the assertion below instead of running for ever.
    monkeypatch.setattr(supervisor.Supervisor, "run", returns_at_once)
    db = tmp_path / "lab.db"
    assert supervisor.main(["--db", str(db), "--allow-unsigned"]) == 2
    assert "unsigned mode is refused" in capsys.readouterr().err
    assert not db.exists()


@pytest.mark.parametrize("flags", [["--allow-unsigned"], ["--mock-reply", '{"summary": "x"}']])
def test_tick_refuses_the_unsigned_ways_in_on_a_deployed_machine(
        deployed: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str], flags: list[str]) -> None:
    from lab import loop
    from lab.model import BoundedModel, MockAdapter, ModelSpec
    monkeypatch.delenv("LAB_OPERATOR_PUBKEY", raising=False)
    spec = ModelSpec("m", "a" * 40, "a" * 40, context_tokens=8192, max_output_tokens=512,
                     weights_mb=1000, heavy=False)
    monkeypatch.setattr(loop, "model_from_env",
                        lambda: BoundedModel(spec, MockAdapter(['{"summary": "x"}'])))
    db = tmp_path / "lab.db"
    with TaskQueue(db, owner="seed"):
        pass
    assert cli_main(["--db", str(db), "tick", *flags]) == 1
    assert "unsigned mode is refused" in capsys.readouterr().err
