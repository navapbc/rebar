"""Bound-but-absent re-emitter fix (bug 1e08-1a35-0267-4ca6).

A bound local ticket whose Jira key is ABSENT from a pass's search snapshot
(deleted, or status=Done beyond the fetcher's _DONE_RECENT_CAP window) used to
diff every field against "" and re-emit every pass. The fix discriminates on
membership and, for the absent case, does a bounded direct GET:
  - 200 (alive, out-of-window) → sync the real diff, no churn,
  - 404 (deleted)             → emit nothing; retire after GRACE consecutive 404s,
  - transport error           → emit nothing, defer.

Covers design regression tests #1, #2, #3, #4, #5, #6, #7, #8, #9, #10, #11, #14.

Behavioral assertions only (emitted mutations, GET call counts, retired-set).
"""

from __future__ import annotations

import importlib.util
import math
import sys
import urllib.error
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
OUTBOUND_DIFFER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "dso_reconciler" / "outbound_differ.py"
)
BINDING_STORE_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "dso_reconciler" / "binding_store.py"
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
def od() -> ModuleType:
    return _load_module("outbound_differ_bound_but_absent", OUTBOUND_DIFFER_PATH)


@pytest.fixture()
def binding_store_mod() -> ModuleType:
    return _load_module("binding_store_bba", BINDING_STORE_PATH)


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class StubBindingStore:
    """A binding store implementing get_jira_key + the absence lifecycle."""

    def __init__(self, bindings: dict[str, str], retired: set[str] | None = None):
        self._bindings = dict(bindings)
        self._retired = set(retired or ())
        self.absent_counts: dict[str, int] = {}
        self.cleared: list[str] = []
        self.last_get: dict[str, str] = {}

    def get_jira_key(self, local_id):
        return self._bindings.get(local_id)

    def is_bound(self, local_id):
        return local_id in self._bindings

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
        "title": "Some issue",
        "description": "desc",
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


# ===========================================================================
# #7 — _safe_get_issue HTTPError mapping
# ===========================================================================


def test_safe_get_issue_404_maps_to_deleted(od):
    client = MagicMock()
    client.get_issue_by_rest.side_effect = _http_error(404)
    assert od._safe_get_issue(client, "DIG-1") is od._DELETED


def test_safe_get_issue_500_maps_to_transport_error(od):
    client = MagicMock()
    client.get_issue_by_rest.side_effect = _http_error(500)
    assert od._safe_get_issue(client, "DIG-1") is od._TRANSPORT_ERROR


def test_safe_get_issue_urlerror_maps_to_transport_error(od):
    client = MagicMock()
    client.get_issue_by_rest.side_effect = urllib.error.URLError("boom")
    assert od._safe_get_issue(client, "DIG-1") is od._TRANSPORT_ERROR


def test_safe_get_issue_timeout_maps_to_transport_error(od):
    client = MagicMock()
    client.get_issue_by_rest.side_effect = TimeoutError("slow")
    assert od._safe_get_issue(client, "DIG-1") is od._TRANSPORT_ERROR


def test_safe_get_issue_200_returns_fields(od):
    client = MagicMock()
    client.get_issue_by_rest.return_value = {"fields": {"summary": "S"}}
    assert od._safe_get_issue(client, "DIG-1") == {"summary": "S"}


# ===========================================================================
# #1 — bound-but-absent, alive (200), divergent → update IS emitted
# ===========================================================================


def test_absent_alive_divergent_emits_update(od):
    jira_key = "DIG-200"
    ticket = _ticket("loc-1", title="NEW TITLE")
    store = StubBindingStore({"loc-1": jira_key})
    client = MagicMock()
    # GET returns the OLD title — divergent from local "NEW TITLE".
    client.get_issue_by_rest.return_value = {
        "fields": {"summary": "OLD TITLE", "labels": []}
    }
    result = od.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot={},  # key absent
        binding_store=store,
        client=client,
        pass_id="p1",
    )
    assert len(result) == 1
    assert result[0].action == "update"
    assert result[0].jira_key == jira_key
    assert result[0].fields.get("summary") == "NEW TITLE"
    client.get_issue_by_rest.assert_called_once_with(jira_key)
    assert store.last_get[jira_key] == "p1"
    assert jira_key in store.cleared  # 200 resets counter


