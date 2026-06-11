"""RED tests for Fix D2 (description ADF extraction in _extract_jira_field).

Historical bug (bug 85a1-f581-2252-4a21): the fetcher persists Jira's REST
``description`` field verbatim — as an ADF document (`{"type": "doc",
"version": 1, "content": [...]}`). The outbound differ's
``_extract_jira_field`` fallback was ``raw.get("name", raw.get("displayName", ""))``
which returned ``""`` for ADF dicts because neither key exists. Diff then
compared the local plain-text description against ``""`` and reported the
field as changed on every pass — driving the 21-mutation idempotency
churn documented in the e2e probe Phase 6.

These tests assert the fix: description ADF dicts are decoded via
``adf.adf_to_text`` so single-line and multi-paragraph descriptions match
their local plain-string counterparts.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
DIFFER_PATH = (
    REPO_ROOT
    / "src"
    / "rebar"
    / "_engine"
    / "rebar_reconciler"
    / "outbound_differ.py"
)


def _load_differ():
    spec = importlib.util.spec_from_file_location(
        "outbound_differ_adf_test", DIFFER_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["outbound_differ_adf_test"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def differ():
    if not DIFFER_PATH.exists():
        pytest.fail(f"outbound_differ.py not found at {DIFFER_PATH}")
    return _load_differ()


def _adf_doc(*lines: str) -> dict:
    """Build a minimal ADF doc — paragraphs for each line, empty paragraphs for blank lines."""
    paragraphs = []
    for line in lines:
        if line:
            paragraphs.append(
                {"type": "paragraph", "content": [{"type": "text", "text": line}]}
            )
        else:
            paragraphs.append({"type": "paragraph", "content": []})
    return {"type": "doc", "version": 1, "content": paragraphs}


def test_extract_jira_field_decodes_single_line_adf_description(differ):
    """An ADF doc with one paragraph extracts to that line as plain text."""
    jira_fields = {"description": _adf_doc("Hello world")}
    got = differ._extract_jira_field(jira_fields, "description")
    assert got == "Hello world", (
        f"single-line ADF description should extract to plain text; got {got!r}"
    )


def test_extract_jira_field_decodes_multi_paragraph_adf_description(differ):
    """Multi-paragraph ADF round-trips through adf_to_text matching local format."""
    jira_fields = {"description": _adf_doc("First", "", "Second")}
    got = differ._extract_jira_field(jira_fields, "description")
    # text_to_adf splits on \n; adf_to_text adds \n per paragraph then rstrip.
    # "First\n\nSecond" → 3 paragraphs (First, empty, Second) → "First\n\nSecond"
    assert got == "First\n\nSecond", (
        f"multi-paragraph ADF must round-trip cleanly; got {got!r}"
    )


def test_diff_fields_does_not_flag_description_when_matches_jira_adf(differ):
    """The headline regression: identical description on both sides must NOT diff.

    Before the fix, this test failed because _extract_jira_field returned ""
    for any ADF dict, making ``"some text" != ""`` always true — the 21-
    mutation idempotency churn.
    """
    ticket = {
        "title": "T",
        "description": "Testing Fix 5 and Fix 1 sync",
        "ticket_type": "task",
        "priority": 2,
        "status": "open",
        "assignee": "",
    }
    jira_fields = {
        "summary": "T",
        "description": _adf_doc("Testing Fix 5 and Fix 1 sync"),
        "issuetype": {"name": "Task"},
        "priority": {"name": "Medium"},
        "status": {"name": "To Do"},
        "assignee": None,
    }
    changed = differ._diff_fields(ticket, jira_fields)
    assert "description" not in changed, (
        f"matching description must NOT appear in changed fields; got: {changed!r}"
    )


def test_extract_jira_field_legacy_plain_string_description_passthrough(differ):
    """Legacy snapshots (pre-ADF migration) store plain strings — must still work."""
    jira_fields = {"description": "Legacy plain text"}
    got = differ._extract_jira_field(jira_fields, "description")
    assert got == "Legacy plain text"


def test_extract_jira_field_assignee_still_returns_displayName(differ):
    """Assignee dict normalization is unchanged: still returns displayName.

    The orphan jira-dig-* mirror concern (49 tickets with assignee:None
    that would now diff against Jira's displayName) is a separate concern
    deferred to a follow-up; this test asserts the local probe's status quo.
    """
    jira_fields = {
        "assignee": {
            "accountId": "5a7...",
            "displayName": "Joe Oakhart",
            "emailAddress": "joe.oakhart@gmail.com",
        }
    }
    got = differ._extract_jira_field(jira_fields, "assignee")
    assert got == "Joe Oakhart"
