"""Tests for the _apply_outbound_update v1 leaf in rebar_reconciler/applier.py.

Behavior under test:
  - Allowlisted fields (summary, description, assignee, priority, status) are
    pushed via client.update_issue, routed through _call_with_retry.
  - Non-allowlisted fields are silently dropped — zero side-effects on those.
  - Status is first-class (DSO_RECONCILER_STATUS_GATING gate removed in bug 85a1
    Gap 8): status flows through client.update_issue without raising.
  - payload['labels'] dispatch: add_label / remove_label per entry (gap fix for
    bugs 3b5f / 85a1 — previously silently dropped).
  - payload['comments'] dispatch: add_comment per entry (same gap fix).
  - ApplyResult payload includes fields_pushed, labels_applied, comments_applied.
  - Empty effective work set (no fields, labels, or comments) emits a WARNING.

NOTE: The outbound differ emits Jira-side field names (e.g. "summary" not
"title"). Tests use "summary" to match the real mutation payloads.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
APPLIER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"
)


def _load_applier():
    spec = importlib.util.spec_from_file_location("applier", APPLIER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["applier"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def applier():
    return _load_applier()


def _make_outbound_update_mutation(applier_mod, changed_fields):
    mut_mod = applier_mod._load_mutation_module()
    return mut_mod.Mutation(
        direction=mut_mod.MutationDirection.outbound,
        action=mut_mod.MutationAction.update,
        target="PROJ-200",
        payload={"changed_fields": changed_fields},
        provenance={"source": "test"},
    )


def test_allowlist_fields_routed_via_call_with_retry(applier):
    """Allowlisted fields are pushed via _call_with_retry → client.update_issue."""
    client = SimpleNamespace(update_issue=MagicMock(return_value=None))
    mutation = _make_outbound_update_mutation(
        applier, {"summary": "new title", "description": "new desc"}
    )

    captured: list[tuple] = []
    real = applier._call_with_retry

    def spy(fn, *args, **kwargs):
        captured.append((fn, args, kwargs))
        return real(fn, *args, **kwargs)

    with patch.object(applier, "_call_with_retry", side_effect=spy):
        result = applier._apply_outbound_update(mutation, client=client)

    # Verify update_issue invoked exactly once with the allowlisted subset.
    update_calls = [c for c in captured if c[0] is client.update_issue]
    assert len(update_calls) == 1, f"expected 1 update_issue call, got {len(update_calls)}"
    _, args, kwargs = update_calls[0]
    assert args == ("PROJ-200",)
    assert set(kwargs.keys()) == {"summary", "description"}
    assert kwargs["summary"] == "new title"
    assert kwargs["description"] == "new desc"

    # ApplyResult reports which fields were pushed (plus labels/comments counters).
    assert result.payload["fields_pushed"] == ["description", "summary"]
    assert result.payload["labels_applied"] == []
    assert result.payload["comments_applied"] == 0


def test_non_allowlist_fields_silently_dropped(applier):
    """Fields outside the allowlist are dropped — no client call when the
    filtered set is empty."""
    client = SimpleNamespace(
        update_issue=MagicMock(return_value=None),
        add_label=MagicMock(),
        remove_label=MagicMock(),
        add_comment=MagicMock(),
    )
    mutation = _make_outbound_update_mutation(
        applier, {"custom_field": "x"}
    )

    result = applier._apply_outbound_update(mutation, client=client)

    assert client.update_issue.call_count == 0
    assert result.payload["fields_pushed"] == []
    assert result.payload["labels_applied"] == []
    assert result.payload["comments_applied"] == 0


def test_status_field_is_forwarded_to_update_issue(applier, monkeypatch):
    """Bug 85a1 (Gap 8): the DSO_RECONCILER_STATUS_GATING gate has been
    removed. Status is now first-class — ``_apply_outbound_update`` passes
    it through to ``client.update_issue`` (which routes status to
    ``transition_issue`` → REST POST /transitions inside acli-integration).
    """
    monkeypatch.delenv("DSO_RECONCILER_STATUS_GATING", raising=False)
    client = SimpleNamespace(update_issue=MagicMock(return_value=None))
    mutation = _make_outbound_update_mutation(
        applier, {"status": "Done", "summary": "x"}
    )

    applier._apply_outbound_update(mutation, client=client)

    # update_issue called with BOTH summary and status — status is no longer
    # stripped, and no StatusMappingError is raised.
    assert client.update_issue.call_count == 1
    args, kwargs = client.update_issue.call_args
    assert args == ("PROJ-200",)
    assert kwargs.get("summary") == "x"
    assert kwargs.get("status") == "Done"


def test_status_only_payload_still_pushes(applier, monkeypatch):
    """A mutation whose only changed field is status reaches client.update_issue.

    Previously the gating prevented any update_issue call when status was the
    sole field; the new contract pushes it.
    """
    monkeypatch.delenv("DSO_RECONCILER_STATUS_GATING", raising=False)
    client = SimpleNamespace(
        update_issue=MagicMock(return_value=None),
        add_label=MagicMock(),
        remove_label=MagicMock(),
        add_comment=MagicMock(),
    )
    mutation = _make_outbound_update_mutation(applier, {"status": "Done"})

    result = applier._apply_outbound_update(mutation, client=client)

    assert client.update_issue.call_count == 1
    _, kwargs = client.update_issue.call_args
    assert kwargs == {"status": "Done"}
    assert result.payload["fields_pushed"] == ["status"]
    assert result.payload["labels_applied"] == []
    assert result.payload["comments_applied"] == 0


# ---------------------------------------------------------------------------
# New tests: typed-leaf gap fix (bugs 3b5f / 85a1) — labels and comments
# ---------------------------------------------------------------------------


def _make_outbound_update_mutation_with_payload(applier_mod, payload):
    """Build a typed outbound update Mutation with an arbitrary payload dict."""
    mut_mod = applier_mod._load_mutation_module()
    return mut_mod.Mutation(
        direction=mut_mod.MutationDirection.outbound,
        action=mut_mod.MutationAction.update,
        target="DIG-42",
        payload=payload,
        provenance={"source": "test"},
    )


def test_typed_leaf_propagates_label_additions(applier):
    """_apply_outbound_update must dispatch add_label for payload['labels']
    entries with action='add'.  Previously this was silently dropped, causing
    label changes to be lost when the typed single-mutation dispatch path was
    used (bugs 3b5f / 85a1)."""
    client = SimpleNamespace(
        update_issue=MagicMock(return_value=None),
        add_label=MagicMock(),
        remove_label=MagicMock(),
        add_comment=MagicMock(),
    )
    mutation = _make_outbound_update_mutation_with_payload(
        applier,
        {
            "changed_fields": {},
            "labels": [
                {"action": "add", "label": "tag-alpha"},
                {"action": "add", "label": "tag-beta"},
            ],
            "comments": [],
        },
    )

    result = applier._apply_outbound_update(mutation, client=client)

    add_label_calls = [c.args for c in client.add_label.call_args_list]
    assert ("DIG-42", "tag-alpha") in add_label_calls, (
        f"add_label not called for 'tag-alpha'. Calls: {add_label_calls}"
    )
    assert ("DIG-42", "tag-beta") in add_label_calls
    assert result.payload["labels_applied"] == ["+tag-alpha", "+tag-beta"]
    # No scalar field changes → update_issue must not be called.
    assert client.update_issue.call_count == 0


def test_typed_leaf_propagates_label_removals(applier):
    """_apply_outbound_update must dispatch remove_label for payload['labels']
    entries with action='remove'."""
    client = SimpleNamespace(
        update_issue=MagicMock(return_value=None),
        add_label=MagicMock(),
        remove_label=MagicMock(),
        add_comment=MagicMock(),
    )
    mutation = _make_outbound_update_mutation_with_payload(
        applier,
        {
            "changed_fields": {},
            "labels": [{"action": "remove", "label": "stale-tag"}],
            "comments": [],
        },
    )

    result = applier._apply_outbound_update(mutation, client=client)

    remove_label_calls = [c.args for c in client.remove_label.call_args_list]
    assert ("DIG-42", "stale-tag") in remove_label_calls, (
        f"remove_label not called for 'stale-tag'. Calls: {remove_label_calls}"
    )
    assert result.payload["labels_applied"] == ["-stale-tag"]


def test_typed_leaf_propagates_comments(applier):
    """_apply_outbound_update must dispatch add_comment for each payload['comments']
    entry.  Previously this was silently dropped (bugs 3b5f / 85a1)."""
    client = SimpleNamespace(
        update_issue=MagicMock(return_value=None),
        add_label=MagicMock(),
        remove_label=MagicMock(),
        add_comment=MagicMock(),
    )
    mutation = _make_outbound_update_mutation_with_payload(
        applier,
        {
            "changed_fields": {},
            "labels": [],
            "comments": [{"body": "first comment"}, {"body": "second comment"}],
        },
    )

    result = applier._apply_outbound_update(mutation, client=client)

    add_comment_calls = [c.args for c in client.add_comment.call_args_list]
    assert ("DIG-42", "first comment") in add_comment_calls, (
        f"add_comment not called for 'first comment'. Calls: {add_comment_calls}"
    )
    assert ("DIG-42", "second comment") in add_comment_calls
    assert result.payload["comments_applied"] == 2


def test_typed_leaf_labels_and_comments_alongside_scalar_fields(applier):
    """When the mutation has both scalar field changes AND labels/comments, all
    three must be dispatched: update_issue for scalars, add_label for labels,
    add_comment for comments."""
    client = SimpleNamespace(
        update_issue=MagicMock(return_value=None),
        add_label=MagicMock(),
        remove_label=MagicMock(),
        add_comment=MagicMock(),
    )
    mutation = _make_outbound_update_mutation_with_payload(
        applier,
        {
            "changed_fields": {"summary": "updated title"},
            "labels": [{"action": "add", "label": "sprint-42"}],
            "comments": [{"body": "status update"}],
        },
    )

    result = applier._apply_outbound_update(mutation, client=client)

    assert client.update_issue.call_count == 1
    _, kwargs = client.update_issue.call_args
    assert kwargs.get("summary") == "updated title"

    add_label_calls = [c.args for c in client.add_label.call_args_list]
    assert ("DIG-42", "sprint-42") in add_label_calls

    add_comment_calls = [c.args for c in client.add_comment.call_args_list]
    assert ("DIG-42", "status update") in add_comment_calls

    assert result.payload["fields_pushed"] == ["summary"]
    assert result.payload["labels_applied"] == ["+sprint-42"]
    assert result.payload["comments_applied"] == 1


def test_empty_changed_fields_with_only_labels_no_noop_warning(applier, caplog):
    """A mutation with empty changed_fields but non-empty labels
    must NOT emit the no-op warning — the work set is non-empty."""
    import logging
    client = SimpleNamespace(
        update_issue=MagicMock(return_value=None),
        add_label=MagicMock(),
        remove_label=MagicMock(),
        add_comment=MagicMock(),
    )
    mutation = _make_outbound_update_mutation_with_payload(
        applier,
        {
            "changed_fields": {},
            "labels": [{"action": "add", "label": "new-label"}],
            "comments": [],
        },
    )

    with caplog.at_level(logging.WARNING):
        result = applier._apply_outbound_update(mutation, client=client)

    noop_warnings = [r for r in caplog.records if "no-op" in r.message]
    assert not noop_warnings, (
        f"Unexpected no-op warning when labels are present: {[r.message for r in noop_warnings]}"
    )
    assert result.payload["labels_applied"] == ["+new-label"]


def test_truly_empty_mutation_emits_loud_warning(applier, caplog):
    """When changed_fields, labels, and comments are all empty, a WARNING must
    be emitted so operators can detect misconfigured mutations instead of
    receiving a silent success."""
    import logging
    client = SimpleNamespace(
        update_issue=MagicMock(return_value=None),
        add_label=MagicMock(),
        remove_label=MagicMock(),
        add_comment=MagicMock(),
    )
    mutation = _make_outbound_update_mutation_with_payload(
        applier,
        {
            "changed_fields": {},
            "labels": [],
            "comments": [],
        },
    )

    with caplog.at_level(logging.WARNING):
        result = applier._apply_outbound_update(mutation, client=client)

    noop_warnings = [r for r in caplog.records if "no-op" in r.message]
    assert noop_warnings, (
        "Expected a WARNING about empty no-op mutation but none was emitted. "
        "This guard prevents silent success when mutations carry no actual work."
    )
    assert result.payload["fields_pushed"] == []
    assert result.payload["labels_applied"] == []
    assert result.payload["comments_applied"] == 0