# ===========================================================================
# #3 — RED anchor: bound-but-absent never diffs against {}/""
# ===========================================================================


def test_absent_alive_matching_emits_nothing(od):
    """The original defect: absent key diffed every field against "" and
    re-emitted. With the fix, a 200 GET whose fields MATCH local emits zero."""
    jira_key = "DIG-MATCH"
    ticket = _ticket("loc-1", title="Same", description="Same desc", assignee="alice")
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
    result = od.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot={},
        binding_store=store,
        client=client,
        pass_id="p1",
    )
    assert result == [], (
        "Bound-but-absent alive key with matching fields must emit nothing — "
        "the original defect re-emitted because it diffed against {}."
    )


# ===========================================================================
# #2 — bound-but-absent 404 → zero mutations; retire after GRACE; no further GET
# ===========================================================================


def test_absent_404_emits_nothing_and_notes_absent(od):
    jira_key = "DIG-404"
    ticket = _ticket("loc-1", title="x")
    store = StubBindingStore({"loc-1": jira_key})
    client = MagicMock()
    client.get_issue_by_rest.side_effect = _http_error(404)
    result = od.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot={},
        binding_store=store,
        client=client,
        pass_id="p1",
    )
    assert result == []
    assert store.absent_counts[jira_key] == 1
    assert store.last_get[jira_key] == "p1"


def test_absent_404_retires_after_grace_then_no_further_get(
    od, binding_store_mod, tmp_path, monkeypatch
):
    """Real BindingStore: GRACE consecutive-404 passes retire the key; once
    retired, no further GET is issued (budget preserved)."""
    monkeypatch.setenv("RECONCILER_ABSENT_RETIRE_GRACE", "3")
    jira_key = "DIG-DEAD"
    bs = binding_store_mod.BindingStore(tmp_path / ".tickets-tracker")
    bs.bind_confirm("loc-1", jira_key)
    bs.save()
    ticket = _ticket("loc-1", title="x")
    client = MagicMock()
    client.get_issue_by_rest.side_effect = _http_error(404)

    for i in range(3):
        assert not bs.is_retired(jira_key), f"must not be retired before pass {i + 1}"
        od.compute_outbound_mutations(
            local_tickets=[ticket],
            jira_snapshot={},
            binding_store=bs,
            client=client,
            pass_id=f"p{i}",
        )
    assert bs.is_retired(jira_key), "must retire after GRACE consecutive 404s"
    gets_at_retirement = client.get_issue_by_rest.call_count
    assert gets_at_retirement == 3

    # Further passes: retired → no GET.
    od.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot={},
        binding_store=bs,
        client=client,
        pass_id="p99",
    )
    assert client.get_issue_by_rest.call_count == gets_at_retirement, (
        "retired key must not be GET'd again"
    )


# ===========================================================================
# #9 — a single 200 resets the absence counter (no premature retirement)
# ===========================================================================


def test_single_200_resets_counter(od, binding_store_mod, tmp_path, monkeypatch):
    monkeypatch.setenv("RECONCILER_ABSENT_RETIRE_GRACE", "3")
    jira_key = "DIG-FLAP"
    bs = binding_store_mod.BindingStore(tmp_path / ".tickets-tracker")
    bs.bind_confirm("loc-1", jira_key)
    bs.save()
    ticket = _ticket("loc-1", title="x")
    client = MagicMock()

    # Two 404s, then a 200 (resets), then two more 404s → must NOT retire.
    seq = [
        _http_error(404),
        _http_error(404),
        {"fields": {"summary": "x", "labels": []}},
        _http_error(404),
        _http_error(404),
    ]

    def _side(_key):
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    client.get_issue_by_rest.side_effect = _side
    for i in range(5):
        od.compute_outbound_mutations(
            local_tickets=[ticket],
            jira_snapshot={},
            binding_store=bs,
            client=client,
            pass_id=f"p{i}",
        )
    assert not bs.is_retired(jira_key), (
        "a 200 mid-sequence must reset the consecutive-404 counter"
    )


# ===========================================================================
# #4 — present-with-empty-fields (key IN snapshot) still diffs and emits
# ===========================================================================


