"""Fixtures for rebar.reconciler integration tests.

Mirrors the unit-tier reconciler conftest: puts the engine on sys.path and
redirects the reconciler's repo-root fallback to a per-test temp dir so leaf
invocations don't write ``.tickets-tracker`` / ``.rebar/bridge`` into the working
tree (which would trip the repo-root leak guard in tests/conftest.py).
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ENGINE_DIR = Path(__file__).resolve().parents[3] / "src" / "rebar" / "_engine"


@pytest.fixture(autouse=True)
def _sandbox_repo_root(tmp_path, monkeypatch):
    monkeypatch.setenv("REBAR_ROOT", str(tmp_path))
    return


@pytest.fixture(autouse=True)
def _default_jira_project(monkeypatch):
    """Pin a valid REB project and hermetic Cloud credentials for these integration
    tests, while allowing per-test overrides and restoring the prior environment
    afterward. Inbound fetch rejects an empty project; backend construction rejects
    missing credentials or a non-email Cloud user.
    """
    monkeypatch.setenv("JIRA_PROJECT", "REB")
    monkeypatch.setenv("JIRA_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_USER", "reconciler-tests@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "test-api-token")
    return
