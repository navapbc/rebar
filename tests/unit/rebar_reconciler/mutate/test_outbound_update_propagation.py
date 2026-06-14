"""Regression test for Bug 87e4: outbound UPDATE doesn't propagate to Jira.

Bug summary:
  After local edits to bound tickets, running the reconciler reports OK and
  emits no errors, but the Jira issues retain their original CREATE-time
  values. None of the local field changes propagate.

Two coordinated root causes:
  1. KEY MISMATCH — `reconcile.py` builds outbound update mutations with
     `payload["changed_fields"]`. `applier._mutation_to_batch_dict` reads
     `payload.get("fields", payload)`. The fallback returns the WHOLE
     payload dict (including `comments`, `labels`, `changed_fields` keys),
     which `update_one` then unpacks via `**fields` to
     `client.update_issue(**fields)`. Jira silently ignores the bogus
     `changed_fields=`, `comments=`, `labels=` kwargs and applies nothing.
  2. NO LABELS/COMMENTS PROPAGATION — `update_one` only calls
     `client.update_issue`. It never calls `add_label`, `remove_label`,
     or `add_comment`. So even if the payload mapping were correct, label
     and comment changes wouldn't propagate from the legacy batch path.

Fix:
  1. `_mutation_to_batch_dict` reads `payload.get("changed_fields", payload.get("fields", {}))`
     and ALSO surfaces `comments` and `labels` keys in the returned dict.
  2. `update_one` iterates `mutation.get("labels", [])` calling
     `add_label`/`remove_label` per action, and iterates
     `mutation.get("comments", [])` calling `add_comment`. Uses
     `_call_with_retry` to match existing resilience.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
APPLIER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"


def _load_applier():
    spec = importlib.util.spec_from_file_location(
        "applier_outbound_update_propagation", APPLIER_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["applier_outbound_update_propagation"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def applier():
    return _load_applier()


def test_mutation_to_batch_dict_maps_changed_fields_key(applier):
    """`_mutation_to_batch_dict` must read `payload["changed_fields"]`, not
    `payload["fields"]`. The new outbound differ pipeline (reconcile.py)
    builds typed Mutations with the `changed_fields` key. The previous
    implementation read `"fields"` and fell back to the whole payload —
    causing `**fields` to unpack `changed_fields=`, `comments=`, `labels=`
    as bogus kwargs to `client.update_issue`."""
    mut_mod = applier._load_mutation_module()
    mutation = mut_mod.Mutation(
        direction=mut_mod.MutationDirection.outbound,
        action=mut_mod.MutationAction.update,
        target="DIG-100",
        payload={
            "changed_fields": {"summary": "new title", "priority": "Low"},
            "comments": [{"body": "new comment"}],
            "labels": [{"action": "add", "label": "label-x"}],
        },
        provenance={"source": "test"},
    )

    result = applier._mutation_to_batch_dict(mutation)

    # The batch dict's "fields" key must contain the actual field changes,
    # NOT the whole payload (which would leak comments/labels keys into
    # update_issue's kwargs).
    assert result["fields"] == {"summary": "new title", "priority": "Low"}, (
        f"_mutation_to_batch_dict leaked the whole payload as fields: "
        f"got {result['fields']!r}. This is the bug — when update_one then "
        f"calls client.update_issue(**fields), it passes the bogus kwargs "
        f"changed_fields=, comments=, labels= which Jira silently ignores."
    )

    # The batch dict must also carry forward comments and labels so
    # update_one can dispatch them via add_label/add_comment.
    assert result["comments"] == [{"body": "new comment"}]
    assert result["labels"] == [{"action": "add", "label": "label-x"}]


def test_update_one_propagates_label_additions(applier):
    """`update_one` must iterate `mutation.get("labels", [])` and call
    `client.add_label` for each entry with action="add"."""
    client = SimpleNamespace(
        update_issue=MagicMock(return_value={}),
        add_label=MagicMock(),
        remove_label=MagicMock(),
        add_comment=MagicMock(),
    )
    mutation = {
        "key": "DIG-100",
        "action": "update",
        "fields": {},
        "labels": [
            {"action": "add", "label": "label-x"},
            {"action": "add", "label": "label-y"},
        ],
        "comments": [],
    }

    applier.update_one(mutation, client)

    # Both labels must reach add_label.
    add_label_calls = [c.args for c in client.add_label.call_args_list]
    assert ("DIG-100", "label-x") in add_label_calls, (
        f"update_one did not call client.add_label('DIG-100', 'label-x'). "
        f"Actual calls: {add_label_calls}. After the fix, labels in the "
        f"mutation payload must propagate via add_label/remove_label."
    )
    assert ("DIG-100", "label-y") in add_label_calls


def test_update_one_propagates_label_removals(applier):
    """`update_one` must call `client.remove_label` for each label entry
    with action="remove"."""
    client = SimpleNamespace(
        update_issue=MagicMock(return_value={}),
        add_label=MagicMock(),
        remove_label=MagicMock(),
        add_comment=MagicMock(),
    )
    mutation = {
        "key": "DIG-100",
        "action": "update",
        "fields": {},
        "labels": [{"action": "remove", "label": "label-a"}],
        "comments": [],
    }

    applier.update_one(mutation, client)

    remove_label_calls = [c.args for c in client.remove_label.call_args_list]
    assert ("DIG-100", "label-a") in remove_label_calls, (
        f"update_one did not call client.remove_label('DIG-100', 'label-a'). "
        f"Actual calls: {remove_label_calls}."
    )


def test_update_one_propagates_comments(applier):
    """`update_one` must iterate `mutation.get("comments", [])` and call
    `client.add_comment` for each entry's body."""
    client = SimpleNamespace(
        update_issue=MagicMock(return_value={}),
        add_label=MagicMock(),
        remove_label=MagicMock(),
        add_comment=MagicMock(),
    )
    mutation = {
        "key": "DIG-100",
        "action": "update",
        "fields": {},
        "labels": [],
        "comments": [{"body": "new comment"}, {"body": "another one"}],
    }

    applier.update_one(mutation, client)

    add_comment_calls = [c.args for c in client.add_comment.call_args_list]
    assert ("DIG-100", "new comment") in add_comment_calls, (
        f"update_one did not call client.add_comment('DIG-100', 'new comment'). "
        f"Actual calls: {add_comment_calls}."
    )
    assert ("DIG-100", "another one") in add_comment_calls


