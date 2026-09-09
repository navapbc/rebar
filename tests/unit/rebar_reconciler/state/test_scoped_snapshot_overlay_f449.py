"""Lag-free scoped snapshot overlay contracts.

Before either differ runs, actively scoped bindings are refreshed from the
primary Jira store. Mirrored scalar fields merge into the search snapshot
without removing enrichment. Transport errors and missing issues preserve the
prior snapshot. The refreshed view prevents stale inbound clobber.
"""

from __future__ import annotations

import sys
import urllib.error
from pathlib import Path
from typing import Any

_ENGINE = Path(__file__).resolve().parents[4] / "src" / "rebar" / "_engine"
if str(_ENGINE) not in sys.path:  # pragma: no cover - import bootstrap
    sys.path.insert(0, str(_ENGINE))

from rebar_reconciler.inbound_differ import compute_inbound_mutations  # noqa: E402
from rebar_reconciler.snapshot_lagfree_refresh import (  # noqa: E402
    overlay_lagfree_scalars,
    refresh_scoped_snapshot,
)


class _FreshClient:
    """A transport whose direct GET returns lag-free fields (the primary store)."""

    def __init__(self, fields_by_key: dict[str, dict[str, Any]]) -> None:
        self._fields = fields_by_key
        self.calls: list[str] = []

    def get_issue_by_rest(self, jira_key: str) -> dict[str, Any]:
        self.calls.append(jira_key)
        return {"fields": self._fields[jira_key]}


class _HTTPErrorClient:
    def __init__(self, code: int) -> None:
        self._code = code

    def get_issue_by_rest(self, jira_key: str) -> dict[str, Any]:
        raise urllib.error.HTTPError(
            url="http://x/" + jira_key, code=self._code, msg="boom", hdrs=None, fp=None
        )


class _TransportErrorClient:
    def get_issue_by_rest(self, jira_key: str) -> dict[str, Any]:
        raise urllib.error.URLError("connection reset")


def _vendor(**ov: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "summary": "OLD title",
        "description": "OLD body",
        "priority": {"name": "Medium"},
        "status": {"name": "To Do"},
        "assignee": "alice@x.com",
    }
    entry.update(ov)
    return entry


# --- 1. the overlay merges the five mirrored scalar fields ------------------------


def test_overlay_merges_lagfree_scalar_fields_over_the_stale_entry() -> None:
    stale = {"REB-1": _vendor(description="OLD body", summary="OLD title")}
    fresh = {"REB-1": _vendor(description="NEW body", summary="NEW title")}
    client = _FreshClient(fresh)

    refreshed = overlay_lagfree_scalars(stale, ["REB-1"], client)

    assert refreshed == 1
    assert client.calls == ["REB-1"]
    assert stale["REB-1"]["description"] == "NEW body"
    assert stale["REB-1"]["summary"] == "NEW title"


# --- 2. enrichment (parent/comment/issuelinks) MUST survive the merge -------------


def test_overlay_preserves_snapshot_enrichment_keys() -> None:
    """The overlay merges mirrored fields without discarding snapshot enrichment."""
    stale = {
        "REB-1": _vendor(
            description="OLD body",
            parent="REB-9",
            comment={"comments": [{"id": "1"}]},
            issuelinks=[{"id": "L1"}],
        )
    }
    # The fresh GET returns only the base fields (no enrichment).
    fresh = {"REB-1": _vendor(description="NEW body")}

    overlay_lagfree_scalars(stale, ["REB-1"], _FreshClient(fresh))

    assert stale["REB-1"]["description"] == "NEW body"
    assert stale["REB-1"]["parent"] == "REB-9"
    assert stale["REB-1"]["comment"] == {"comments": [{"id": "1"}]}
    assert stale["REB-1"]["issuelinks"] == [{"id": "L1"}]


# --- 3. fallback: a transport error / 404 leaves the entry UNTOUCHED (defer) -------


def test_overlay_leaves_entry_untouched_on_transport_error() -> None:
    stale = {"REB-1": _vendor(description="OLD body")}

    refreshed = overlay_lagfree_scalars(stale, ["REB-1"], _TransportErrorClient())

    assert refreshed == 0
    assert stale["REB-1"]["description"] == "OLD body"


