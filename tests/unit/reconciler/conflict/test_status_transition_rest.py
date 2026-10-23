"""Outbound status transition uses REST to list reachable transitions and post the matched
ID. Matching ignores case and accepts destination-state names; unreachable targets raise
actionable errors."""

from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS_DIR = REPO_ROOT / "src" / "rebar" / "_engine"
ACLI_PATH = SCRIPTS_DIR.parent / "reconciler" / "adapters" / "jira" / "acli.py"

# acli.py does `from rebar.reconciler.adapters.jira.adf import text_to_adf`.
# When loaded via spec_from_file_location the import searches sys.path;
# pre-register the adf submodule manually so the import resolves.
_ADF_PATH = SCRIPTS_DIR.parent / "reconciler" / "adapters" / "jira" / "adf.py"
# acli.py also imports ``from rebar.reconciler.adapters.jira.comment_limits import ...``
# (bug 6afc-20ee-84e5-4dd5). Bootstrap it explicitly alongside adf.
_CL_PATH = SCRIPTS_DIR.parent / "reconciler" / "adapters" / "jira" / "comment_limits.py"


def _load_acli():
    return importlib.import_module("rebar.reconciler.adapters.jira.acli")


@pytest.fixture(scope="module")
def acli():
    if not ACLI_PATH.exists():
        pytest.fail(f"acli.py not found at {ACLI_PATH}")
    return _load_acli()


def _make_client_with_transitions(acli, transitions):
    """Build an AcliClient whose REST GET returns the given transitions list."""
    client = acli.AcliClient(
        jira_url="https://example.atlassian.net",
        user="u",
        api_token="t",
    )
    client._direct_rest_get = MagicMock(return_value={"transitions": transitions})
    client._direct_rest_post_raw = MagicMock(return_value=None)
    return client


def test_transition_uses_rest_post_with_transition_id(acli):
    """The new transition path GETs /transitions and POSTs the matched id."""
    client = _make_client_with_transitions(
        acli,
        [
            {"id": "11", "name": "To Do", "to": {"name": "To Do"}},
            {"id": "21", "name": "In Progress", "to": {"name": "In Progress"}},
            {"id": "31", "name": "Done", "to": {"name": "Done"}},
        ],
    )
    client.transition_issue_by_name("DIG-100", "In Progress")
    # GET first to list
    client._direct_rest_get.assert_called_once_with("/rest/api/3/issue/DIG-100/transitions")
    # POST with the matched transition id
    client._direct_rest_post_raw.assert_called_once_with(
        "/rest/api/3/issue/DIG-100/transitions",
        {"transition": {"id": "21"}},
    )


def test_transition_case_insensitive_name_match(acli):
    """'in progress' should match transition 'In Progress'."""
    client = _make_client_with_transitions(
        acli,
        [{"id": "21", "name": "In Progress", "to": {"name": "In Progress"}}],
    )
    client.transition_issue_by_name("DIG-200", "in progress")
    client._direct_rest_post_raw.assert_called_once()


def test_transition_matches_via_to_state_name(acli):
    """A transition named Move to Review matches a requested In Review destination through
    to.name."""
    client = _make_client_with_transitions(
        acli,
        [
            {"id": "41", "name": "Move to Review", "to": {"name": "In Review"}},
        ],
    )
    client.transition_issue_by_name("DIG-300", "In Review")
    client._direct_rest_post_raw.assert_called_once_with(
        "/rest/api/3/issue/DIG-300/transitions",
        {"transition": {"id": "41"}},
    )


def test_transition_unmapped_raises_with_available_set(acli):
    """When the target status isn't reachable, raise with the available transitions named."""
    client = _make_client_with_transitions(
        acli,
        [
            {"id": "11", "name": "To Do", "to": {"name": "To Do"}},
            {"id": "21", "name": "In Progress", "to": {"name": "In Progress"}},
        ],
    )
    with pytest.raises(Exception) as exc_info:
        client.transition_issue_by_name("DIG-400", "Blocked")
    msg = str(exc_info.value)
    # Error message must mention what was attempted and what was available
    assert "Blocked" in msg
    assert "To Do" in msg or "In Progress" in msg
    # No POST attempted
    client._direct_rest_post_raw.assert_not_called()


def test_transition_no_transitions_available_raises(acli):
    """Empty transitions list → cannot transition; raise."""
    client = _make_client_with_transitions(acli, [])
    with pytest.raises(RuntimeError):
        client.transition_issue_by_name("DIG-500", "Done")
    client._direct_rest_post_raw.assert_not_called()
