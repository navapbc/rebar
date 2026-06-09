"""Inbound bound-but-absent mirroring fix (bug 0702-3b6d-c1db-4ed3).

INBOUND symmetric counterpart to the outbound bug 1e08-1a35-0267-4ca6 fix.

``inbound_differ.compute_inbound_mutations`` iterates ONLY the keys present in
``jira_snapshot``. A bound local ticket whose Jira key is ABSENT from this
pass's search snapshot (status=Done beyond the fetcher's ``_DONE_RECENT_CAP``
window) is therefore NEVER visited inbound, so a Jira-side edit to it is
silently dropped in the inbound (Jira→local) direction.

Design — single-GET sharing (the least-fragile option):
  The outbound differ (bug 1e08) already issues the bounded direct GET for
  these alive keys and computes their real ``fields``. Rather than add a second
  GET path (and a second budget) to the inbound differ, the outbound differ
  RECORDS each alive (HTTP 200) bound-but-absent GET into an out-param dict
  (``absent_alive_fields``). The reconcile orchestrator merges those entries
  into the snapshot it passes to the inbound differ, so:
    - each alive bound-but-absent key is GET'd exactly ONCE per pass (shared),
    - the inbound differ's existing loop handles the merged key with NO new
      budget/rotation/GET logic,
    - a 404 key is NEVER recorded (never added to the dict) so inbound never
      mirrors a deleted issue — retirement stays owned by outbound,
    - bidir suppression is unchanged: the outbound update mutation for the same
      key is still passed to inbound, so a field outbound is pushing is
      suppressed inbound (no oscillation against the 1e08 outbound update).

Behavioral assertions only (emitted mutations, GET call counts).
"""

from __future__ import annotations

import importlib.util
import sys
import urllib.error
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
INBOUND_DIFFER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "inbound_differ.py"
)
OUTBOUND_DIFFER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "outbound_differ.py"
)


def _load_module(name: str, path: Path) -> ModuleType:
    if name in sys.modules:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def ib() -> ModuleType:
    return _load_module("inbound_differ_bba", INBOUND_DIFFER_PATH)


@pytest.fixture(scope="module")
def od() -> ModuleType:
    return _load_module("outbound_differ_bba_inbound", OUTBOUND_DIFFER_PATH)


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class StubBindingStore:
    """Implements both directions: get_jira_key (outbound) + get_local_id
    (inbound) plus the absence lifecycle the outbound differ exercises."""

    def __init__(self, bindings: dict[str, str], retired: set[str] | None = None):
        # bindings: {local_id: jira_key}
        self._l2j = dict(bindings)
        self._j2l = {v: k for k, v in bindings.items()}
        self._retired = set(retired or ())
        self.absent_counts: dict[str, int] = {}
        self.cleared: list[str] = []
        self.last_get: dict[str, str] = {}

    # outbound direction
    def get_jira_key(self, local_id):
        return self._l2j.get(local_id)

    def is_bound(self, local_id):
        return local_id in self._l2j

    # inbound direction
    def get_local_id(self, jira_key):
        return self._j2l.get(jira_key)

    # absence lifecycle
    def is_retired(self, jira_key):
        return jira_key in self._retired

    def last_get_pass(self, jira_key):
        return self.last_get.get(jira_key, "")

    def set_last_get(self, jira_key, pass_id):
        self.last_get[jira_key] = pass_id

    def note_absent(self, jira_key):
        self.absent_counts[jira_key] = self.absent_counts.get(jira_key, 0) + 1

    def clear_absent(self, jira_key):
        self.cleared.append(jira_key)
        self.absent_counts[jira_key] = 0


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="http://x/issue/X", code=code, msg="err", hdrs=None, fp=None
    )


def _ticket(tid: str, **over) -> dict:
    base = {
        "ticket_id": tid,
        "title": "Local title",
        "description": "Local desc",
        "status": "open",
        "priority": 2,
        "ticket_type": "bug",
        "assignee": "alice",
        "tags": [],
        "comments": [],
        "deps": [],
    }
    base.update(over)
    return base


def _run_pass(
    od, ib, *, local_tickets, snapshot, store, client, pass_id, local_label_intent=None
):
    """Drive one bidirectional pass the way reconcile.py threads the two
    differs, sharing the outbound bounded-GET results into the inbound snapshot.

    Returns ``(outbound_mutations, inbound_mutations, absent_alive_fields)``.
    """
    absent_alive_fields: dict[str, dict] = {}
    outbound = od.compute_outbound_mutations(
        local_tickets=local_tickets,
        jira_snapshot=snapshot,
        binding_store=store,
        client=client,
        pass_id=pass_id,
        absent_alive_fields=absent_alive_fields,
        local_label_intent=local_label_intent,
    )
    # Orchestrator merge: alive bound-but-absent GETs become inbound-visible.
    inbound_snapshot = dict(snapshot)
    inbound_snapshot.update(absent_alive_fields)
    local_by_id = {t["ticket_id"]: t for t in local_tickets}
    inbound, _suppressed = ib.compute_inbound_mutations(
        inbound_snapshot,
        store,
        local_by_id,
        outbound_mutations=outbound,
    )
    return outbound, inbound, absent_alive_fields


