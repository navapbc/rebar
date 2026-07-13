"""S2c happy-path (the ONLY tests the implementer sees): all members fresh-verified, then a
single ancestor-atomic submit merges the stack and closes its tickets. Edge cases held out."""

from __future__ import annotations

import pytest
from _autolander_fakes import RecordingClient, change_info

pytestmark = pytest.mark.unit


def test_all_members_fresh_verified_true_when_all_verified():
    from autolander.loop import WipChain, all_members_fresh_verified

    c = change_info("Iv", 400, verified=True)
    client = RecordingClient(changes={"Iv": c})
    wip = WipChain(
        change_id="Iv", chain_member_ids=["Iv"], tested_shas={"Iv": c["current_revision"]}
    )

    assert all_members_fresh_verified(client, wip) is True


def test_ancestor_atomic_submit_merges_and_closes_ticket():
    from autolander.loop import WipChain, ancestor_atomic_submit

    merged = change_info("Iv", 400, verified=True, status="MERGED")
    client = RecordingClient(changes={"Iv": merged})
    wip = WipChain(
        change_id="Iv", chain_member_ids=["Iv"], tested_shas={"Iv": merged["current_revision"]}
    )
    closed = []

    outcome = ancestor_atomic_submit(
        client, wip, close_ticket=lambda cid, *, ticket_id=None: closed.append(cid)
    )

    assert outcome == "merged"
    # exactly one submit, on the tip
    assert [c for c in client.calls if c[0] == "submit"] == [("submit", "Iv", {})]
    assert closed == ["Iv"]
