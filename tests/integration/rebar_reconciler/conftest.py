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
    """Pin ``JIRA_PROJECT=REB`` for these reconciler integration tests (overridable).

    Bug 626d made the configured project load-bearing: the inbound fetch is scoped to
    ``jira.project`` and fails closed on an empty key (it must not silently search the
    wrong project). These tests don't configure a project of their own, so pin the
    real project key here. ``REB`` matches the hermetic Jira fixtures under
    ``tests/fixtures/jira/`` (epic f89d, story A); the mock/fake clients ignore the
    project argument, so key-matching mock data of any project prefix still merges.

    Bug ad85 additionally made Cloud credentials load-bearing: ``_build_jira_backend``
    now fails loudly (``BackendEnvError``) when ``JIRA_URL`` / ``JIRA_USER`` /
    ``JIRA_API_TOKEN`` are absent or ``JIRA_USER`` is not an email, at parity with the
    DC ``JIRA_PAT`` guard. These reconcile paths build the (default Cloud) backend, so
    pin hermetic, valid creds here alongside the project (overridable per-test).
    """
    monkeypatch.setenv("JIRA_PROJECT", "REB")
    monkeypatch.setenv("JIRA_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_USER", "reconciler-tests@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "test-api-token")
    yield