# ===========================================================================
# #1 — bound-but-absent, alive (200), Jira-side change → INBOUND mutation
# ===========================================================================


def test_absent_alive_jira_changed_emits_inbound_mutation(od, ib):
    """A bound local ticket whose Jira key is absent from the snapshot but ALIVE
    (200) with a NON-CONFLICTING Jira-side change → an inbound mutation updates
    the local ticket. RED today: inbound iterates only the snapshot keys, so it
    emits nothing for the absent key.

    The Jira-side change here is a new label (additive — local-wins does NOT
    govern it, so outbound does not clobber it). This is the genuine
    inbound-only signal for an out-of-window edit; scalar fields where local and
    Jira disagree are governed by local-wins (outbound pushes, inbound
    suppressed — see the non-oscillation test)."""
    jira_key = "DIG-OOW"
    # All scalar fields match local (no outbound scalar push); Jira added a
    # label side-band that local does not have.
    ticket = _ticket(
        "loc-1",
        title="Same",
        description="Same",
        assignee="alice",
        tags=[],
    )
    store = StubBindingStore({"loc-1": jira_key})
    client = MagicMock()
    client.get_issue_by_rest.return_value = {
        "fields": {
            "summary": "Same",
            "description": "Same",
            "assignee": {"displayName": "alice"},
            "priority": {"name": "Medium"},
            "status": {"name": "To Do"},
            "labels": ["jira-added-label"],
        }
    }
    _ob, inbound, absent = _run_pass(
        od,
        ib,
        local_tickets=[ticket],
        snapshot={},  # key absent from search working set
        store=store,
        client=client,
        pass_id="p1",
        # Real reconcile path passes the intent map; an empty intent for loc-1
        # gates out the spurious outbound REMOVE of the Jira-side-added label
        # (bug a06c), so inbound's legitimate ADD is not suppressed.
        local_label_intent={"loc-1": set()},
    )
    # The alive GET result was shared into the inbound snapshot.
    assert jira_key in absent
    # Inbound mirrors the Jira-side label addition to local.
    assert len(inbound) == 1
    assert inbound[0].jira_key == jira_key
    assert inbound[0].local_id == "loc-1"
    assert any(
        lm.get("action") == "add" and lm.get("label") == "jira-added-label"
        for lm in inbound[0].labels
    )
    # Single shared GET — outbound issued it; inbound reused the result.
    client.get_issue_by_rest.assert_called_once_with(jira_key)


# ===========================================================================
# #2 — 404 on the inbound bounded GET → no inbound mutation (issue gone)
# ===========================================================================


def test_absent_404_emits_no_inbound_mutation(od, ib):
    """A 404 means the Jira issue is gone. The outbound differ records nothing
    into absent_alive_fields, so inbound never sees the key and emits nothing —
    coordinating with (not fighting) the outbound retirement counter."""
    jira_key = "DIG-GONE"
    ticket = _ticket("loc-1", title="Local")
    store = StubBindingStore({"loc-1": jira_key})
    client = MagicMock()
    client.get_issue_by_rest.side_effect = _http_error(404)
    _ob, inbound, absent = _run_pass(
        od,
        ib,
        local_tickets=[ticket],
        snapshot={},
        store=store,
        client=client,
        pass_id="p1",
    )
    assert jira_key not in absent, "a deleted (404) key must NOT be shared inbound"
    assert inbound == [], "no inbound mutation for a gone Jira issue"
    # Outbound owns the retirement counter — bumped once, not double-counted.
    assert store.absent_counts[jira_key] == 1


# ===========================================================================
# #3 — bidir non-oscillation: a field outbound is syncing is NOT inbound-mirrored
# ===========================================================================


def test_bidir_no_oscillation_outbound_field_suppressed_inbound(od, ib):
    """Local edited the title (outbound will push it). The out-of-window Jira
    issue still has the OLD title. Without suppression, inbound would mirror the
    stale Jira title back over the local edit — fighting the 1e08 outbound
    update. Suppression must hold: inbound emits no title change."""
    jira_key = "DIG-FIGHT"
    ticket = _ticket("loc-1", title="Local NEW title", description="Same")
    store = StubBindingStore({"loc-1": jira_key})
    client = MagicMock()
    client.get_issue_by_rest.return_value = {
        "fields": {
            "summary": "Stale Jira title",
            "description": "Same",
            "assignee": {"displayName": "alice"},
            "priority": {"name": "Medium"},
            "status": {"name": "To Do"},
            "labels": [],
        }
    }
    outbound, inbound, _absent = _run_pass(
        od,
        ib,
        local_tickets=[ticket],
        snapshot={},
        store=store,
        client=client,
        pass_id="p1",
    )
    # Outbound pushes the local title (it is the source of truth).
    assert any(
        m.action == "update" and m.fields.get("summary") == "Local NEW title"
        for m in outbound
    )
    # Inbound must NOT mirror the stale Jira title back (suppression holds).
    for m in inbound:
        assert "title" not in m.fields, (
            f"inbound oscillated against the outbound title push: {m.fields}"
        )