def test_present_empty_fields_still_diffs(od):
    jira_key = "DIG-EMPTY"
    ticket = _ticket("loc-1", title="Local title")
    store = StubBindingStore({"loc-1": jira_key})
    client = MagicMock()
    # Key IS present but with empty fields → membership says present → diff path.
    result = od.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot={jira_key: {}},
        binding_store=store,
        client=client,
        pass_id="p1",
    )
    assert len(result) == 1 and result[0].action == "update"
    assert result[0].fields.get("summary") == "Local title"
    # No GET issued — membership (not value) is the discriminator.
    client.get_issue_by_rest.assert_not_called()


# ===========================================================================
# #5 — pending binding (jira_key=None) routes to create, never the absent guard
# ===========================================================================


def test_pending_binding_routes_to_create(od):
    ticket = _ticket("loc-pending", title="New")
    store = StubBindingStore({})  # get_jira_key returns None
    client = MagicMock()
    result = od.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot={},
        binding_store=store,
        client=client,
        pass_id="p1",
    )
    assert len(result) == 1 and result[0].action == "create"
    client.get_issue_by_rest.assert_not_called()


# ===========================================================================
# #6 — recovered-this-pass binding absent from snapshot → GET 200 syncs same pass
# ===========================================================================


def test_recovered_this_pass_syncs(od):
    jira_key = "DIG-RECOV"
    ticket = _ticket("loc-1", title="Edited locally")
    store = StubBindingStore({"loc-1": jira_key})
    client = MagicMock()
    client.get_issue_by_rest.return_value = {
        "fields": {"summary": "Stale", "labels": []}
    }
    result = od.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot={},
        binding_store=store,
        client=client,
        pass_id="p1",
    )
    assert len(result) == 1 and result[0].fields.get("summary") == "Edited locally"


# ===========================================================================
# #11 — 200 path issues exactly ONE network call (field + comment diff, no double-fetch)
# ===========================================================================


def test_200_path_single_network_call(od):
    jira_key = "DIG-ONECALL"
    ticket = _ticket(
        "loc-1",
        title="T",
        comments=[{"body": "a brand new local comment"}],
    )
    store = StubBindingStore({"loc-1": jira_key})
    client = MagicMock()
    # GET returns fields WITH a native comment block (no second get_comments).
    client.get_issue_by_rest.return_value = {
        "fields": {
            "summary": "T",
            "labels": [],
            "comment": {"comments": [], "total": 0},
        }
    }
    result = od.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot={},
        binding_store=store,
        client=client,
        pass_id="p1",
    )
    # Exactly ONE network call total: the direct GET.
    assert client.get_issue_by_rest.call_count == 1
    client.get_comments.assert_not_called()
    # The new comment IS emitted (overlay carried the GET's comment block).
    comment_muts = [m for m in result if m.comments]
    assert len(comment_muts) == 1
    assert any("brand new local comment" in c["body"] for c in comment_muts[0].comments)


# ===========================================================================
# #14 — idempotency anchor: two passes over unchanged absent-alive → 0 mutations pass 2
# ===========================================================================


def test_idempotency_two_passes(od):
    jira_key = "DIG-IDEM"
    # Local matches Jira exactly.
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
    r1 = od.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot={},
        binding_store=store,
        client=client,
        pass_id="p1",
    )
    r2 = od.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot={},
        binding_store=store,
        client=client,
        pass_id="p2",
    )
    assert r1 == [], "pass 1 (matching) must already be a no-op"
    assert r2 == [], "pass 2 must be a no-op (no reintroduced churn)"


# ===========================================================================
# #8 — rotation anti-starvation: N >> K serviced within ceil(N/K) passes;
#      a dead key behind a saturated budget still retires.
# ===========================================================================


