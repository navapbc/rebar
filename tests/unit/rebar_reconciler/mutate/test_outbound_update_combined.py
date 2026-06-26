"""Collapsed outbound-update path — combined multi-sub-op parity + delegation.

Story D (33d0) of epic f89d. Outbound update application is now a SINGLE path:
``batch_dispatch.update_one``. The typed leaf ``_apply_outbound_update`` no longer
carries a parallel implementation — it converts the Mutation to the batch dict and
delegates to ``update_one`` (mirroring ``_apply_outbound_delete``).

This file is the parity gate for the collapse:

  * ``test_combined_all_subops_*`` drives fields + status + label add/remove +
    comments + links + parent through ``update_one`` in ONE update and asserts each
    sub-op took effect — the behavioral parity the collapse must preserve.
  * ``test_typed_leaf_delegates_to_update_one`` proves the typed leaf produces the
    SAME client effects as the batch path (one source of truth) and no longer
    returns its own sub-op counters (those move to story E).
  * the link tests retarget bug-d843's richer link-dedup behaviors (direction-
    agnostic dedup, one cached probe per loop, best-effort probe fallback) at the
    PRODUCTION path, since the typed-leaf copies were deleted.

Effects only — no mock-call-count-as-contract, no golden payloads (see
docs/adr/0004-reconciler-snapshot-contract.md).
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
_REC = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler"


def _load(name: str, path: Path) -> ModuleType:
    if name in sys.modules:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def batch() -> ModuleType:
    return _load("batch_dispatch_combined_test", _REC / "batch_dispatch.py")


@pytest.fixture(scope="module")
def applier() -> ModuleType:
    return _load("applier_combined_test", _REC / "applier.py")


class _RecordingClient:
    """Captures every sub-op the production path dispatches."""

    def __init__(self, existing_links: list | None = None) -> None:
        self.update_issue_calls: list[tuple] = []
        self.add_label_calls: list[tuple] = []
        self.remove_label_calls: list[tuple] = []
        self.add_comment_calls: list[tuple] = []
        self.set_relationship_calls: list[tuple] = []
        self.set_parent_calls: list[tuple] = []
        self.get_issue_links_calls = 0
        self._existing_links = existing_links or []

    def update_issue(self, key, **fields):
        self.update_issue_calls.append((key, fields))
        return {"status": "updated"}

    def set_parent(self, key, parent_key):
        self.set_parent_calls.append((key, parent_key))

    def add_label(self, key, label):
        self.add_label_calls.append((key, label))

    def remove_label(self, key, label):
        self.remove_label_calls.append((key, label))

    def add_comment(self, key, body):
        self.add_comment_calls.append((key, body))

    def get_issue_links(self, key):
        self.get_issue_links_calls += 1
        return list(self._existing_links)

    def set_relationship(self, from_key, to_key, link_type="Blocks"):
        self.set_relationship_calls.append((from_key, to_key, link_type))


def _issuelink(type_name, *, inward=None, outward=None):
    entry: dict = {"type": {"name": type_name}}
    if inward is not None:
        entry["inwardIssue"] = {"key": inward}
    if outward is not None:
        entry["outwardIssue"] = {"key": outward}
    return entry


def _batch_dict(key, *, fields=None, labels=None, comments=None, links=None):
    return {
        "action": "update",
        "direction": "outbound",
        "key": key,
        "fields": fields or {},
        "local_id": "",
        "follow_on": None,
        "comments": comments or [],
        "labels": labels or [],
        "links": links or [],
    }


# ---------------------------------------------------------------------------
# AC3 — one UPDATE carrying all five sub-ops, each must take effect.
# ---------------------------------------------------------------------------


def test_combined_all_subops_take_effect_through_update_one(batch) -> None:
    client = _RecordingClient(existing_links=[])
    mutation = _batch_dict(
        "DIG-1",
        # parent is carried in fields and routed to set_parent by update_one.
        fields={"summary": "new title", "status": "Done", "parent": "EPIC-9"},
        labels=[
            {"action": "add", "label": "tag-alpha"},
            {"action": "remove", "label": "stale"},
        ],
        comments=[{"body": "status note"}],
        links=[{"action": "add", "type": "Blocks", "to_key": "DIG-2", "relation": "blocks"}],
    )

    batch.update_one(mutation, client)

    # fields: scalar update with the allowlisted subset (parent stripped out).
    assert len(client.update_issue_calls) == 1
    _key, pushed = client.update_issue_calls[0]
    assert _key == "DIG-1"
    assert pushed.get("summary") == "new title" and pushed.get("status") == "Done"
    assert "parent" not in pushed, "parent must be routed via set_parent, not update_issue"
    # parent: routed to set_parent.
    assert client.set_parent_calls == [("DIG-1", "EPIC-9")]
    # labels: add + remove dispatched.
    assert client.add_label_calls == [("DIG-1", "tag-alpha")]
    assert client.remove_label_calls == [("DIG-1", "stale")]
    # comments: add_comment dispatched.
    assert client.add_comment_calls == [("DIG-1", "status note")]
    # links: set_relationship dispatched (deduped against the empty live probe).
    assert client.set_relationship_calls == [("DIG-1", "DIG-2", "Blocks")]


# ---------------------------------------------------------------------------
# Collapse contract — the typed leaf is now a thin delegator over update_one.
# ---------------------------------------------------------------------------


def test_typed_leaf_delegates_to_update_one(applier) -> None:
    """`_apply_outbound_update` produces the same client effects as the batch path
    and no longer returns its own sub-op counters (those are story E)."""
    mut_mod = applier._load_mutation_module()
    mutation = mut_mod.Mutation(
        direction=mut_mod.MutationDirection.outbound,
        action=mut_mod.MutationAction.update,
        target="DIG-7",
        payload={
            "changed_fields": {"summary": "x"},
            "labels": [{"action": "add", "label": "L"}],
            "comments": [{"body": "c"}],
            "links": [{"action": "add", "type": "Blocks", "to_key": "DIG-8"}],
        },
        provenance={"source": "test"},
    )
    client = _RecordingClient(existing_links=[])

    result = applier._apply_outbound_update(mutation, client=client)

    # Same production effects the batch path would produce.
    assert client.update_issue_calls and client.update_issue_calls[0][0] == "DIG-7"
    assert client.add_label_calls == [("DIG-7", "L")]
    assert client.add_comment_calls == [("DIG-7", "c")]
    assert client.set_relationship_calls == [("DIG-7", "DIG-8", "Blocks")]
    # The leaf no longer carries the parallel counters — single source of truth.
    assert "links_applied" not in result.payload
    assert "labels_applied" not in result.payload
    assert "update_result" in result.payload


def test_typed_leaf_stub_path_without_client(applier) -> None:
    """With no client the leaf is an inert stub (typed-dispatch coverage path)."""
    mut_mod = applier._load_mutation_module()
    mutation = mut_mod.Mutation(
        direction=mut_mod.MutationDirection.outbound,
        action=mut_mod.MutationAction.update,
        target="DIG-1",
        payload={"changed_fields": {"summary": "x"}},
        provenance={"source": "test"},
    )
    result = applier._apply_outbound_update(mutation, client=None)
    assert result.payload == {}


# ---------------------------------------------------------------------------
# Link dedup richness, retargeted at the production path (bug d843).
# ---------------------------------------------------------------------------


def test_link_dedup_is_direction_agnostic(batch) -> None:
    """A Blocks link to DIG-2 is "present" whether DIG-2 is the inward or outward
    side of the existing link — no re-add."""
    client = _RecordingClient(existing_links=[_issuelink("Blocks", inward="DIG-2")])
    mutation = _batch_dict("DIG-1", links=[{"action": "add", "type": "Blocks", "to_key": "DIG-2"}])
    batch.update_one(mutation, client)
    assert client.set_relationship_calls == []


def test_link_probe_called_once_for_multiple_adds(batch) -> None:
    """The live get_issue_links probe is cached for the whole link loop."""
    client = _RecordingClient(existing_links=[])
    mutation = _batch_dict(
        "DIG-1",
        links=[
            {"action": "add", "type": "Blocks", "to_key": "DIG-2"},
            {"action": "add", "type": "Relates", "to_key": "DIG-3"},
        ],
    )
    batch.update_one(mutation, client)
    assert client.get_issue_links_calls == 1
    assert len(client.set_relationship_calls) == 2


def test_link_probe_failure_falls_back_to_add(batch, caplog) -> None:
    """A best-effort probe error falls back to attempting the add, never blocks it."""
    client = _RecordingClient(existing_links=[])
    client.get_issue_links = lambda key: (_ for _ in ()).throw(RuntimeError("probe boom"))
    mutation = _batch_dict("DIG-1", links=[{"action": "add", "type": "Blocks", "to_key": "DIG-2"}])
    with caplog.at_level(logging.WARNING):
        batch.update_one(mutation, client)
    assert client.set_relationship_calls == [("DIG-1", "DIG-2", "Blocks")]


def test_link_write_failure_is_non_fatal(batch) -> None:
    """A set_relationship that itself RAISES is swallowed (non-fatal): the scalar
    update still succeeds and update_one returns without propagating."""
    client = _RecordingClient(existing_links=[])
    client.set_relationship = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("write boom"))
    mutation = _batch_dict(
        "DIG-1",
        fields={"summary": "still applied"},
        links=[{"action": "add", "type": "Blocks", "to_key": "DIG-2"}],
    )
    # Must not raise; the scalar field update still goes through.
    batch.update_one(mutation, client)
    assert client.update_issue_calls and client.update_issue_calls[0][0] == "DIG-1"


def test_labels_only_update_dispatches_labels(batch) -> None:
    """A labels-only update dispatches the labels through the production path.

    Documents the production contract: with no scalar fields and no parent,
    update_one still issues an (empty) update_issue edit AND dispatches the
    labels — the labels are not lost. (The old typed leaf guarded the empty
    update_issue call; the surviving batch path does not, but production already
    behaved this way.)
    """
    client = _RecordingClient(existing_links=[])
    mutation = _batch_dict("DIG-1", labels=[{"action": "add", "label": "only-label"}])
    batch.update_one(mutation, client)
    assert client.add_label_calls == [("DIG-1", "only-label")]