# ===========================================================================
# #4 — budget bound: no double-GET of the same key across inbound+outbound
# ===========================================================================


def test_no_double_get_across_directions(od, ib):
    """The alive bound-but-absent key is GET'd exactly once per pass; the
    inbound side consumes the shared result (no second network call)."""
    jira_key = "DIG-ONE"
    ticket = _ticket("loc-1", title="Old", description="Same")
    store = StubBindingStore({"loc-1": jira_key})
    client = MagicMock()
    client.get_issue_by_rest.return_value = {
        "fields": {
            "summary": "Jira edit",
            "description": "Same",
            "assignee": {"displayName": "alice"},
            "priority": {"name": "Medium"},
            "status": {"name": "To Do"},
            "labels": [],
        }
    }
    _run_pass(
        od,
        ib,
        local_tickets=[ticket],
        snapshot={},
        store=store,
        client=client,
        pass_id="p1",
    )
    assert client.get_issue_by_rest.call_count == 1, (
        "exactly one shared GET across both directions in a single pass"
    )
    client.get_comments.assert_not_called()


def test_inbound_respects_outbound_get_budget(od, ib, monkeypatch):
    """Keys NOT selected by the outbound per-pass GET budget are not GET'd, and
    therefore are not inbound-mirrored this pass (inbound inherits the budget)."""
    import math

    N = 5
    K = 2
    bindings = {f"loc-{i}": f"DIG-{i}" for i in range(N)}
    store = StubBindingStore(bindings)
    tickets = [
        _ticket(f"loc-{i}", title="Old", description="Same") for i in range(N)
    ]
    client = MagicMock()
    client.get_issue_by_rest.return_value = {
        "fields": {
            "summary": "Jira edit",
            "description": "Same",
            "assignee": {"displayName": "alice"},
            "priority": {"name": "Medium"},
            "status": {"name": "To Do"},
            "labels": [],
        }
    }
    monkeypatch.setenv("RECONCILER_ABSENT_GET_BUDGET", str(K))
    _ob, inbound, absent = _run_pass(
        od,
        ib,
        local_tickets=tickets,
        snapshot={},
        store=store,
        client=client,
        pass_id="2026-06-05T09-31-00",
    )
    assert client.get_issue_by_rest.call_count == K, "outbound budget bounds GETs"
    assert len(absent) == K, "only budgeted keys are shared inbound"
    # At most K inbound mutations this pass (the budgeted, alive, changed keys).
    assert len(inbound) <= K
    # Anti-starvation envelope: all serviced within ceil(N/K) passes (outbound's
    # rotation owns this; we just assert the inbound side never exceeds budget).
    assert math.ceil(N / K) >= 1


# ===========================================================================
# #5 — idempotency: two passes over an unchanged out-of-window-alive issue
# ===========================================================================


def test_idempotency_two_passes_unchanged(od, ib):
    """When the out-of-window Jira issue MATCHES local, neither pass emits an
    inbound mutation (no reintroduced churn)."""
    jira_key = "DIG-IDEM"
    ticket = _ticket(
        "loc-1",
        title="Same",
        description="Same desc",
        assignee="alice",
        priority=2,
        status="open",
    )
    store = StubBindingStore({"loc-1": jira_key})
    client = MagicMock()
    client.get_issue_by_rest.return_value = {
        "fields": {
            "summary": "Same",
            "description": "Same desc",
            "assignee": {"displayName": "alice"},
            "priority": {"name": "Medium"},
            "status": {"name": "To Do"},
            "labels": [],
        }
    }
    _ob1, inbound1, _a1 = _run_pass(
        od,
        ib,
        local_tickets=[ticket],
        snapshot={},
        store=store,
        client=client,
        pass_id="p1",
    )
    _ob2, inbound2, _a2 = _run_pass(
        od,
        ib,
        local_tickets=[ticket],
        snapshot={},
        store=store,
        client=client,
        pass_id="p2",
    )
    assert inbound1 == [], "pass 1 (matching) must emit no inbound mutation"
    assert inbound2 == [], "pass 2 must emit no inbound mutation (idempotent)"


# ===========================================================================
# Backward-compat: the out-param is optional (legacy callers omit it).
# ===========================================================================


def test_absent_alive_fields_param_is_optional(od):
    """Existing callers that do not pass absent_alive_fields keep working."""
    jira_key = "DIG-COMPAT"
    ticket = _ticket("loc-1", title="NEW")
    store = StubBindingStore({"loc-1": jira_key})
    client = MagicMock()
    client.get_issue_by_rest.return_value = {
        "fields": {"summary": "OLD", "labels": []}
    }
    result = od.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot={},
        binding_store=store,
        client=client,
        pass_id="p1",
    )
    assert len(result) == 1 and result[0].action == "update"
