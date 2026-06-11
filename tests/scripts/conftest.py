"""Pytest fixtures for bridge field-coverage tests.

Constants and helper functions live in bridge_test_helpers.py — import from
there in test files.  This conftest.py only provides pytest fixtures that are
auto-discovered by pytest regardless of working directory.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Ensure tests/scripts/ is on sys.path so that `from bridge_test_helpers import ...`
# works regardless of how pytest is invoked (e.g., from a non-root directory).
_TESTS_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_SCRIPTS_DIR)

# Ensure the bundled engine scripts are importable for tests that load modules by path.
_SCRIPTS_DIR = str(Path(__file__).resolve().parents[2] / "src" / "rebar" / "_engine")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
ACLI_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "acli.py"


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def acli_mod() -> ModuleType:
    if not ACLI_PATH.exists():
        pytest.fail(f"acli.py not found at {ACLI_PATH}")
    return _load_module("acli_integration", ACLI_PATH)


# ---------------------------------------------------------------------------
# ACLI capture fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def acli_capture(
    acli_mod: ModuleType,
) -> tuple[Any, list[list[str]], Any]:
    """Provide an AcliClient with a fake _run_acli that captures commands.

    Returns:
        (client, captured_cmds, fake_run_acli) — the client is pre-configured
        with test credentials; captured_cmds accumulates every command list
        passed to _run_acli; fake_run_acli is the callable for use with
        patch.object.
    """
    captured_cmds: list[list[str]] = []

    def fake_run_acli(cmd: list[str], *, acli_cmd: list[str] | None = None) -> Any:
        captured_cmds.append(cmd)
        result = MagicMock()
        # get_issue uses "search --jql" (not "view"), which returns a list
        if "search" in cmd:
            result.stdout = json.dumps([{"key": "TEST-1"}])
        else:
            result.stdout = json.dumps({"key": "TEST-1"})
        return result

    client = acli_mod.AcliClient(
        jira_url="https://test.atlassian.net",
        user="test@example.com",
        api_token="fake-token",
        jira_project="TEST",
        acli_cmd=["echo"],
    )
    return client, captured_cmds, fake_run_acli


# ---------------------------------------------------------------------------
# urllib seam mock (bug 3775)
# ---------------------------------------------------------------------------
# AcliClient.create_issue() routes through the module-level create_issue ->
# _verify_created_issue, which calls urllib.request.urlopen directly when
# JIRA_URL / JIRA_USER / JIRA_API_TOKEN are present in the environment (a
# silent env-var behaviour switch). Under the socket guard (tests/conftest.py
# _network_guard) that real GET raises RuntimeError. This fixture mocks the
# urlopen seam so create_issue tests stay fully offline without an
# allow_network bridge. Mirrors the _mock_urlopen_verify helper landed for
# test_acli_integration.py (commit 27024174e7, bug 1c68).


@pytest.fixture
def mock_jira_verify() -> Iterator[MagicMock]:
    """Patch urllib.request.urlopen so _verify_created_issue stays offline.

    Returns a well-formed Jira issue GET response (key "TEST-1") so the
    verify-after-create REST path resolves without a real socket. Tests using
    this fixture exercise the create payload/argv, not the verify response.
    """
    body = json.dumps(
        {"key": "TEST-1", "summary": "Test", "status": {"name": "To Do"}}
    ).encode("utf-8")
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    with patch("urllib.request.urlopen", return_value=resp) as mock_urlopen:
        yield mock_urlopen