def test_rotation_services_all_within_ceil_n_over_k(
    od, binding_store_mod, tmp_path, monkeypatch
):
    monkeypatch.setenv("RECONCILER_ABSENT_GET_BUDGET", "2")
    monkeypatch.setenv("RECONCILER_ABSENT_RETIRE_GRACE", "100")  # never retire
    N = 7
    K = 2
    bs = binding_store_mod.BindingStore(tmp_path / ".tickets-tracker")
    tickets = []
    keys = []
    for i in range(N):
        lid, jk = f"loc-{i}", f"DIG-{i}"
        bs.bind_confirm(lid, jk)
        tickets.append(_ticket(lid, title=f"T{i}"))
        keys.append(jk)
    bs.save()
    client = MagicMock()
    client.get_issue_by_rest.return_value = {"fields": {"summary": "x", "labels": []}}

    serviced: set[str] = set()
    passes = math.ceil(N / K)
    for p in range(passes):
        before = client.get_issue_by_rest.call_count
        od.compute_outbound_mutations(
            local_tickets=tickets,
            jira_snapshot={},
            binding_store=bs,
            client=client,
            pass_id=f"2026-06-05T09-31-{p:02d}",
        )
        # Capture which keys were GET'd this pass via last_get_pass.
        for jk in keys:
            if bs.last_get_pass(jk) == f"2026-06-05T09-31-{p:02d}":
                serviced.add(jk)
        after = client.get_issue_by_rest.call_count
        assert after - before <= K, f"pass {p} exceeded budget K={K}"
    assert serviced == set(keys), (
        f"every absent key must be serviced within ceil(N/K)={passes} passes; "
        f"missing: {set(keys) - serviced}"
    )


def test_dead_key_behind_saturated_budget_still_retires(
    od, binding_store_mod, tmp_path, monkeypatch
):
    monkeypatch.setenv("RECONCILER_ABSENT_GET_BUDGET", "1")
    monkeypatch.setenv("RECONCILER_ABSENT_RETIRE_GRACE", "2")
    bs = binding_store_mod.BindingStore(tmp_path / ".tickets-tracker")
    # Two absent keys; one dead (404), one alive (200). Budget K=1.
    bs.bind_confirm("loc-dead", "DIG-DEAD")
    bs.bind_confirm("loc-alive", "DIG-ALIVE")
    bs.save()
    tickets = [_ticket("loc-dead", title="d"), _ticket("loc-alive", title="a")]

    def _side(key):
        if key == "DIG-DEAD":
            raise _http_error(404)
        return {"fields": {"summary": "x", "labels": []}}

    client = MagicMock()
    client.get_issue_by_rest.side_effect = _side

    # Run enough passes for rotation to service the dead key GRACE times.
    for p in range(12):
        od.compute_outbound_mutations(
            local_tickets=tickets,
            jira_snapshot={},
            binding_store=bs,
            client=client,
            pass_id=f"2026-06-05T09-31-{p:02d}",
        )
        if bs.is_retired("DIG-DEAD"):
            break
    assert bs.is_retired("DIG-DEAD"), (
        "a dead key sharing a saturated budget must still accrue its GRACE "
        "404s via rotation and retire in bounded passes"
    )


# ===========================================================================
# #10 — _rest_issue_to_snapshot_fields parity on intersecting field VALUES
# ===========================================================================


def test_rest_issue_to_snapshot_fields_parity(od):
    """A real GET payload's fields, run through the helper, match the fetcher
    snapshot entry on the intersecting field set (the helper does NO transform,
    so values are identical for shared keys; a GET is a superset)."""
    get_payload = {
        "key": "DIG-1",
        "fields": {
            "summary": "Title",
            "description": {"type": "doc", "content": []},
            "parent": {"key": "DIG-EPIC"},
            "priority": {"name": "High"},
            "status": {"name": "Done"},
            "assignee": {"displayName": "Bob", "accountId": "abc"},
            "extra_get_only_field": "ignored",
        },
    }
    # The fetcher stores a verbatim sorted copy of the SEARCH fields subset.
    fetcher_entry = {
        k: get_payload["fields"][k]
        for k in sorted(
            {"summary", "description", "parent", "priority", "status", "assignee"}
        )
    }
    helper_out = od._rest_issue_to_snapshot_fields(get_payload)
    intersect = set(helper_out) & set(fetcher_entry)
    assert "summary" in intersect and "parent" in intersect
    for k in intersect:
        assert helper_out[k] == fetcher_entry[k], f"value parity mismatch on {k}"


# ===========================================================================
# transport error → emit nothing, defer (counter untouched)
# ===========================================================================


def test_transport_error_defers_counter_untouched(od):
    jira_key = "DIG-TX"
    ticket = _ticket("loc-1", title="x")
    store = StubBindingStore({"loc-1": jira_key})
    client = MagicMock()
    client.get_issue_by_rest.side_effect = _http_error(503)
    result = od.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot={},
        binding_store=store,
        client=client,
        pass_id="p1",
    )
    assert result == []
    # note_absent NOT called on transport error.
    assert jira_key not in store.absent_counts
    assert store.last_get[jira_key] == "p1"  # GET still recorded
