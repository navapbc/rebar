"""Description ADF extraction in the outbound field diff (Fix D2).

Historical bug (bug 85a1-f581-2252-4a21): the fetcher persists Jira's REST
``description`` field verbatim — as an ADF document (`{"type": "doc",
"version": 1, "content": [...]}`). The outbound differ's Jira-field extraction
fell back to ``raw.get("name", raw.get("displayName", ""))``, which returned
``""`` for ADF dicts because neither key exists. The diff then compared the
local plain-text description against ``""`` and reported the field as changed
on every pass — driving the 21-mutation idempotency churn documented in the
e2e probe Phase 6.

These tests drive the live diff (``outbound_field_diff.compute_update_fields``
with the Jira backend's mappers): an ADF description is decoded to plain text
before comparison, so single-line and multi-paragraph descriptions match their
local plain-string counterparts.
"""

from __future__ import annotations

from typing import Any

from rebar.reconciler.adapters.jira.backend import JiraBackend
from rebar.reconciler.outbound_field_diff import compute_update_fields


def _adf_doc(*lines: str) -> dict:
    """Build a minimal ADF doc — paragraphs for each line, empty paragraphs for blank lines."""
    paragraphs = []
    for line in lines:
        if line:
            paragraphs.append({"type": "paragraph", "content": [{"type": "text", "text": line}]})
        else:
            paragraphs.append({"type": "paragraph", "content": []})
    return {"type": "doc", "version": 1, "content": paragraphs}


def _ticket(**ov: Any) -> dict:
    t = {
        "title": "T",
        "description": "D",
        "ticket_type": "task",
        "priority": 2,
        "status": "open",
        "assignee": "",
    }
    t.update(ov)
    return t


def _jira(**ov: Any) -> dict:
    f = {
        "summary": "T",
        "description": "D",
        "issuetype": {"name": "Task"},
        "priority": {"name": "Medium"},
        "status": {"name": "To Do"},
        "assignee": None,
    }
    f.update(ov)
    return f


def _diff(ticket: dict, jira: dict) -> dict[str, Any]:
    """Run the live outbound field diff with the Jira backend's mappers."""
    backend = JiraBackend(transport=object())
    return compute_update_fields(
        ticket, jira, inbound_mapper=backend.inbound, outbound_mapper=backend.outbound
    )


def test_single_line_adf_description_matches_local_plain_text() -> None:
    """An ADF doc with one paragraph compares equal to that line as plain text."""
    changed = _diff(_ticket(description="Hello world"), _jira(description=_adf_doc("Hello world")))
    assert "description" not in changed, f"single-line ADF must decode; got {changed!r}"


def test_multi_paragraph_adf_description_matches_local_plain_text() -> None:
    """Multi-paragraph ADF round-trips through the decoder to the local format.

    "First\\n\\nSecond" → 3 paragraphs (First, empty, Second) → "First\\n\\nSecond".
    """
    changed = _diff(
        _ticket(description="First\n\nSecond"),
        _jira(description=_adf_doc("First", "", "Second")),
    )
    assert "description" not in changed, f"multi-paragraph ADF must round-trip; got {changed!r}"


def test_diff_does_not_flag_description_when_matches_jira_adf() -> None:
    """The headline regression: identical description on both sides must NOT diff."""
    changed = _diff(
        _ticket(description="Testing Fix 5 and Fix 1 sync"),
        _jira(description=_adf_doc("Testing Fix 5 and Fix 1 sync")),
    )
    assert "description" not in changed, (
        f"matching description must NOT appear in changed fields; got: {changed!r}"
    )


def test_legacy_plain_string_description_passes_through() -> None:
    """Legacy snapshots (pre-ADF migration) store plain strings — must still match."""
    changed = _diff(
        _ticket(description="Legacy plain text"), _jira(description="Legacy plain text")
    )
    assert "description" not in changed


def test_differing_adf_description_still_emits() -> None:
    """Decoding does not mask a real difference: a changed local body is pushed."""
    changed = _diff(_ticket(description="local edit"), _jira(description=_adf_doc("remote")))
    assert "description" in changed


def test_assignee_dict_matches_local_display_name() -> None:
    """A Jira assignee dict compares equal to a local displayName string."""
    jira = _jira(
        assignee={
            "accountId": "5a7...",
            "displayName": "Joe Oakhart",
            "emailAddress": "joe.oakhart@gmail.com",
        }
    )
    assert "assignee" not in _diff(_ticket(assignee="Joe Oakhart"), jira)
