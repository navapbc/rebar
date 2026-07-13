"""Tests for comment deduplication in outbound_differ._diff_comments.

Regression tests for DIG-5287 / bug 4572 convergence blocker: the outbound
differ re-emitted every local comment as an "add" on every pass because:

  1. The differ looked for jira_issue.get("comments", []) but the Jira REST
     API stores comments at jira_issue["comment"]["comments"] (the outer key
     is "comment", not "comments").

  2. Jira Cloud v3 returns comment bodies as ADF documents (nested dicts),
     not plain strings. Without ADF-to-text normalisation, the differ saw
     local plain-text body != Jira ADF dict and re-emitted every comment.

Comparison pipeline (main's _normalize_comment_body):
  1. ADF dict → adf_to_text() (handles Jira Cloud v3 ADF bodies)
  2. RECONCILER_MARKER stripped (loop-breaker echo dedup)
  3. whitespace strip()
  Body equality after this pipeline → skip (already mirrored); otherwise emit
  with outbound decoration (_decorate_outbound_comment).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[4]
OUTBOUND_DIFFER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "outbound_differ.py"
)


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def outbound_differ() -> ModuleType:
    return _load_module("outbound_differ_comment_dedup", OUTBOUND_DIFFER_PATH)


# ---------------------------------------------------------------------------
# Stub BindingStore
# ---------------------------------------------------------------------------


class StubBindingStore:
    def __init__(self, bindings: dict[str, str] | None = None) -> None:
        self._bindings: dict[str, str] = bindings or {}

    def get_baseline(self, local_id):
        # story d6bd: baseline arbitration is always-on; unset -> None (local-wins).
        return None

    def is_pending(self, local_id):
        return False

    def get_jira_key(self, local_id: str) -> str | None:
        return self._bindings.get(local_id)

    def is_bound(self, local_id: str) -> bool:
        return local_id in self._bindings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_jira_snapshot_with_comments(
    jira_key: str,
    comment_bodies: list[str],
    as_adf: bool = False,
) -> dict:
    """Build a jira_snapshot dict structured as the fetcher produces it.

    The Jira REST API places comments at fields["comment"]["comments"], so
    the fetcher snapshot uses key "comment" (not "comments").
    comment_bodies: list of plain-text bodies.
    as_adf: if True, wrap each body in a minimal ADF doc (as Jira Cloud v3
    actually returns).
    """
    if as_adf:
        jira_comments = [
            {
                "id": str(100 + i),
                "body": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": body}],
                        }
                    ],
                },
            }
            for i, body in enumerate(comment_bodies)
        ]
    else:
        jira_comments = [
            {"id": str(100 + i), "body": body} for i, body in enumerate(comment_bodies)
        ]

    return {
        jira_key: {
            "summary": "Some issue",
            "description": "desc",
            "issuetype": "Bug",
            "priority": "Medium",
            "status": "To Do",
            "assignee": "alice",
            "labels": [],
            # Jira REST API shape: {"comment": {"comments": [...], "total": N}}
            "comment": {"comments": jira_comments, "total": len(jira_comments)},
        }
    }


def _make_ticket_with_comments(
    ticket_id: str,
    comment_bodies: list[str],
) -> dict:
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


# ---------------------------------------------------------------------------
# Test 1: All local comments already in Jira (plain-text bodies) → no mutation
# ---------------------------------------------------------------------------


def test_all_local_comments_in_jira_no_mutation_emitted(
    outbound_differ: ModuleType,
) -> None:
    """When every local comment body is already in the Jira snapshot, the
    differ must emit zero comment mutations — no duplicate 'add' on re-pass.

    This is the DIG-5287 / bug 4572 convergence blocker: Phase-6 stalled at
    27→18→18 mutations instead of converging to 0 because every comment was
    re-emitted.
    """
    jira_key = "DIG-5287"
    bodies = ["First note", "Second note", "Third note"]
    ticket = _make_ticket_with_comments("local-1", bodies)
    store = StubBindingStore({"local-1": jira_key})
    snapshot = _make_jira_snapshot_with_comments(jira_key, bodies)

    result, _ = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=snapshot,
        binding_store=store,
    )

    # No fields changed, no label changes — the ONLY driver would be comments.
    comment_mutations = [m for m in result if m.comments]
    assert comment_mutations == [], (
        f"Expected no comment mutations when all local bodies match Jira. "
        f"Got mutations: {[m.comments for m in comment_mutations]}. "
        f"Root cause: _diff_comments read 'comments' key instead of "
        f"'comment.comments' from the Jira snapshot."
    )


# ---------------------------------------------------------------------------
# Test 2: 1 new + 2 already-in-Jira → exactly 1 add emitted
# ---------------------------------------------------------------------------


def test_one_new_comment_plus_two_existing_emits_exactly_one_add(
    outbound_differ: ModuleType,
) -> None:
    """When local has 1 new comment and 2 already mirrored to Jira, the
    differ must emit exactly 1 'add' mutation — for the new comment only."""
    jira_key = "DIG-100"
    existing_bodies = ["Already synced A", "Already synced B"]
    local_bodies = existing_bodies + ["Brand new comment"]
    ticket = _make_ticket_with_comments("local-2", local_bodies)
    store = StubBindingStore({"local-2": jira_key})
    snapshot = _make_jira_snapshot_with_comments(jira_key, existing_bodies)

    result, _ = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=snapshot,
        binding_store=store,
    )

    assert len(result) == 1, f"Expected 1 mutation, got {len(result)}"
    m = result[0]
    assert len(m.comments) == 1, (
        f"Expected exactly 1 comment add, got {len(m.comments)}: {m.comments}"
    )
    assert m.comments[0]["action"] == "add"
    # The emitted body has the RECONCILER_MARKER decoration appended.
    assert "Brand new comment" in m.comments[0]["body"]


# ---------------------------------------------------------------------------
# Test 3: Empty Jira comment list → all local comments emitted
# ---------------------------------------------------------------------------


def test_empty_jira_comments_emits_all_local_as_adds(
    outbound_differ: ModuleType,
) -> None:
    """When the Jira snapshot has no comments, all local comments must be
    emitted as 'add' mutations."""
    jira_key = "DIG-200"
    local_bodies = ["First local", "Second local"]
    ticket = _make_ticket_with_comments("local-3", local_bodies)
    store = StubBindingStore({"local-3": jira_key})
    # Snapshot has the Jira issue but with no comments
    snapshot = _make_jira_snapshot_with_comments(jira_key, [])

    result, _ = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=snapshot,
        binding_store=store,
    )

    assert len(result) == 1
    m = result[0]
    assert len(m.comments) == 2, f"Expected 2 comment adds (empty Jira list), got {len(m.comments)}"
    # Each emitted body contains the local text (plus RECONCILER_MARKER decoration).
    emitted_texts = {c["body"] for c in m.comments}
    for body in local_bodies:
        assert any(body in emitted for emitted in emitted_texts), (
            f"Local body {body!r} not found in any emitted comment body"
        )


# ---------------------------------------------------------------------------
# Test 4: ADF bodies from Jira normalised via adf_to_text for comparison
# ---------------------------------------------------------------------------


def test_adf_comment_body_deduped_against_local_plain_text(
    outbound_differ: ModuleType,
) -> None:
    """Jira Cloud v3 returns comment bodies as ADF documents (nested dicts).
    The differ must normalise ADF to plain text before comparing to local
    plain-text bodies. A body present in Jira as ADF must NOT be re-emitted.

    Matching rule: _normalize_comment_body(jira_adf).strip() ==
                   _normalize_comment_body(local_text).strip()
    """
    jira_key = "DIG-300"
    plain_body = "Status update: work is in progress"
    ticket = _make_ticket_with_comments("local-4", [plain_body])
    store = StubBindingStore({"local-4": jira_key})
    # Jira snapshot has the body as ADF
    snapshot = _make_jira_snapshot_with_comments(jira_key, [plain_body], as_adf=True)

    result, _ = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=snapshot,
        binding_store=store,
    )

    comment_mutations = [m for m in result if m.comments]
    assert comment_mutations == [], (
        f"ADF body matching the local plain-text body must not be re-emitted. "
        f"Got comment mutations: {[m.comments for m in comment_mutations]}. "
        f"Fix: apply adf_to_text() to Jira ADF bodies before body-equality check."
    )


# ---------------------------------------------------------------------------
# Test 5: Whitespace differences normalised (trailing newlines, spaces)
# ---------------------------------------------------------------------------


def test_whitespace_normalisation_prevents_re_emit(
    outbound_differ: ModuleType,
) -> None:
    """Comment bodies with leading/trailing whitespace differences are treated
    as equal after strip(). Jira sometimes round-trips with a trailing newline."""
    jira_key = "DIG-400"
    local_body = "  Investigating root cause  "
    # Jira returns the same body but with different whitespace
    jira_body = "Investigating root cause\n"
    ticket = _make_ticket_with_comments("local-5", [local_body])
    store = StubBindingStore({"local-5": jira_key})
    snapshot = _make_jira_snapshot_with_comments(jira_key, [jira_body])

    result, _ = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=snapshot,
        binding_store=store,
    )

    comment_mutations = [m for m in result if m.comments]
    assert comment_mutations == [], (
        f"Whitespace-normalised bodies must be treated as equal. "
        f"Got comment mutations: {[m.comments for m in comment_mutations]}"
    )


# ---------------------------------------------------------------------------
# Test 6: Marker-decorated Jira comment matched against local plain comment
#
# The exact bug 4572 mismatch scenario: a comment was previously pushed
# outbound with _decorate_outbound_comment() (appending RECONCILER_MARKER).
# Jira returns it back in the snapshot with the marker still in the body.
# _normalize_comment_body strips the marker before comparison, so the
# decorated Jira body must match the undecorated local body → no re-emit.
# ---------------------------------------------------------------------------


def test_marker_decorated_jira_comment_deduped_against_local_plain(
    outbound_differ: ModuleType,
) -> None:
    """A Jira comment that carries the RECONCILER_MARKER decoration (from a
    previous outbound push) must match a local comment without the marker.

    _normalize_comment_body strips RECONCILER_MARKER before comparison, so
    the already-pushed decorated body must NOT be re-emitted as a new 'add'.
    This is the exact bug 4572 mismatch: without the marker strip, the
    Jira body "hello\\n\\n<marker>" never equals the local body "hello".
    """
    jira_key = "DIG-500"
    local_body = "hello"
    # Simulate what Jira returns after a previous outbound push: the body
    # includes the RECONCILER_MARKER appended by _decorate_outbound_comment.
    marker = outbound_differ.RECONCILER_MARKER
    jira_body_with_marker = f"{local_body}\n\n{marker}"

    ticket = _make_ticket_with_comments("local-6", [local_body])
    store = StubBindingStore({"local-6": jira_key})
    snapshot = _make_jira_snapshot_with_comments(jira_key, [jira_body_with_marker])

    result, _ = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=snapshot,
        binding_store=store,
    )

    comment_mutations = [m for m in result if m.comments]
    assert comment_mutations == [], (
        f"Marker-decorated Jira body must match the local plain body after "
        f"RECONCILER_MARKER strip. Got comment mutations: "
        f"{[m.comments for m in comment_mutations]}. "
        f"This is the bug 4572 mismatch: the decorated Jira echo must be "
        f"recognised as already-mirrored on the next reconciler pass."
    )
