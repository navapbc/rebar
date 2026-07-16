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

    def get_baseline(self, local_id):
        # story d6bd: baseline arbitration is always-on; unset -> None (local-wins).
        return None

    def is_pending(self, local_id):
        return False

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
    result, _ = outbound_differ.compute_outbound_mutations(
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


_OUT_BLOCKS_LINK = {"id": "10002", "type": {"name": "Blocks"}, "outwardIssue": {"key": "PROJ-2"}}


def test_managed_outward_blocks_removed_maps_to_depends_on(outbound_differ: ModuleType) -> None:
    """The REMOVE path carries its OWN inward/outward direction logic (a copy of the ADD path's).
    A managed `depends_on` is stored in Jira as an OUTWARD `Blocks` (B blocks A == A depends_on B);
    when unlinked locally it must map back to `depends_on`, not `blocks`. This is the sibling of the
    c8ed ADD-path swap bug — the outward branch had no coverage before this."""
    store = StubBindingStore({"local-1": "PROJ-1", "local-2": "PROJ-2"})
    child = _child(managed_refs=[["depends_on", "local-2"]], deps=[])  # managed, then unlinked
    result, _ = outbound_differ.compute_outbound_mutations(
        local_tickets=[child], jira_snapshot=_snapshot([_OUT_BLOCKS_LINK]), binding_store=store
    )
    removes = [
        lk
        for m in result
        for lk in (m.links or [])
        if lk.get("action") == "remove" and lk.get("to_key") == "PROJ-2"
    ]
    assert removes, f"managed outward-Blocks unlinked locally must emit a REMOVE; got {result}"
    assert removes[0]["type"] == "Blocks"
    assert removes[0]["relation"] == "depends_on", (
        f"outward Blocks must map back to depends_on (not blocks); got {removes[0]}"
    )


def test_unmanaged_jira_link_is_not_removed(outbound_differ: ModuleType) -> None:
    """A Jira link we NEVER managed (absent from managed_refs — e.g. a human added it in Jira)
    must NOT be removed; it is adopted inbound instead."""
    store = StubBindingStore({"local-1": "PROJ-1", "local-2": "PROJ-2"})
    child = _child(managed_refs=[], deps=[])  # never managed this link
    result, _ = outbound_differ.compute_outbound_mutations(
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
    result, _ = outbound_differ.compute_outbound_mutations(
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

    outs, _ = outbound_differ.compute_outbound_mutations(
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


class _FakeClient:
    def __init__(self, links, delete_exc=None) -> None:
        self._links = links
        self._delete_exc = delete_exc
        self.deleted: list[str] = []

    def update_issue(self, key, **kwargs):
        return {"key": key}

    def get_issue_links(self, key):
        return self._links

    def delete_issue_link(self, link_id):
        if self._delete_exc is not None:
            raise self._delete_exc
        self.deleted.append(link_id)
        return {"status": "deleted", "link_id": link_id}


def test_update_one_applies_link_remove(batch_dispatch: ModuleType) -> None:
    """update_one resolves the link id and deletes it for a REMOVE mutation."""
    client = _FakeClient(
        [{"id": "77", "type": {"name": "Blocks"}, "inwardIssue": {"key": "PROJ-2"}}]
    )
    mutation = {
        "key": "PROJ-1",
        "fields": {},
        "links": [{"action": "remove", "type": "Blocks", "to_key": "PROJ-2"}],
    }
    subop: dict[str, int] = {}
    batch_dispatch.update_one(mutation, client, subop_applied=subop)
    assert client.deleted == ["77"]
    assert subop.get("links_applied") == 1


def test_update_one_tolerates_acli_delete_failure(batch_dispatch: ModuleType) -> None:
    """delete_issue_link shells out via ACLI (subprocess.CalledProcessError, NOT HTTPError).
    A failure after the link was found in the probe (concurrent removal / transient) must be
    idempotent — update_one does not raise and the pass is not unwound."""
    import subprocess

    exc = subprocess.CalledProcessError(1, ["jira", "workitem", "link", "delete"])
    client = _FakeClient(
        [{"id": "88", "type": {"name": "Blocks"}, "inwardIssue": {"key": "PROJ-2"}}],
        delete_exc=exc,
    )
    mutation = {
        "key": "PROJ-1",
        "fields": {},
        "links": [{"action": "remove", "type": "Blocks", "to_key": "PROJ-2"}],
    }
    subop: dict[str, int] = {}
    # Must NOT raise (the CalledProcessError is caught and treated as idempotent).
    batch_dispatch.update_one(mutation, client, subop_applied=subop)
    assert subop.get("links_applied") == 1, "an ACLI delete race is idempotent success"


class _MutableJira:
    """A minimal mutable Jira that reflects link deletes on subsequent reads — enough to
    drive TWO reconcile passes over the differ + applier and prove the unlink converges."""

    def __init__(self, links_by_key: dict[str, list[dict]]) -> None:
        self._links = {k: list(v) for k, v in links_by_key.items()}

    def issuelinks(self, key: str) -> list[dict]:
        return list(self._links.get(key, []))

    def update_issue(self, key, **kwargs):
        return {"key": key}

    def get_issue_links(self, key):
        return list(self._links.get(key, []))

    def delete_issue_link(self, link_id):
        for key, links in self._links.items():
            self._links[key] = [lk for lk in links if lk.get("id") != link_id]
        return {"status": "deleted", "link_id": link_id}


def test_two_passes_managed_unlink_stays_removed(
    outbound_differ: ModuleType, inbound_differ: ModuleType, batch_dispatch: ModuleType
) -> None:
    """Operator-facing churn proof (story safe-luge-nog AC): a user unlinks a MANAGED
    dependency locally; running TWO reconcile passes must leave it removed — pass 1 deletes the
    Jira link (inbound re-add suppressed), and pass 2 over the refreshed snapshot neither
    re-adds it locally nor re-emits anything. This is the regression the whole gate exists for."""
    store = StubBindingStore({"local-1": "PROJ-1", "local-2": "PROJ-2"})
    # Locally DETACHED a managed link: deps empty, managed_refs still records it.
    child = _child(managed_refs=[["blocks", "local-2"]], deps=[])
    jira = _MutableJira(
        {"PROJ-1": [{"id": "L1", "type": {"name": "Blocks"}, "inwardIssue": {"key": "PROJ-2"}}]}
    )

    def _run_pass() -> tuple[list, int]:
        snapshot = _snapshot(jira.issuelinks("PROJ-1"))
        outs, _ = outbound_differ.compute_outbound_mutations(
            local_tickets=[child], jira_snapshot=snapshot, binding_store=store
        )
        # Apply outbound to the mutable Jira (pass-1 deletes the link; later passes no-op).
        for m in outs:
            batch_dispatch.update_one(
                {"key": m.jira_key, "fields": dict(m.fields or {}), "links": list(m.links or [])},
                jira,
            )
        inbound, suppressed = inbound_differ.compute_inbound_mutations(
            snapshot, store, {"local-1": child}, outs
        )
        inbound_link_adds = [lk for im in inbound for lk in (getattr(im, "links", []) or [])]
        return inbound_link_adds, suppressed

    # PASS 1: the managed unlink is propagated (Jira link deleted) and the inbound re-add is
    # suppressed this pass.
    adds_1, suppressed_1 = _run_pass()
    assert not adds_1, "pass 1: inbound link re-add must be suppressed (remove-wins)"
    assert suppressed_1 >= 1
    assert jira.issuelinks("PROJ-1") == [], "pass 1: the Jira link is deleted"

    # PASS 2: over the REFRESHED snapshot (link gone) nothing re-adds it; the unlink stuck.
    adds_2, _ = _run_pass()
    assert not adds_2, "pass 2: a removed managed link is NOT resurrected inbound"
    assert jira.issuelinks("PROJ-1") == [], "pass 2: the unlink stays removed (no churn)"
    assert child["deps"] == [], "local stays unlinked across both passes"
