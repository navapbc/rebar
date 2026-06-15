"""Tests for the Cycle 3 outbound link-apply path in _apply_outbound_update.

Behavior under test:
  - payload['links'] entries with action='add' are dispatched via
    client.set_relationship(mutation.target, to_key, type), routed through
    _call_with_retry (mirroring the label/comment dispatch pattern).
  - A link-only update (empty changed_fields/labels/comments, non-empty links)
    is NOT treated as a no-op: no warning, and links_applied is counted.
  - set_relationship failures are non-fatal: surfaced in result.payload
    ['link_errors'] and logged, but the apply still returns a result.

These exercise the MOCK-client path (no live Jira); mirrors
test_applier_outbound_update.py's SimpleNamespace/MagicMock approach.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
APPLIER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"


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


def _make_outbound_update_mutation_with_payload(applier_mod, payload, target="DIG-1"):
    mut_mod = applier_mod._load_mutation_module()
    return mut_mod.Mutation(
        direction=mut_mod.MutationDirection.outbound,
        action=mut_mod.MutationAction.update,
        target=target,
        payload=payload,
        provenance={"source": "test"},
    )


def _client():
    return SimpleNamespace(
        update_issue=MagicMock(return_value=None),
        add_label=MagicMock(),
        remove_label=MagicMock(),
        add_comment=MagicMock(),
        set_relationship=MagicMock(return_value=None),
    )


def test_link_add_dispatched_via_set_relationship(applier):
    """payload['links'] add entry → client.set_relationship(target, to_key, type)."""
    client = _client()
    mutation = _make_outbound_update_mutation_with_payload(
        applier,
        {
            "changed_fields": {},
            "labels": [],
            "comments": [],
            "links": [
                {
                    "action": "add",
                    "type": "Blocks",
                    "to_key": "DIG-2",
                    "relation": "blocks",
                    "link_uuid": "u-1",
                }
            ],
        },
    )

    result = applier._apply_outbound_update(mutation, client=client)

    set_rel_calls = [c.args for c in client.set_relationship.call_args_list]
    assert ("DIG-1", "DIG-2", "Blocks") in set_rel_calls, (
        f"set_relationship not called for the link add. Calls: {set_rel_calls}"
    )
    assert result.payload["links_applied"] == 1
    # Link-only update: no scalar/label/comment side-effects.
    assert client.update_issue.call_count == 0


def test_link_only_update_is_not_a_noop(applier, caplog):
    """A mutation carrying only links must NOT emit the no-op warning."""
    client = _client()
    mutation = _make_outbound_update_mutation_with_payload(
        applier,
        {
            "changed_fields": {},
            "labels": [],
            "comments": [],
            "links": [{"action": "add", "type": "Relates", "to_key": "DIG-9"}],
        },
    )

    with caplog.at_level(logging.WARNING):
        result = applier._apply_outbound_update(mutation, client=client)

    noop_warnings = [r for r in caplog.records if "no-op" in r.message]
    assert not noop_warnings, (
        f"Unexpected no-op warning when links are present: {[r.message for r in noop_warnings]}"
    )
    assert result.payload["links_applied"] == 1


def test_link_add_routed_through_call_with_retry(applier):
    """The link add must go through _call_with_retry (same retry/error path
    as labels/comments)."""
    client = _client()
    mutation = _make_outbound_update_mutation_with_payload(
        applier,
        {
            "changed_fields": {},
            "labels": [],
            "comments": [],
            "links": [{"action": "add", "type": "Blocks", "to_key": "DIG-2"}],
        },
    )

    captured: list[tuple] = []
    from rebar_reconciler import apply_outbound

    real = apply_outbound._call_with_retry

    def spy(fn, *args, **kwargs):
        captured.append((fn, args, kwargs))
        return real(fn, *args, **kwargs)

    with patch.object(apply_outbound, "_call_with_retry", side_effect=spy):
        applier._apply_outbound_update(mutation, client=client)

    rel_calls = [c for c in captured if c[0] is client.set_relationship]
    assert len(rel_calls) == 1, f"expected 1 set_relationship via retry, got {len(rel_calls)}"
    _, args, _ = rel_calls[0]
    assert args == ("DIG-1", "DIG-2", "Blocks")


def test_link_failure_is_non_fatal_and_surfaced(applier, caplog):
    """A set_relationship failure is logged + surfaced in result['link_errors']
    but does not raise."""
    client = _client()
    client.set_relationship = MagicMock(side_effect=RuntimeError("boom"))
    mutation = _make_outbound_update_mutation_with_payload(
        applier,
        {
            "changed_fields": {},
            "labels": [],
            "comments": [],
            "links": [{"action": "add", "type": "Blocks", "to_key": "DIG-2"}],
        },
    )

    with caplog.at_level(logging.WARNING):
        result = applier._apply_outbound_update(mutation, client=client)

    assert result.payload["links_applied"] == 0
    assert "link_errors" in result.payload
    assert any("set_relationship failed" in e for e in result.payload["link_errors"])


def test_link_entry_missing_keys_is_skipped(applier):
    """Entries without type/to_key, or with action != add, are skipped."""
    client = _client()
    mutation = _make_outbound_update_mutation_with_payload(
        applier,
        {
            "changed_fields": {},
            "labels": [],
            "comments": [],
            "links": [
                {"action": "add", "type": "Blocks"},  # missing to_key
                {"action": "add", "to_key": "DIG-2"},  # missing type
                {"action": "remove", "type": "Blocks", "to_key": "DIG-3"},  # not add
                "not-a-dict",
            ],
        },
    )

    result = applier._apply_outbound_update(mutation, client=client)

    assert client.set_relationship.call_count == 0
    assert result.payload["links_applied"] == 0
