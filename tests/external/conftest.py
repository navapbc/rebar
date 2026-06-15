"""Fixtures for the external-integration tier (tests/external/).

These tests hit third-party services (live LLM providers, etc.), so they are
marked ``external`` and excluded from the default test run. This conftest provides
the same temp git-backed rebar store the interface tier uses, scoped to this tier
so the suites stay independent.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

import rebar


def _env_truthy(name: str) -> bool:
    """True if env var *name* is set to a case-insensitive truthy value."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}


@pytest.fixture(autouse=True)
def _require_external_opt_in() -> None:
    """Make every test under tests/external/ INERT unless explicitly opted in.

    External tests hit live third-party services (real Jira mutations, billable
    LLM calls). They must not run during a default suite invocation even when
    credentials happen to be present in the environment — that is the leak this
    guard closes (see bug 4a48-6dd5-aef3-4c8e). This is IN ADDITION to each
    test's own credential skipif: both the opt-in env var AND credentials are
    required for an external test to actually execute.
    """
    if not _env_truthy("REBAR_RUN_EXTERNAL"):
        pytest.skip(
            "external tests are inert by default; set REBAR_RUN_EXTERNAL=1 "
            "(plus the relevant live credentials) to run them"
        )


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def rebar_repo(tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """An initialized rebar repo in a temp git dir (mirrors the interface tier)."""
    repo = Path(tmp_path) / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.setenv("PROJECT_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    yield repo