def test_overlay_leaves_entry_untouched_on_404() -> None:
    stale = {"REB-1": _vendor(description="OLD body")}

    refreshed = overlay_lagfree_scalars(stale, ["REB-1"], _HTTPErrorClient(404))

    assert refreshed == 0
    assert stale["REB-1"]["description"] == "OLD body"


# --- 4. a key absent from the snapshot is skipped (no GET, no crash) ---------------


def test_overlay_skips_keys_absent_from_the_snapshot() -> None:
    stale = {"REB-1": _vendor()}
    client = _FreshClient({"REB-2": _vendor()})

    refreshed = overlay_lagfree_scalars(stale, ["REB-2"], client)

    assert refreshed == 0
    assert client.calls == []  # never GET a key we are not arbitrating this pass


# --- 5. TEETH: a stale snapshot clobbers inbound; the overlay prevents it ----------


class _IdentityInboundMapper:
    def map_remote_to_local(self, remote_fields: dict[str, Any]) -> dict[str, Any]:
        out = dict(remote_fields)
        pr = out.get("priority")
        if isinstance(pr, dict):
            out["priority"] = 2 if pr.get("name") == "Medium" else 1
        st = out.get("status")
        if isinstance(st, dict):
            out["status"] = "open" if st.get("name") == "To Do" else "closed"
        out["title"] = out.get("summary", out.get("title"))
        return out

    def normalize_rich_text(self, body: Any) -> str:  # pragma: no cover - unused
        return "" if body is None else str(body)