def test_mutation_to_batch_dict_extracts_create_fields_from_payload_top_level(applier):
    """For outbound CREATE mutations, reconcile.py stores fields at the TOP
    LEVEL of the payload alongside bookkeeping keys (local_id, comments,
    labels). `_mutation_to_batch_dict` must extract the create fields by
    stripping bookkeeping keys — NOT pull from a nested `fields` or
    `changed_fields` key (which doesn't exist for create).

    Regression for the 'title/summary is empty' error introduced when
    fixing the UPDATE path with an empty-dict default."""
    mut_mod = applier._load_mutation_module()
    mutation = mut_mod.Mutation(
        direction=mut_mod.MutationDirection.outbound,
        action=mut_mod.MutationAction.create,
        target="local-id-1",
        payload={
            # Create fields at top level (matches reconcile.py:525-535).
            "summary": "New issue",
            "description": "desc",
            "issuetype": "Task",
            "priority": "Medium",
            # Bookkeeping keys that must NOT leak into fields.
            "local_id": "local-id-1",
            "comments": [],
            "labels": [{"action": "add", "label": "x"}],
        },
        provenance={"source": "test"},
    )

    result = applier._mutation_to_batch_dict(mutation)

    # All create fields must be in result["fields"].
    assert result["fields"]["summary"] == "New issue"
    assert result["fields"]["description"] == "desc"
    assert result["fields"]["issuetype"] == "Task"
    assert result["fields"]["priority"] == "Medium"
    # Bookkeeping keys must NOT leak.
    assert "local_id" not in result["fields"]
    assert "comments" not in result["fields"]
    assert "labels" not in result["fields"]


def test_update_one_does_not_pass_bogus_kwargs_to_update_issue(applier):
    """`update_one` must not forward `comments`, `labels`, or
    `changed_fields` keys to `client.update_issue` — those are handled by
    separate methods (add_comment/add_label) and would be silently dropped
    by Jira if passed as edit fields."""
    client = SimpleNamespace(
        update_issue=MagicMock(return_value={}),
        add_label=MagicMock(),
        remove_label=MagicMock(),
        add_comment=MagicMock(),
    )
    mutation = {
        "key": "DIG-100",
        "action": "update",
        "fields": {"summary": "new title"},
        "labels": [{"action": "add", "label": "x"}],
        "comments": [{"body": "x"}],
    }

    applier.update_one(mutation, client)

    # update_issue must be called with summary only, NOT with comments/labels
    # leaked through.
    assert client.update_issue.call_count == 1
    _, kwargs = client.update_issue.call_args
    assert "comments" not in kwargs, (
        f"client.update_issue received bogus 'comments' kwarg: {kwargs!r}"
    )
    assert "labels" not in kwargs, f"client.update_issue received bogus 'labels' kwarg: {kwargs!r}"
    assert "changed_fields" not in kwargs, (
        f"client.update_issue received bogus 'changed_fields' kwarg: {kwargs!r}"
    )
    assert kwargs == {"summary": "new title"}
