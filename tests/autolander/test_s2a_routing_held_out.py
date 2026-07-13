"""S2a HELD-OUT oracle (withheld from the implementer): FIFO ordering, empty pool,
merge-vs-chain-vs-single routing, on-behalf-of body, and the real GerritClient HTTP
boundary (XSSI strip + request path/body). Asserts observable behaviour only — calls
recorded at the client/transport seam, endpoint kinds, return values — never internals."""

from __future__ import annotations

import pytest
from _autolander_fakes import FakeTransport, RecordingClient, change_info

pytestmark = pytest.mark.unit


# --- selection: FIFO + empty pool -----------------------------------------
def test_fifo_selects_oldest_autosubmit_vote_first():
    from autolander.loop import select_front_candidate

    newest = change_info("Inew", 3, autosubmit_date="2026-07-12 12:00:00.000000000")
    oldest = change_info("Iold", 1, autosubmit_date="2026-07-12 09:00:00.000000000")
    middle = change_info("Imid", 2, autosubmit_date="2026-07-12 10:30:00.000000000")
    # deliberately NOT pre-sorted, to prove the loop sorts by approval date
    client = RecordingClient(query_result=[newest, oldest, middle])

    cand = select_front_candidate(client)

    assert cand is not None
    assert cand.change_id == "Iold", "must pick the OLDEST-voted submittable change (FIFO)"


def test_empty_pool_returns_none():
    from autolander.loop import select_front_candidate

    assert select_front_candidate(RecordingClient(query_result=[])) is None


def test_selection_query_is_submittable_autosubmit():
    """Selection asks Gerrit for open, Autosubmit+1, submittable changes with detailed
    labels (so the FIFO date is available). The filtering is delegated to the query, not
    re-implemented client-side."""
    from autolander.loop import select_front_candidate

    client = RecordingClient(query_result=[])
    select_front_candidate(client)

    q = next(c for c in client.calls if c[0] == "query")
    query_str, meta = q[1], q[2]
    assert "label:Autosubmit+1" in query_str
    assert "is:submittable" in query_str
    assert "status:open" in query_str
    assert any("DETAILED_LABELS" in o for o in (meta["opts"] or [])), "need approval dates"


# --- classification + routing ---------------------------------------------
def test_merge_change_uses_rebase_never_rebase_chain():
    """A --no-ff merge change (current commit has >1 parent) rebases via POST /rebase
    (first-parent-only), NEVER rebase:chain — even if RelatedChanges lists members."""
    from autolander.loop import KIND_MERGE, classify_change, route_rebase, select_front_candidate

    merge = change_info("Imerge", 5, autosubmit_date="2026-07-12 09:00:00.000000000", parents=2)
    client = RecordingClient(
        query_result=[merge],
        # a merge may still show related members; routing must ignore that and use /rebase
        related={"Imerge": [{"change_id": "Imerge"}, {"change_id": "Iother"}]},
    )

    kind, members = classify_change(client, merge)
    assert kind == KIND_MERGE

    cand = select_front_candidate(client)
    assert cand.kind == KIND_MERGE
    endpoint = route_rebase(client, cand)
    assert endpoint == "rebase"
    assert client.mutating_calls() == [("rebase", "Imerge", {"on_behalf_of_uploader": True})]
    assert all(call[0] != "rebase:chain" for call in client.calls)


def test_multi_member_chain_uses_rebase_chain():
    """A >1-member linear non-merge relation chain rebases via POST /rebase:chain."""
    from autolander.loop import KIND_CHAIN, classify_change, route_rebase, select_front_candidate

    top = change_info("Itop", 7, autosubmit_date="2026-07-12 09:00:00.000000000", parents=1)
    # RelatedChanges: current-tip has 3 linear members (bottom -> top)
    related_members = [
        {"change_id": "Ibottom", "_change_number": 5},
        {"change_id": "Imid", "_change_number": 6},
        {"change_id": "Itop", "_change_number": 7},
    ]
    client = RecordingClient(query_result=[top], related={"Itop": related_members})

    kind, members = classify_change(client, top)
    assert kind == KIND_CHAIN
    assert len(members) == 3

    cand = select_front_candidate(client)
    assert cand.kind == KIND_CHAIN
    endpoint = route_rebase(client, cand)
    assert endpoint == "rebase:chain"
    assert client.mutating_calls() == [("rebase:chain", "Itop", {"on_behalf_of_uploader": True})]


def test_single_nonmerge_no_related_is_single():
    from autolander.loop import KIND_SINGLE, classify_change

    c = change_info("Isolo", 9, autosubmit_date="2026-07-12 09:00:00.000000000", parents=1)
    client = RecordingClient(query_result=[c], related={"Isolo": []})
    kind, members = classify_change(client, c)
    assert kind == KIND_SINGLE
    assert members == ["Isolo"]


# --- real GerritClient HTTP boundary --------------------------------------
def _client(transport):
    from autolander.gerrit import GerritClient

    return GerritClient("https://gerrit.example/a", "bot", "tok", transport=transport)


def test_get_json_strips_xssi_prefix(fake_transport: FakeTransport):
    fake_transport.route("GET", "/changes/123", 200, ')]}\'\n{"change_id": "123", "_number": 123}')
    client = _client(fake_transport)
    got = client.get_change("123")
    assert got["change_id"] == "123"


def test_rebase_posts_on_behalf_of_uploader_body(fake_transport: FakeTransport):
    fake_transport.route("POST", "/rebase", 200, ')]}\'\n{"change_id": "123"}')
    client = _client(fake_transport)
    client.rebase("123", on_behalf_of_uploader=True)
    method, path, body = next(c for c in fake_transport.calls if c[0] == "POST")
    assert path.endswith("/rebase") and "rebase:chain" not in path
    assert body == {"rebase_on_behalf_of_uploader": True}


