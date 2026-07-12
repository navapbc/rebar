"""S2a HELD-OUT oracle (withheld from the implementer): FIFO ordering, empty pool,
merge-vs-chain-vs-single routing, on-behalf-of body, and the real GerritClient HTTP
boundary (XSSI strip + request path/body). Asserts observable behaviour only — calls
recorded at the client/transport seam, endpoint kinds, return values — never internals."""

from __future__ import annotations

import pytest
from _fakes import FakeTransport, RecordingClient, change_info

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