class _PassthroughOutboundMapper:
    def map_fields_to_remote(self, changed: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        return dict(changed)

    def resolve_assignee(self, *a: Any, **k: Any) -> tuple[Any, bool, bool]:
        return (None, False, False)


class _BindingStore:
    def __init__(self, reverse: dict[str, str]) -> None:
        self._reverse = reverse

    def get_local_id(self, jira_key: str) -> str | None:
        return self._reverse.get(jira_key)


def _local(**ov: Any) -> dict[str, Any]:
    t: dict[str, Any] = {
        "ticket_id": "loc-1",
        "ticket_type": "task",
        "title": "NEW title",
        "description": "NEW body",
        "priority": 2,
        "status": "open",
        "assignee": "alice@x.com",
    }
    t.update(ov)
    return t


def _inbound(snapshot: dict[str, dict[str, Any]]) -> list[Any]:
    mutations, _ = compute_inbound_mutations(
        snapshot,
        _BindingStore({"REB-1": "loc-1"}),
        {"loc-1": _local()},
        inbound_mapper=_IdentityInboundMapper(),
        outbound_mapper=_PassthroughOutboundMapper(),
    )
    return [m for m in mutations if "description" in getattr(m, "fields", {})]


def test_stale_snapshot_would_clobber_without_the_overlay() -> None:
    """A stale search snapshot makes the inbound differ revert local state."""
    stale = {"REB-1": _vendor(description="OLD body", summary="NEW title")}

    clobbers = _inbound(stale)

    assert len(clobbers) == 1
    assert clobbers[0].fields["description"] == "OLD body"


def test_overlay_prevents_the_inbound_clobber() -> None:
    """With the scoped overlay applied, the snapshot is lag-free (description=NEW==local),
    so the inbound differ mirrors nothing — rebar's own write survives the echo pass."""
    stale = {"REB-1": _vendor(description="OLD body", summary="NEW title")}
    fresh = {"REB-1": _vendor(description="NEW body", summary="NEW title")}

    overlay_lagfree_scalars(stale, ["REB-1"], _FreshClient(fresh))

    assert _inbound(stale) == []


# The wrapper maps scoped local IDs to Jira keys and skips the overlay when scope,
# transport, bindings, or snapshot entries are unavailable.


class _CtxBindingStore:
    """Forward (local_id -> jira_key) binding store used by the orchestrator."""

    def __init__(self, forward: dict[str, str]) -> None:
        self._forward = forward

    def get_jira_key(self, local_id: str) -> str | None:
        return self._forward.get(local_id)


class _Ctx:
    """Minimal stand-in for reconcile._PassContext (attribute duck-type)."""

    def __init__(
        self,
        *,
        selection_ids: Any = None,
        filter_local_ids: Any = None,
        runtime_transport: Any = None,
        binding_store: Any = None,
        curr_snapshot: Any = None,
    ) -> None:
        self.selection_ids = selection_ids
        self.filter_local_ids = filter_local_ids
        self.runtime_transport = runtime_transport
        self.binding_store = binding_store
        self.curr_snapshot = curr_snapshot


def test_orchestrator_noops_for_an_unscoped_pass() -> None:
    """No selection_ids and no filter_local_ids -> never GET (bounded-cost guard)."""
    snapshot = {"REB-1": _vendor(description="OLD body")}
    client = _FreshClient({"REB-1": _vendor(description="NEW body")})
    ctx = _Ctx(
        runtime_transport=client,
        binding_store=_CtxBindingStore({"loc-1": "REB-1"}),
        curr_snapshot=snapshot,
    )

    refresh_scoped_snapshot(ctx)

    assert client.calls == []
    assert snapshot["REB-1"]["description"] == "OLD body"


def test_orchestrator_noops_when_no_transport_is_bound() -> None:
    """A partial test ctx (no runtime_transport) must be a safe no-op, not a crash."""
    snapshot = {"REB-1": _vendor(description="OLD body")}
    ctx = _Ctx(
        selection_ids=["loc-1"],
        runtime_transport=None,
        binding_store=_CtxBindingStore({"loc-1": "REB-1"}),
        curr_snapshot=snapshot,
    )

    refresh_scoped_snapshot(ctx)

    assert snapshot["REB-1"]["description"] == "OLD body"


def test_orchestrator_noops_without_binding_store_or_snapshot() -> None:
    client = _FreshClient({"REB-1": _vendor(description="NEW body")})

    # missing binding_store
    refresh_scoped_snapshot(
        _Ctx(
            selection_ids=["loc-1"],
            runtime_transport=client,
            binding_store=None,
            curr_snapshot={"REB-1": _vendor()},
        )
    )
    # empty snapshot
    refresh_scoped_snapshot(
        _Ctx(
            selection_ids=["loc-1"],
            runtime_transport=client,
            binding_store=_CtxBindingStore({"loc-1": "REB-1"}),
            curr_snapshot={},
        )
    )

    assert client.calls == []


def test_orchestrator_skips_a_scoped_id_whose_key_is_not_in_the_snapshot() -> None:
    """A scoped local_id that resolves to a key ABSENT from this pass's snapshot is not
    GET (unbound key resolution or bind_store-only key)."""
    snapshot = {"REB-1": _vendor(description="OLD body")}
    client = _FreshClient({"REB-2": _vendor(description="NEW body")})
    ctx = _Ctx(
        selection_ids=["loc-2"],
        runtime_transport=client,
        binding_store=_CtxBindingStore({"loc-2": "REB-2"}),  # REB-2 not in snapshot
        curr_snapshot=snapshot,
    )

    refresh_scoped_snapshot(ctx)

    assert client.calls == []
    assert snapshot["REB-1"]["description"] == "OLD body"


def test_orchestrator_refreshes_the_scoped_bound_key_via_selection_ids() -> None:
    """Happy path: a scoped selection_id -> jira_key in-snapshot -> the entry is refreshed
    lag-free from the primary store."""
    snapshot = {"REB-1": _vendor(description="OLD body", parent="REB-9")}
    client = _FreshClient({"REB-1": _vendor(description="NEW body")})
    ctx = _Ctx(
        selection_ids=["loc-1"],
        runtime_transport=client,
        binding_store=_CtxBindingStore({"loc-1": "REB-1"}),
        curr_snapshot=snapshot,
    )

    refresh_scoped_snapshot(ctx)

    assert client.calls == ["REB-1"]
    assert snapshot["REB-1"]["description"] == "NEW body"
    assert snapshot["REB-1"]["parent"] == "REB-9"  # enrichment preserved


def test_orchestrator_uses_filter_local_ids_when_selection_ids_absent() -> None:
    """The scoped set falls back to filter_local_ids (the other scoping seam)."""
    snapshot = {"REB-1": _vendor(description="OLD body")}
    client = _FreshClient({"REB-1": _vendor(description="NEW body")})
    ctx = _Ctx(
        selection_ids=None,
        filter_local_ids=["loc-1"],
        runtime_transport=client,
        binding_store=_CtxBindingStore({"loc-1": "REB-1"}),
        curr_snapshot=snapshot,
    )

    refresh_scoped_snapshot(ctx)

    assert client.calls == ["REB-1"]
    assert snapshot["REB-1"]["description"] == "NEW body"
