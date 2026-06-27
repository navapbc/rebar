"""Issue-link / dependency REMOVAL must sync to Jira so the reconciler stops re-adding it.

Churn (bug wake-inn-parse): both link differs were ADD-only, so a local ``unlink`` emitted
nothing outbound while the inbound differ re-added the still-present Jira link every pass —
silently reverting the unlink. The fix makes the outbound link sync SYMMETRIC: a managed link
(in the ticket's ``managed_refs``) absent locally emits a REMOVE; a never-managed Jira link is
left for inbound ADOPT (never clobbered); and the same-pass inbound suppression drops the
re-add echo so local wins (remove-wins).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
_ENGINE = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def outbound_differ() -> ModuleType:
    return _load("outbound_differ", _ENGINE / "outbound_differ.py")


@pytest.fixture(scope="module")
def inbound_differ() -> ModuleType:
    return _load("inbound_differ", _ENGINE / "inbound_differ.py")


@pytest.fixture(scope="module")
def batch_dispatch() -> ModuleType:
    return _load("batch_dispatch", _ENGINE / "batch_dispatch.py")


class StubBindingStore:
    def __init__(self, bindings: dict[str, str]) -> None:
        self._bindings = bindings  # {local_id: jira_key}

    def get_jira_key(self, local_id: str) -> str | None:
        return self._bindings.get(local_id)

    def get_local_id(self, jira_key: str) -> str | None:
        for local_id, key in self._bindings.items():
            if key == jira_key:
                return local_id
        return None

    def is_bound(self, local_id: str) -> bool:
        return local_id in self._bindings


def _child(managed_refs, deps=None):
    return {
        "ticket_id": "local-1",
        "title": "T",
        "description": "d",
        "status": "open",
        "priority": 2,
        "ticket_type": "task",
        "assignee": "alice",
        "tags": [],
        "comments": [],
        "deps": deps or [],
        "parent_id": None,
        "managed_refs": managed_refs,
    }


def _snapshot(issuelinks):
    return {
        "PROJ-1": {
            "summary": "T",
            "description": "d",
            "issuetype": "Task",
            "priority": "Medium",
            "status": "To Do",
            "assignee": "alice",
            "labels": [],
            "issuelinks": issuelinks,
        }
    }


# A Jira "Blocks" link with PROJ-2 on the INWARD side == local "blocks" relation to local-2.
_BLOCKS_LINK = {"id": "10001", "type": {"name": "Blocks"}, "inwardIssue": {"key": "PROJ-2"}}


# ── outbound REMOVE pass ─────────────────────────────────────────────────────
def test_managed_link_absent_locally_emits_remove(outbound_differ: ModuleType) -> None:
    """A link we MANAGED (in managed_refs), present in Jira but absent from local deps, is a
    deliberate unlink — emit an outbound REMOVE so the Jira link is deleted."""
    store = StubBindingStore({"local-1": "PROJ-1", "local-2": "PROJ-2"})
    child = _child(managed_refs=[["blocks", "local-2"]], deps=[])  # managed, then unlinked
    result = outbound_differ.compute_outbound_mutations(
        local_tickets=[child], jira_snapshot=_snapshot([_BLOCKS_LINK]), binding_store=store
    )
    removes = [
        lk
        for m in result
        for lk in (m.links or [])
        if lk.get("action") == "remove" and lk.get("to_key") == "PROJ-2"
    ]
    assert removes, f"a managed, locally-removed link must emit an outbound REMOVE; got {result}"
    assert removes[0]["type"] == "Blocks"


def test_unmanaged_jira_link_is_not_removed(outbound_differ: ModuleType) -> None:
    """A Jira link we NEVER managed (absent from managed_refs — e.g. a human added it in Jira)
    must NOT be removed; it is adopted inbound instead."""
    store = StubBindingStore({"local-1": "PROJ-1", "local-2": "PROJ-2"})
    child = _child(managed_refs=[], deps=[])  # never managed this link
    result = outbound_differ.compute_outbound_mutations(
        local_tickets=[child], jira_snapshot=_snapshot([_BLOCKS_LINK]), binding_store=store
    )
    removes = [lk for m in result for lk in (m.links or []) if lk.get("action") == "remove"]
    assert not removes, f"an unmanaged Jira link must not be clobbered; got {result}"


def test_link_still_present_locally_emits_no_remove(outbound_differ: ModuleType) -> None:
    """When the link is still in local deps, it is not a removal — emit nothing for it."""
    store = StubBindingStore({"local-1": "PROJ-1", "local-2": "PROJ-2"})
    child = _child(
        managed_refs=[["blocks", "local-2"]],
        deps=[{"target_id": "local-2", "relation": "blocks", "link_uuid": "u1"}],
    )
    result = outbound_differ.compute_outbound_mutations(
        local_tickets=[child], jira_snapshot=_snapshot([_BLOCKS_LINK]), binding_store=store
    )
    removes = [lk for m in result for lk in (m.links or []) if lk.get("action") == "remove"]
    assert not removes, f"a still-linked dep must not be removed; got {result}"


# ── same-pass suppression (remove-wins) ──────────────────────────────────────
def test_outbound_remove_suppresses_inbound_link_readd(
    outbound_differ: ModuleType, inbound_differ: ModuleType
) -> None:
    """The outbound REMOVE and the inbound re-add fight in the same pass; remove-wins, so the
    inbound link-ADD for that target is suppressed and the unlink converges."""
    store = StubBindingStore({"local-1": "PROJ-1", "local-2": "PROJ-2"})
    child = _child(managed_refs=[["blocks", "local-2"]], deps=[])
    snapshot = _snapshot([_BLOCKS_LINK])

    outs = outbound_differ.compute_outbound_mutations(
        local_tickets=[child], jira_snapshot=snapshot, binding_store=store
    )
    # Without coordination the inbound differ would re-add the still-present Jira link.
    bare, _ = inbound_differ.compute_inbound_mutations(snapshot, store, {"local-1": child}, [])
    bare_link_adds = [lk for m in bare for lk in (getattr(m, "links", []) or [])]
    assert bare_link_adds, "control: inbound WOULD re-add the link without the outbound remove"

    coord, suppressed = inbound_differ.compute_inbound_mutations(
        snapshot, store, {"local-1": child}, outs
    )
    coord_link_adds = [lk for m in coord for lk in (getattr(m, "links", []) or [])]
    assert not coord_link_adds, "the inbound link re-add must be suppressed by the outbound remove"
    assert suppressed >= 1


# ── applier: resolve the link id to delete ───────────────────────────────────
def test_find_link_id_resolves_either_direction(batch_dispatch: ModuleType) -> None:
    links = [
        {"id": "55", "type": {"name": "Blocks"}, "inwardIssue": {"key": "PROJ-2"}},
        {"id": "66", "type": {"name": "Relates"}, "outwardIssue": {"key": "PROJ-3"}},
    ]
    assert batch_dispatch._find_link_id(links, "Blocks", "PROJ-2") == "55"
    assert batch_dispatch._find_link_id(links, "Relates", "PROJ-3") == "66"
    # No such link -> None (already gone == idempotent success).
    assert batch_dispatch._find_link_id(links, "Blocks", "PROJ-9") is None
    assert batch_dispatch._find_link_id([], "Blocks", "PROJ-2") is None
