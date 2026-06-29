"""RED tests for bug 06a5: client-side assignee pre-validation.

Mirrors the Gap 8 pattern (transition_issue_by_name pre-validates against
/rest/api/3/issue/{key}/transitions) for assignee outbound mutations:
pre-validate against /rest/api/3/user/assignable/search so a bogus
assignee raises BEFORE the ACLI subprocess silently exit-0s.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS_DIR = REPO_ROOT / "src" / "rebar" / "_engine"
ACLI_PATH = SCRIPTS_DIR / "rebar_reconciler" / "acli.py"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
_ADF_PATH = SCRIPTS_DIR / "rebar_reconciler" / "adf.py"
if "rebar_reconciler" not in sys.modules:
    import types as _types

    _dr = _types.ModuleType("rebar_reconciler")
    _dr.__path__ = [str(SCRIPTS_DIR / "rebar_reconciler")]
    sys.modules["rebar_reconciler"] = _dr
if "rebar_reconciler.adf" not in sys.modules:
    _adf_spec = importlib.util.spec_from_file_location("rebar_reconciler.adf", _ADF_PATH)
    _adf_mod = importlib.util.module_from_spec(_adf_spec)
    sys.modules["rebar_reconciler.adf"] = _adf_mod
    _adf_spec.loader.exec_module(_adf_mod)
# acli.py also imports ``from rebar_reconciler.comment_limits import ...``
# (bug 6afc-20ee-84e5-4dd5). Bootstrap it explicitly alongside adf.
_CL_PATH = SCRIPTS_DIR / "rebar_reconciler" / "comment_limits.py"
if "rebar_reconciler.comment_limits" not in sys.modules:
    _cl_spec = importlib.util.spec_from_file_location("rebar_reconciler.comment_limits", _CL_PATH)
    _cl_mod = importlib.util.module_from_spec(_cl_spec)
    sys.modules["rebar_reconciler.comment_limits"] = _cl_mod
    _cl_spec.loader.exec_module(_cl_mod)


def _load_acli():
    spec = importlib.util.spec_from_file_location("acli_assignee_test", ACLI_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["acli_assignee_test"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def acli():
    if not ACLI_PATH.exists():
        pytest.fail(f"acli.py not found at {ACLI_PATH}")
    return _load_acli()


def _make_client(acli, users):
    client = acli.AcliClient(
        jira_url="https://example.atlassian.net",
        user="u",
        api_token="t",
        jira_project="DIG",
    )
    client._direct_rest_get = MagicMock(return_value=users)
    return client


def test_validate_assignee_returns_accountId_on_exact_email_match(acli):
    client = _make_client(
        acli,
        [
            {
                "accountId": "abc123",
                "emailAddress": "joe@example.com",
                "displayName": "Joe",
            },
            {
                "accountId": "xyz789",
                "emailAddress": "jane@example.com",
                "displayName": "Jane",
            },
        ],
    )
    got = client.validate_assignee_exists("joe@example.com", issue_key="DIG-100")
    assert got == "abc123"
    # Confirms it hit /assignable/search with issueKey scope and the query.
    called_path = client._direct_rest_get.call_args[0][0]
    assert "/rest/api/3/user/assignable/search" in called_path
    assert "issueKey=DIG-100" in called_path
    assert "query=joe" in called_path


def test_validate_assignee_raises_on_empty_user_list(acli):
    client = _make_client(acli, [])
    with pytest.raises(Exception) as exc_info:
        client.validate_assignee_exists("bogus@example.com", issue_key="DIG-200")
    msg = str(exc_info.value)
    assert "bogus@example.com" in msg


def test_validate_assignee_rejects_fuzzy_only_match(acli):
    """A non-exact (substring/relevance) search result must NOT be accepted.

    Bug 9b94 follow-up: an agent identity like ``loop-agent`` fuzzily matches an
    unrelated account (``Jira Triage Agent``) via Jira's substring search. Falling
    back to that first result would mis-assign the ticket; a non-exact result must
    be treated as no match so the caller leaves the issue unassigned.
    """
    client = _make_client(
        acli,
        [{"accountId": "712020:abc", "emailAddress": "", "displayName": "Jira Triage Agent"}],
    )
    with pytest.raises(acli.AssigneeNotFoundError):
        client.validate_assignee_exists("loop-agent", issue_key="REB-136")


# Three "Joe"s are assignable; only one is "Joe Oakhart".
_THREE_JOES = [
    {
        "accountId": "joe-oakhart-acct",
        "emailAddress": "joeoakhart@navapbc.com",
        "displayName": "Joe Oakhart",
    },
    {
        "accountId": "joe-stepp-acct",
        "emailAddress": "joe.stepp@example.com",
        "displayName": "Joe Stepp",
    },
    {
        "accountId": "joe-robinson-acct",
        "emailAddress": "joe.robinson@example.com",
        "displayName": "Joe Robinson",
    },
]


def test_validate_assignee_resolves_normalized_variant(acli):
    """A local assignee that is a normalized variant of EXACTLY ONE user's identity
    (e.g. ``joe-oakhart`` for ``Joe Oakhart`` — case/separator-insensitive) resolves
    to that user, instead of being dropped as unresolvable (which would unassign the
    ticket). Bug 9b94 follow-up: this is what makes ``joe-oakhart`` sync to Joe and
    converge, rather than the exact-only match unassigning it."""
    client = _make_client(acli, _THREE_JOES)
    assert client.validate_assignee_exists("joe-oakhart", issue_key="REB-9") == "joe-oakhart-acct"


def test_validate_assignee_ambiguous_partial_stays_unresolvable(acli):
    """A partial/ambiguous local assignee (``joe``) that does NOT uniquely normalize
    to a single user's full identity must stay unresolvable — never guess among the
    three Joes."""
    client = _make_client(acli, _THREE_JOES)
    with pytest.raises(acli.AssigneeNotFoundError):
        client.validate_assignee_exists("joe", issue_key="REB-9")


def test_validate_assignee_accepts_project_scope(acli):
    """CREATE path has no issue_key — must accept project_key instead."""
    client = _make_client(
        acli,
        [
            {
                "accountId": "abc123",
                "emailAddress": "joe@example.com",
                "displayName": "Joe",
            }
        ],
    )
    got = client.validate_assignee_exists("Joe", project_key="DIG")
    assert got == "abc123"
    called_path = client._direct_rest_get.call_args[0][0]
    assert "project=DIG" in called_path


def test_validate_assignee_requires_scope(acli):
    client = _make_client(acli, [])
    with pytest.raises(ValueError):
        client.validate_assignee_exists("joe@example.com")


def test_update_issue_validates_assignee_before_acli_dispatch(acli):
    """When kwargs contains a real assignee, validate first; raise blocks ACLI dispatch."""
    client = _make_client(acli, [])  # No assignable users → validation fails
    with patch.object(acli, "update_issue") as mod_update:
        with pytest.raises(acli.AssigneeNotFoundError):
            client.update_issue("DIG-300", assignee="bogus@example.com")
        # ACLI module-level update_issue must NOT be called when validation fails.
        mod_update.assert_not_called()


def test_update_issue_normalizes_assignee_to_accountId(acli):
    """Successful validation should pass the resolved accountId to ACLI, not the raw input."""
    client = _make_client(
        acli,
        [
            {
                "accountId": "abc123",
                "emailAddress": "joe@example.com",
                "displayName": "Joe",
            }
        ],
    )
    with patch.object(acli, "update_issue") as mod_update:
        mod_update.return_value = {}
        client.update_issue("DIG-400", assignee="joe@example.com")
        # The forwarded assignee kwarg should be the resolved accountId.
        _, kwargs = mod_update.call_args
        assert kwargs.get("assignee") == "abc123"


def test_update_issue_skips_validation_when_no_assignee(acli):
    """Non-assignee updates (e.g. summary-only) must not trigger /assignable/search."""
    client = _make_client(acli, [])
    with patch.object(acli, "update_issue") as mod_update:
        mod_update.return_value = {}
        client.update_issue("DIG-500", summary="new title")
        client._direct_rest_get.assert_not_called()
        mod_update.assert_called_once()


# ── bug 544e: the CREATE path resolves the assignee through the SAME validator as
# the UPDATE path, so an ambiguous/unmappable assignee is left UNASSIGNED (matching
# the update outcome) rather than passed raw to ACLI/Jira to fuzzy-match or default.
# CREATE has no issue key yet → resolution must use PROJECT scope.


def test_create_issue_normalizes_assignee_to_accountId(acli):
    """CREATE must forward the RESOLVED accountId to ACLI, not the raw assignee
    string (which Jira would fuzzy-resolve or default)."""
    client = _make_client(
        acli,
        [{"accountId": "abc123", "emailAddress": "joe@example.com", "displayName": "Joe"}],
    )
    with patch.object(acli.acli_cli_ops, "create_issue") as mod_create:
        mod_create.return_value = {"key": "DIG-1"}
        client.create_issue({"ticket_type": "task", "title": "t", "assignee": "joe@example.com"})
        _, kwargs = mod_create.call_args
        assert kwargs.get("assignee") == "abc123"
        # Resolution used PROJECT scope (create has no issue key yet).
        called_path = client._direct_rest_get.call_args[0][0]
        assert "project=DIG" in called_path


def test_create_issue_omits_ambiguous_assignee(acli):
    """CREATE with an ambiguous handle ('joe' → 3 Joes) must OMIT the assignee so the
    issue is left UNASSIGNED — never forward the raw string for Jira to mis-resolve."""
    client = _make_client(acli, _THREE_JOES)
    with patch.object(acli.acli_cli_ops, "create_issue") as mod_create:
        mod_create.return_value = {"key": "REB-1"}
        client.create_issue({"ticket_type": "task", "title": "t", "assignee": "joe"})
        _, kwargs = mod_create.call_args
        assert kwargs.get("assignee") in (None, ""), (
            f"ambiguous assignee should be omitted, got {kwargs.get('assignee')!r}"
        )


def test_create_issue_no_assignee_skips_validation(acli):
    """No assignee on create → no /assignable/search probe, issue created unassigned."""
    client = _make_client(acli, [])
    with patch.object(acli.acli_cli_ops, "create_issue") as mod_create:
        mod_create.return_value = {"key": "DIG-2"}
        client.create_issue({"ticket_type": "task", "title": "t"})
        client._direct_rest_get.assert_not_called()
        mod_create.assert_called_once()
