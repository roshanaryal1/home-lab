"""Minimal async-test support, so the suite needs no pytest-asyncio.

`pytest-asyncio` would do this too, but it is a dependency for the sake
of a dozen lines, and this project is meant to run unattended for long
periods. Fewer moving parts is worth more here than plugin features we
would not use.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest


@pytest.fixture(autouse=True)
def _no_deployed_lab_key(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory):
    """The Mac mini has a real /etc/homelab/operator.pub, which makes unsigned mode
    refuse to run; tests that use it must not depend on the machine they run on."""
    from lab import supervisor
    monkeypatch.setattr(supervisor, "DEPLOYED_OPERATOR_KEY",
                        tmp_path_factory.getbasetemp() / "no-deployed-operator.pub")
    yield


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "asyncio: run this coroutine test in a fresh event loop"
    )


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem: pytest.Function):
    """Run coroutine tests to completion in their own event loop."""
    test = pyfuncitem.obj
    if not inspect.iscoroutinefunction(test):
        return None

    kwargs = {
        name: pyfuncitem.funcargs[name]
        for name in pyfuncitem._fixtureinfo.argnames
    }
    asyncio.run(test(**kwargs))
    return True
