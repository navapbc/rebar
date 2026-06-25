"""Fixtures for rebar_reconciler integration tests.

Mirrors the unit-tier reconciler conftest: puts the engine on sys.path and
redirects the reconciler's repo-root fallback to a per-test temp dir so leaf
invocations don't write ``.tickets-tracker`` / ``bridge_state`` into the working
tree (which would trip the repo-root leak guard in tests/conftest.py).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ENGINE_DIR = Path(__file__).resolve().parents[3] / "src" / "rebar" / "_engine"
if str(_ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(_ENGINE_DIR))


@pytest.fixture(autouse=True)
def _sandbox_repo_root(tmp_path, monkeypatch):
    monkeypatch.setenv("REBAR_ROOT", str(tmp_path))
    yield


@pytest.fixture(autouse=True)
def _default_jira_project(monkeypatch):
    """Pin ``JIRA_PROJECT=DIG`` for these reconciler integration tests (overridable).

    Bug 626d made the configured project load-bearing: the inbound fetch is scoped to
    ``jira.project`` and fails closed on an empty key (it must not silently search the
    wrong project). These tests use DIG-keyed mock data and don't configure a project,
    so pin DIG here just as the unit conftest does.
    """
    monkeypatch.setenv("JIRA_PROJECT", "DIG")
    yield
