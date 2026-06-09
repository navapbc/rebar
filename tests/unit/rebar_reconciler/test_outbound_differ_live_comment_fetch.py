"""Tests for live comment fetch in outbound_differ when snapshot lacks 'comment' field.

Root cause: fetcher.fetch_snapshot → AcliClient.search_issues (acli-integration.py:1021+,
`acli jira workitem search --jql ... --paginate --json`) returns issue fields WITHOUT
the `comment` field (Jira search doesn't return comments). So in outbound_differ.py
`_diff_comments`, `jira_issue.get("comment", {})` is ALWAYS {} on live runs →
jira_comments=[] → every local comment re-emitted as add on every pass.

Fix: when the snapshot lacks a 'comment' field for a bound ticket with local comments,
fetch the Jira comments live via client.get_comments(jira_key). If the fetch fails,
skip comment mutations for that ticket with a loud warning + alert. If the snapshot
DOES carry a comment field (tests/synthetic), use it as today.

Test cases:
  (a) snapshot WITHOUT comment field + mock client returning the local comments
      → NO adds emitted (RED today: adds emitted without touching the client)
  (b) snapshot without comment field + client returns subset
      → exactly the missing ones emitted
  (c) client get_comments raises → no comment mutations + warning/alert asserted
  (d) snapshot WITH comment field → client NOT called (fixture path preserved)
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
OUTBOUND_DIFFER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "outbound_differ.py"
)


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def outbound_differ() -> ModuleType:
    # Use a unique name so this test module's load doesn't collide with other fixtures.
    return _load_module("outbound_differ_live_comment_fetch", OUTBOUND_DIFFER_PATH)


# ---------------------------------------------------------------------------
# Stub BindingStore
# ---------------------------------------------------------------------------


class StubBindingStore:
    def __init__(self, bindings: dict[str, str] | None = None) -> None:
        self._bindings: dict[str, str] = bindings or {}

    def get_jira_key(self, local_id: str) -> str | None:
        return self._bindings.get(local_id)

    def is_bound(self, local_id: str) -> bool:
        return local_id in self._bindings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ticket_with_comments(ticket_id: str, comment_bodies: list[str]) -> dict:
    return {
        "ticket_id": ticket_id,
        "title": "Some issue",
        "description": "desc",
        "status": "open",
        "priority": 2,
        "ticket_type": "bug",
        "assignee": "alice",
        "tags": [],
        "comments": [{"body": body} for body in comment_bodies],
        "deps": [],
    }


def _make_snapshot_without_comment_field(jira_key: str) -> dict:
    """Build a snapshot entry as search_issues returns it: NO 'comment' field.

    This is the live production shape: Jira search does not return comment data.
    """
    return {
        jira_key: {
            "summary": "Some issue",
            "description": "desc",
            "issuetype": "Bug",
            "priority": {"name": "Medium"},
            "status": {"name": "To Do"},
            "assignee": {"displayName": "alice", "emailAddress": "alice@example.com"},
            "labels": [],
            # NOTE: no "comment" key — this is the search-result shape
        }
    }


def _make_snapshot_with_comment_field(
    jira_key: str, comment_bodies: list[str]
) -> dict:
    """Build a snapshot entry WITH a 'comment' field (fixture/synthetic shape)."""
    jira_comments = [
        {"id": str(100 + i), "body": body} for i, body in enumerate(comment_bodies)
    ]
    return {
        jira_key: {
            "summary": "Some issue",
            "description": "desc",
            "issuetype": "Bug",
            "priority": {"name": "Medium"},
            "status": {"name": "To Do"},
            "assignee": {"displayName": "alice", "emailAddress": "alice@example.com"},
            "labels": [],
            "comment": {"comments": jira_comments, "total": len(jira_comments)},
        }
    }


def _make_stub_client(comments_to_return: list[dict]) -> MagicMock:
    """Return a mock AcliClient whose get_comments returns the given list."""
    client = MagicMock()
    client.get_comments.return_value = comments_to_return
    return client


# ---------------------------------------------------------------------------
# Test (a): snapshot WITHOUT comment field + client returns all local comments
#           → NO adds emitted
# ---------------------------------------------------------------------------


def test_no_comment_field_client_returns_all_local_no_adds_emitted(
    outbound_differ: ModuleType,
) -> None:
    """RED: snapshot lacks 'comment' field (live search shape), client returns
    comments that match all local comments → no adds should be emitted.

    Without the fix: _diff_comments sees comment_field={} (no 'comment' key),
    jira_comments=[], and re-emits every local comment as 'add' without ever
    calling client.get_comments(). This test fails red because adds ARE emitted.

    With the fix: _diff_comments detects the missing 'comment' field, calls
    client.get_comments(jira_key), gets back the same bodies, and emits nothing.
    """
    jira_key = "DIG-5301"
    local_bodies = ["Investigation note", "Fix plan", "Testing results"]
    ticket = _make_ticket_with_comments("local-live-1", local_bodies)
    store = StubBindingStore({"local-live-1": jira_key})
    snapshot = _make_snapshot_without_comment_field(jira_key)

    # Client returns all local comments as already in Jira (normalized, no ADF)
    jira_comment_dicts = [{"id": str(i), "body": body} for i, body in enumerate(local_bodies)]
    client = _make_stub_client(jira_comment_dicts)

    result = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=snapshot,
        binding_store=store,
        client=client,
    )

    # client.get_comments MUST have been called since snapshot lacks 'comment'
    client.get_comments.assert_called_once_with(jira_key)

    comment_mutations = [m for m in result if m.comments]
    assert comment_mutations == [], (
        f"Expected no comment mutations when client returns all local bodies. "
        f"Got mutations: {[m.comments for m in comment_mutations]}. "
        f"Root cause: _diff_comments did not call client.get_comments() when "
        f"'comment' field was absent from snapshot — treating jira_comments=[] "
        f"and re-emitting every local comment as 'add'."
    )


# ---------------------------------------------------------------------------
# Test (b): snapshot without comment field + client returns subset
#           → exactly the missing ones emitted
# ---------------------------------------------------------------------------


def test_no_comment_field_client_returns_subset_emits_missing_only(
    outbound_differ: ModuleType,
) -> None:
    """RED: snapshot lacks 'comment' field, client returns 2 of 3 local comments.
    The differ must emit exactly 1 add (the missing one).

    Without the fix: all 3 are re-emitted (client not consulted). RED.
    With the fix: 1 add for "Brand new comment" only.
    """
    jira_key = "DIG-5302"
    existing_in_jira = ["Investigation note", "Fix plan"]
    new_local_body = "Brand new comment"
    local_bodies = existing_in_jira + [new_local_body]

    ticket = _make_ticket_with_comments("local-live-2", local_bodies)
    store = StubBindingStore({"local-live-2": jira_key})
    snapshot = _make_snapshot_without_comment_field(jira_key)

    # Client returns only the 2 already-synced comments
    jira_comment_dicts = [
        {"id": str(i), "body": body} for i, body in enumerate(existing_in_jira)
    ]
    client = _make_stub_client(jira_comment_dicts)

    result = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=snapshot,
        binding_store=store,
        client=client,
    )

    client.get_comments.assert_called_once_with(jira_key)

    # Should have exactly 1 mutation with exactly 1 comment add
    comment_mutations = [m for m in result if m.comments]
    assert len(comment_mutations) == 1, (
        f"Expected 1 mutation with comment add. Got {len(comment_mutations)} mutations. "
        f"All result mutations: {result}"
    )
    assert len(comment_mutations[0].comments) == 1, (
        f"Expected exactly 1 comment add, got {len(comment_mutations[0].comments)}: "
        f"{comment_mutations[0].comments}"
    )
    emitted_body = comment_mutations[0].comments[0]["body"]
    assert new_local_body in emitted_body, (
        f"Expected the new comment body in the emitted add. "
        f"Got: {emitted_body!r}"
    )


# ---------------------------------------------------------------------------
# Test (c): client.get_comments raises → no comment mutations + warning
# ---------------------------------------------------------------------------


def test_no_comment_field_client_raises_skips_comment_mutations(
    outbound_differ: ModuleType,
    capsys,
) -> None:
    """RED: snapshot lacks 'comment' field, client.get_comments raises an exception.
    The differ must skip comment mutations for that ticket entirely and emit a warning.

    Without the fix: client.get_comments not called, adds still emitted blindly.
    With the fix: exception caught, no adds emitted, warning printed to stderr.
    """
    jira_key = "DIG-5303"
    local_bodies = ["First comment", "Second comment"]
    ticket = _make_ticket_with_comments("local-live-3", local_bodies)
    store = StubBindingStore({"local-live-3": jira_key})
    snapshot = _make_snapshot_without_comment_field(jira_key)

    client = MagicMock()
    client.get_comments.side_effect = RuntimeError("ACLI connection refused")

    result = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=snapshot,
        binding_store=store,
        client=client,
    )

    client.get_comments.assert_called_once_with(jira_key)

    # NO comment mutations must be emitted when the fetch fails
    comment_mutations = [m for m in result if m.comments]
    assert comment_mutations == [], (
        f"Expected NO comment mutations when get_comments raises. "
        f"Got: {[m.comments for m in comment_mutations]}. "
        f"Emitting blind comment adds when comment state is unknown is the "
        f"root cause of DIG-5301 reaching 14 comments."
    )

    # A warning must be emitted to stderr
    captured = capsys.readouterr()
    assert captured.err, (
        "Expected a warning on stderr when get_comments raises, got none."
    )
    assert jira_key in captured.err, (
        f"Expected the jira_key {jira_key!r} in the warning. Got: {captured.err!r}"
    )


# ---------------------------------------------------------------------------
# Test (d): snapshot WITH comment field → client NOT called (fixture path preserved)
# ---------------------------------------------------------------------------


def test_with_comment_field_in_snapshot_client_not_called(
    outbound_differ: ModuleType,
) -> None:
    """GREEN (fixture path): snapshot carries a 'comment' field (fixture/synthetic
    shape). The client must NOT be called — the snapshot data is sufficient.

    This verifies backward compatibility: existing unit test fixtures (which
    hand-feed the comment field) continue to work without touching the client.
    """
    jira_key = "DIG-5304"
    local_bodies = ["Already synced note"]
    ticket = _make_ticket_with_comments("local-live-4", local_bodies)
    store = StubBindingStore({"local-live-4": jira_key})
    # Snapshot HAS the comment field — the existing test fixture path
    snapshot = _make_snapshot_with_comment_field(jira_key, local_bodies)

    client = MagicMock()

    result = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=snapshot,
        binding_store=store,
        client=client,
    )

    # Client must NOT be called when the snapshot already has comment data
    client.get_comments.assert_not_called()

    # No adds should be emitted (the comment is already in Jira)
    comment_mutations = [m for m in result if m.comments]
    assert comment_mutations == [], (
        f"Expected no comment mutations when snapshot has the comment. "
        f"Got: {[m.comments for m in comment_mutations]}"
    )
