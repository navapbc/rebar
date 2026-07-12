"""S2a happy-path (the ONLY tests the implementer sees): the minimal correct behaviour for
selection + single-change rebase-routing. Edge/E2E cases are held out."""

from __future__ import annotations

import pytest
from _fakes import RecordingClient, change_info

pytestmark = pytest.mark.unit


def test_selects_the_single_submittable_autosubmit_change():
    """One submittable Autosubmit+1 change in the pool -> it is the front candidate,
    classified as a single (non-merge, no relation chain) change."""
    from autolander.loop import KIND_SINGLE, select_front_candidate

    c = change_info("Ione", 101, autosubmit_date="2026-07-12 10:00:00.000000000", parents=1)
    client = RecordingClient(query_result=[c], related={"Ione": []})

    cand = select_front_candidate(client)

    assert cand is not None
    assert cand.change_id == "Ione"
    assert cand.number == 101
    assert cand.kind == KIND_SINGLE
    assert cand.member_ids == ["Ione"]


def test_route_single_change_uses_rebase_preserving_uploader():
    """A single candidate is routed to POST /rebase (NOT rebase:chain), preserving the
    uploader so DCO/rebar-ticket trailers survive and CI re-runs."""
    from autolander.loop import KIND_SINGLE, Candidate, route_rebase

    client = RecordingClient()
    cand = Candidate(
        change_id="Ione",
        number=101,
        autosubmit_date="2026-07-12 10:00:00.000000000",
        kind=KIND_SINGLE,
        member_ids=["Ione"],
    )

    endpoint = route_rebase(client, cand)

    assert endpoint == "rebase"
    assert client.mutating_calls() == [("rebase", "Ione", {"on_behalf_of_uploader": True})]
