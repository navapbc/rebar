"""RED tests for Fix #1: create_one labels/comments dispatch.

Historical bug (bug 85a1-f581-2252-4a21, originated PR #87e4): the
label/comment dispatch fix for outbound UPDATE was added to ``update_one``
(applier.py:1744-1779) but NOT to the symmetric CREATE leaf ``create_one``.
Phase 1 of the e2e field-validation probe consequently observed
freshly-created Jira issues with only the ``rebar-id:<local_id>`` system
label (written at applier.py:1628) — user-supplied labels and comments
from the mutation payload were silently dropped.

Mutation payload shape for CREATE (per _mutation_to_batch_dict applier.py:2376-2390
and reconcile.py:592-603): ``labels`` and ``comments`` survive as
top-level keys on the batch dict.

This RED test asserts that, after a successful create, ``client.add_label``
is called for each ``{action: "add", label: X}`` entry and ``client.add_comment``
is called for each ``{body: Y}`` entry — in addition to the existing
``rebar-id:<local_id>`` identity label write.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
APPLIER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"


def _load_applier():
    spec = importlib.util.spec_from_file_location("applier_create_labels_comments", APPLIER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["applier_create_labels_comments"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def applier():
    if not APPLIER_PATH.exists():
        pytest.fail(f"applier.py not found at {APPLIER_PATH}")
    return _load_applier()


def _make_mock_client(create_return=None):
    client = MagicMock()
    client.search_issues.return_value = []  # JQL miss — proceed to create
    client.create_issue.return_value = (
        create_return if create_return is not None else {"key": "DIG-999"}
    )
    return client


def _make_create_mutation_with_labels_and_comments(
    local_id: str = "tick-fixt1",
    labels: list[dict] | None = None,
    comments: list[dict] | None = None,
) -> dict:
    return {
        "action": "create",
        "local_id": local_id,
        "fields": {"summary": f"Reconcile {local_id}", "issuetype": {"name": "Task"}},
        "labels": labels or [],
        "comments": comments or [],
    }


def test_create_one_dispatches_user_supplied_labels(applier):
    """When mutation has labels=[{action: add, label: X}, ...], add_label is called for each X."""
    local_id = "tick-lbl1"
    client = _make_mock_client(create_return={"key": "DIG-111"})
    mutation = _make_create_mutation_with_labels_and_comments(
        local_id=local_id,
        labels=[
            {"action": "add", "label": "label-a"},
            {"action": "add", "label": "label-b"},
        ],
    )
    applier.create_one(mutation, client, rest_calls=0)

    # add_label must be called for each user label PLUS the rebar-id system label.
    actual_calls = [c.args for c in client.add_label.call_args_list]
    assert ("DIG-111", "label-a") in actual_calls, (
        f"user label 'label-a' was not propagated to add_label; calls: {actual_calls!r}"
    )
    assert ("DIG-111", "label-b") in actual_calls, (
        f"user label 'label-b' was not propagated to add_label; calls: {actual_calls!r}"
    )
    assert ("DIG-111", f"rebar-id:{local_id}") in actual_calls, (
        f"rebar-id system label still required; calls: {actual_calls!r}"
    )


def test_create_one_dispatches_user_supplied_comments(applier):
    """When mutation has comments=[{body: Y}, ...], add_comment is called for each Y."""
    local_id = "tick-cmt1"
    client = _make_mock_client(create_return={"key": "DIG-222"})
    mutation = _make_create_mutation_with_labels_and_comments(
        local_id=local_id,
        comments=[{"body": "First probe comment"}, {"body": "Second comment"}],
    )
    applier.create_one(mutation, client, rest_calls=0)

    actual_bodies = [c.args[1] for c in client.add_comment.call_args_list]
    assert "First probe comment" in actual_bodies, (
        f"first user comment was not propagated to add_comment; bodies: {actual_bodies!r}"
    )
    assert "Second comment" in actual_bodies, (
        f"second user comment was not propagated to add_comment; bodies: {actual_bodies!r}"
    )


def test_create_one_skips_remove_action_labels(applier):
    """remove-action entries are no-ops at CREATE time (issue has no preexisting labels)."""
    local_id = "tick-lbl2"
    client = _make_mock_client(create_return={"key": "DIG-333"})
    mutation = _make_create_mutation_with_labels_and_comments(
        local_id=local_id,
        labels=[{"action": "remove", "label": "stale-label"}],
    )
    applier.create_one(mutation, client, rest_calls=0)

    # remove_label should not be called for CREATE — there's no prior state to remove from.
    client.remove_label.assert_not_called()


def test_create_one_label_failure_does_not_abort_create(applier):
    """A label dispatch failure logs but does not abort the create or rollback the issue."""
    local_id = "tick-lbl3"
    client = _make_mock_client(create_return={"key": "DIG-444"})
    # First add_label = system rebar-id label (succeeds);
    # subsequent calls = user labels (fail). Order is rebar-id first per the
    # existing identity-write block at applier.py:1628.
    client.add_label.side_effect = [None, RuntimeError("label dispatch failed")]
    mutation = _make_create_mutation_with_labels_and_comments(
        local_id=local_id,
        labels=[{"action": "add", "label": "user-label"}],
    )
    # Should not raise; the create succeeded, label dispatch is best-effort.
    result = applier.create_one(mutation, client, rest_calls=0)
    assert result is not None
    assert result.get("key") == "DIG-444"
    # delete_issue must NOT be called — label-dispatch failure is post-identity-write
    # best-effort, not a rollback trigger.
    client.delete_issue.assert_not_called()


def test_create_one_no_labels_or_comments_unchanged_behavior(applier):
    """When mutation lacks labels/comments keys, behavior matches the existing identity-write contract."""
    local_id = "tick-bare"
    client = _make_mock_client(create_return={"key": "DIG-555"})
    # Bare mutation — no labels/comments keys (covers the legacy mutation shape).
    mutation = {
        "action": "create",
        "local_id": local_id,
        "fields": {"summary": "bare", "issuetype": {"name": "Task"}},
    }
    applier.create_one(mutation, client, rest_calls=0)
    # Only rebar-id system label written.
    client.add_label.assert_called_once_with("DIG-555", f"rebar-id:{local_id}")
    client.add_comment.assert_not_called()
