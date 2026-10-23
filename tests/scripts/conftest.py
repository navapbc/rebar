"""Pytest-discovered bridge fixtures. Shared helpers are defined in bridge_test_helpers."""

from __future__ import annotations

import importlib
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

# Import repo scripts by bare name as production script entrypoints do.
_REPO_SCRIPTS_DIR = str(Path(__file__).resolve().parents[2] / "scripts")
if _REPO_SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _REPO_SCRIPTS_DIR)

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
ACLI_PATH = REPO_ROOT / "src" / "rebar" / "reconciler" / "adapters" / "jira" / "acli.py"


@pytest.fixture(scope="module")
def acli_mod() -> ModuleType:
    if not ACLI_PATH.exists():
        pytest.fail(f"acli.py not found at {ACLI_PATH}")
    return importlib.import_module("rebar.reconciler.adapters.jira.acli")


# ---------------------------------------------------------------------------
# ACLI capture fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def acli_capture(
    acli_mod: ModuleType,
) -> tuple[Any, list[list[str]], Any]:
    """Return a configured AcliClient, captured command lists, and its fake runner."""
    captured_cmds: list[list[str]] = []

    def fake_run_acli(
        cmd: list[str],
        *,
        acli_cmd: list[str] | None = None,
        retry_on_timeout: bool = False,
        call_timeout: float | None = None,
    ) -> Any:
        # retry_on_timeout: bug-d843 read/write flag now passed by callers.
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


# Mock urllib's verify-after-create GET. Credential-bearing environments otherwise
# trigger a network socket through AcliClient.create_issue during offline tests.


@pytest.fixture
def mock_jira_verify() -> Iterator[MagicMock]:
    """Keep verify-after-create offline with a valid TEST-1 issue response."""
    body = json.dumps({"key": "TEST-1", "summary": "Test", "status": {"name": "To Do"}}).encode(
        "utf-8"
    )
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    with patch("urllib.request.urlopen", return_value=resp) as mock_urlopen:
        yield mock_urlopen
