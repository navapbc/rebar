"""S2c HELD-OUT oracle (withheld from the implementer): the all-members-fresh gate, the
bounded-await timeout, partial-land detection (loud + never proceed), and per-member ticket
close on merge. Observable behaviour only."""

from __future__ import annotations

import pytest
from _autolander_fakes import RecordingClient, change_info

pytestmark = pytest.mark.unit


def test_submit_blocked_if_any_member_lacks_fresh_verified():
    from autolander.loop import WipChain, all_members_fresh_verified

    bottom = change_info("Ibot", 401, verified=True)
    top = change_info("Itop", 402, verified=False)  # top not yet re-verified
    client = RecordingClient(changes={"Ibot": bottom, "Itop": top})
    wip = WipChain(
        change_id="Itop",
        chain_member_ids=["Ibot", "Itop"],
        tested_shas={"Ibot": bottom["current_revision"], "Itop": top["current_revision"]},
    )

    assert all_members_fresh_verified(client, wip) is False, (
        "must NOT land while any member is unverified"
    )


def test_await_times_out_when_ci_never_verifies():
    from autolander.loop import WipChain, await_fresh_verified

    never = change_info("Ihang", 403, verified=False)
    client = RecordingClient(changes={"Ihang": never})
    wip = WipChain(
        change_id="Ihang",
        chain_member_ids=["Ihang"],
        tested_shas={"Ihang": never["current_revision"]},
    )

    # a clock that jumps past the timeout on the 2nd reading; sleep is a no-op
    ticks = iter([0, 0, 10_000, 10_000, 10_000])
    got = await_fresh_verified(
        client, wip, timeout_s=1800, time_fn=lambda: next(ticks), sleep_fn=lambda _s: None
    )
    assert got is False, "CI-hung await must time out (-> caller hands back), not block forever"


def test_await_returns_true_when_all_fresh():
    from autolander.loop import WipChain, await_fresh_verified

    ok = change_info("Iok", 404, verified=True)
    client = RecordingClient(changes={"Iok": ok})
    wip = WipChain(
        change_id="Iok", chain_member_ids=["Iok"], tested_shas={"Iok": ok["current_revision"]}
    )

    ticks = iter([0, 0, 0, 0])
    got = await_fresh_verified(
        client, wip, timeout_s=1800, time_fn=lambda: next(ticks, 0), sleep_fn=lambda _s: None
    )
    assert got is True


def test_partial_land_raises_loudly_and_does_not_close_tickets():
    from autolander.loop import PartialLandError, WipChain, ancestor_atomic_submit

    # after submit: bottom MERGED but top left NEW -> a partial land
    bottom = change_info("Ibot", 401, verified=True, status="MERGED")
    top = change_info("Itop", 402, verified=True, status="NEW")
    client = RecordingClient(changes={"Ibot": bottom, "Itop": top})
    wip = WipChain(
        change_id="Itop",
        chain_member_ids=["Ibot", "Itop"],
        tested_shas={"Ibot": bottom["current_revision"], "Itop": top["current_revision"]},
    )
    closed = []

    with pytest.raises(PartialLandError):
        ancestor_atomic_submit(client, wip, close_ticket=closed.append)
    assert closed == [], "no tickets closed when the land is partial"


def test_merge_closes_every_member_ticket():
    from autolander.loop import WipChain, ancestor_atomic_submit

    bottom = change_info("Ibot", 401, verified=True, status="MERGED")
    top = change_info("Itop", 402, verified=True, status="MERGED")
    client = RecordingClient(changes={"Ibot": bottom, "Itop": top})
    wip = WipChain(
        change_id="Itop",
        chain_member_ids=["Ibot", "Itop"],
        tested_shas={"Ibot": bottom["current_revision"], "Itop": top["current_revision"]},
    )
    closed = []

    outcome = ancestor_atomic_submit(client, wip, close_ticket=closed.append)

    assert outcome == "merged"
    assert closed == ["Ibot", "Itop"]
    assert [c for c in client.calls if c[0] == "submit"] == [("submit", "Itop", {})]


def test_has_fresh_verified_reads_current_patchset_vote():
    from autolander.loop import has_fresh_verified

    assert has_fresh_verified(change_info("A", 1, verified=True)) is True
    assert has_fresh_verified(change_info("A", 1, verified=False)) is False