def test_rebase_chain_hits_rebase_chain_endpoint(fake_transport: FakeTransport):
    fake_transport.route("POST", "/rebase:chain", 200, ')]}\'\n{"change_id": "123"}')
    client = _client(fake_transport)
    client.rebase_chain("123", on_behalf_of_uploader=True)
    method, path, body = next(c for c in fake_transport.calls if c[0] == "POST")
    assert path.endswith("/rebase:chain")
    assert body == {"rebase_on_behalf_of_uploader": True}


def test_query_changes_builds_query_and_opts(fake_transport: FakeTransport):
    fake_transport.route("GET", "/changes/", 200, ')]}\'\n[{"change_id": "a", "_number": 1}]')
    client = _client(fake_transport)
    out = client.query_changes("status:open label:Autosubmit+1 is:submittable", ["DETAILED_LABELS"])
    assert out and out[0]["change_id"] == "a"
    method, path, body = fake_transport.calls[-1]
    assert method == "GET" and "o=DETAILED_LABELS" in path


def test_non_2xx_raises_gerrit_error(fake_transport: FakeTransport):
    from autolander.gerrit import GerritError

    fake_transport.route("POST", "/rebase", 409, ')]}\'\n{"message": "conflict"}')
    client = _client(fake_transport)
    with pytest.raises(GerritError) as ei:
        client.rebase("123")
    assert ei.value.status == 409


def test_merged_related_ancestor_excluded_from_chain():
    """A change atop a JUST-MERGED parent (the merged parent still shows in RelatedChanges)
    is a SINGLE change, not a 2-member chain — the merged ancestor is part of main now."""
    from autolander.loop import KIND_SINGLE, classify_change

    top = change_info("Itop", 11, autosubmit_date="2026-07-13 09:00:00.000000000", parents=1)
    related = [
        {"change_id": "Iparent", "_change_number": 10, "status": "MERGED"},  # just landed
        {"change_id": "Itop", "_change_number": 11, "status": "NEW"},
    ]
    client = RecordingClient(query_result=[top], related={"Itop": related})
    kind, members = classify_change(client, top)
    assert kind == KIND_SINGLE, "a merged ancestor must NOT make this a chain"
    assert members == ["Itop"]


# --- FIX 15a1: selection backs off a handed-back stack -----------------------
def test_selection_skips_change_with_valid_handback_marker(tmp_path):
    """A submittable Autosubmit change whose `Autosubmit +1` was cast by ANOTHER account keeps
    matching SELECTION_QUERY after a hand-back (the bot can only zero its OWN vote). A VALID
    `needs_rebase` marker (patchset SHA still matches) means the owner has not rebased yet, so
    selection must SKIP it instead of re-selecting it every poll (the 15a1 infinite loop)."""
    from autolander.failure import MarkerStore, NeedsRebaseMarker
    from autolander.loop import select_front_candidate

    c = change_info("Ihb", 801, autosubmit_date="2026-07-13 10:00:00.000000000")
    client = RecordingClient(query_result=[c], related={"Ihb": []})
    store = MarkerStore(tmp_path)

    # baseline: no marker -> selected normally
    assert select_front_candidate(client, store).change_id == "Ihb"

    # valid marker (patchset_sha == current revision) -> skipped
    store.upsert(
        NeedsRebaseMarker(
            change_id="Ihb",
            patchset_sha=c["current_revision"],
            stack_id="Ihb",
            change_ids=["Ihb"],
        )
    )
    assert select_front_candidate(client, store) is None, "handed-back stack must not re-select"

    # marker_store omitted -> unchanged legacy behaviour (still selects)
    assert select_front_candidate(client).change_id == "Ihb"


def test_selection_skips_handed_back_front_and_picks_next(tmp_path):
    """When the FIFO-front candidate is handed back, selection moves to the NEXT eligible
    candidate in FIFO order rather than returning None."""
    from autolander.failure import MarkerStore, NeedsRebaseMarker
    from autolander.loop import select_front_candidate

    front = change_info("Ifront", 802, autosubmit_date="2026-07-13 09:00:00.000000000")
    nxt = change_info("Inext", 803, autosubmit_date="2026-07-13 09:30:00.000000000")
    client = RecordingClient(query_result=[front, nxt], related={"Ifront": [], "Inext": []})
    store = MarkerStore(tmp_path)
    store.upsert(
        NeedsRebaseMarker(
            change_id="Ifront",
            patchset_sha=front["current_revision"],
            stack_id="Ifront",
            change_ids=["Ifront"],
        )
    )

    cand = select_front_candidate(client, store)
    assert cand is not None and cand.change_id == "Inext", "skip handed-back front, take next"


def test_selection_ignores_stale_handback_marker(tmp_path):
    """A marker whose SHA no longer matches the change's current patchset means the owner has
    rebased/re-uploaded: `get_valid` self-invalidates, so the change is eligible again."""
    from autolander.failure import MarkerStore, NeedsRebaseMarker
    from autolander.loop import select_front_candidate

    c = change_info("Istale", 804, autosubmit_date="2026-07-13 08:00:00.000000000")
    client = RecordingClient(query_result=[c], related={"Istale": []})
    store = MarkerStore(tmp_path)
    store.upsert(
        NeedsRebaseMarker(
            change_id="Istale",
            patchset_sha="OLD_SHA",  # owner has since rebased -> a new patchset
            stack_id="Istale",
            change_ids=["Istale"],
        )
    )

    assert select_front_candidate(client, store).change_id == "Istale"
