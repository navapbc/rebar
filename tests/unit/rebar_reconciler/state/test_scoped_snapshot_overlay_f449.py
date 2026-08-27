"""Bug f449 — a scoped reconcile pass must arbitrate on LAG-FREE remote state.

The exposed defect (see f449 root-cause): the bound-field INBOUND differ
(``inbound_differ._diff_jira_vs_local``) is LEVEL-triggered and consults NO baseline —
it emits an inbound mirror for any mirrored field where the current snapshot differs from
local. The snapshot is the fetcher's JQL-SEARCH result, which lags on an eventually-
consistent remote. After rebar pushes a field (local=NEW, baseline advanced to NEW), the
OUTBOUND differ suppresses it (local==baseline, ADR 0026 / bug e6e9), so same-pass
bidirectional suppression (bug 3bf8) does NOT fire — and the inbound differ, seeing the
STALE snapshot (description=OLD) against local(NEW), mirrors OLD back over local. That is a
clobber of rebar's own just-synced write.

The fix refreshes the actively-scoped bound keys from the PRIMARY store
(``get_issue_by_rest`` — immediately consistent) and MERGES the mirrored scalar fields into
``curr_snapshot`` BEFORE both differs and baseline advancement run. With the snapshot made
lag-free, the echo pass sees jira==local and mirrors nothing.

This is the HELD-OUT oracle (rebar-implement TDD): it pins the overlay's field-merge,
enrichment preservation, transport-error / 404 fallback, and — the teeth — that a stale
snapshot WOULD clobber but the overlay prevents it.
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
    """The search snapshot entry carries enrichment (parent/comment/issuelinks) added by
    the fetcher's _enrich_project AFTER the base fields. A direct GET does not return that
    enrichment; the overlay must MERGE mirrored scalars, never wholesale-replace."""
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
    """Documents the bug (and gives the fix its teeth): a stale post-write search snapshot
    (description=OLD) against local(NEW) makes the level-triggered inbound differ emit a
    description mirror that reverts local to OLD."""
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
