"""``minimal_env`` keeps what a Windows child needs to start (bug arctic-unsecular-esok).

Tests that hand a child a from-scratch environment to prove an import stays light dropped
``SYSTEMROOT``; without it Windows cannot load Winsock and ``import asyncio`` fails with
``OSError: [WinError 10106]`` before the test's own assertion runs.
"""

from __future__ import annotations

import pytest
from _subprocess_env import SubprocessEnv, minimal_env

pytestmark = pytest.mark.unit


def test_systemroot_is_carried_when_the_host_sets_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYSTEMROOT", "C:\\Windows")
    env = minimal_env(PYTHONPATH="src")
    assert env == {"SYSTEMROOT": "C:\\Windows", "PYTHONPATH": "src"}
    assert isinstance(env, SubprocessEnv)


def test_systemroot_is_omitted_when_the_host_lacks_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SYSTEMROOT", raising=False)
    assert minimal_env(PATH="/bin") == {"PATH": "/bin"}


def test_nothing_else_leaks_from_the_ambient_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REBAR_LEAK_PROBE", "1")
    assert "REBAR_LEAK_PROBE" not in minimal_env()
