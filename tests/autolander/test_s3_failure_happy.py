"""S3 happy-path (the ONLY tests the implementer sees): a rebase-conflict records a
needs_rebase marker and strips Autosubmit from the whole stack; the marker store round-trips.
Recheck / CI-fail / self-invalidation / restart / idempotency edges are held out."""

from __future__ import annotations

import pytest
from _autolander_fakes import RecordingClient, change_info

pytestmark = pytest.mark.unit


def test_handle_rebase_conflict_records_marker_and_strips_whole_stack(tmp_path):
    from autolander.failure import OUTCOME_NEEDS_REBASE, MarkerStore, handle_rebase_conflict
    from autolander.loop import WipChain

    bottom = change_info("Ibot", 501, revision="shaB")
    top = change_info("Itop", 502, revision="shaT")
    client = RecordingClient(changes={"Ibot": bottom, "Itop": top})
    wip = WipChain(change_id="Itop", chain_member_ids=["Ibot", "Itop"])
    store = MarkerStore(tmp_path)

    outcome = handle_rebase_conflict(client, wip, store, stack_id="topic-x", now="2026-07-12")

    assert outcome == OUTCOME_NEEDS_REBASE
    # Autosubmit removed (vote 0) from EVERY member
    removed = {
        c[1]
        for c in client.calls
        if c[0] == "set_review" and (c[2]["labels"] or {}).get("Autosubmit") == 0
    }
    assert removed == {"Ibot", "Itop"}
    # a needs_rebase marker was persisted for the tip and is acknowledged after removal
    m = store.get_valid(client, "Itop")
    assert m is not None and m.stack_id == "topic-x" and m.acknowledged is True


def test_marker_store_upsert_and_get_valid_round_trip(tmp_path):
    from autolander.failure import MarkerStore, NeedsRebaseMarker
    from autolander.loop import WipChain  # noqa: F401 (import parity with the module under test)

    change = change_info("Ionly", 503, revision="shaX")
    client = RecordingClient(changes={"Ionly": change})
    store = MarkerStore(tmp_path)
    store.upsert(
        NeedsRebaseMarker(
            change_id="Ionly", patchset_sha="shaX", stack_id="Ionly", change_ids=["Ionly"]
        )
    )

    m = store.get_valid(client, "Ionly")
    assert m is not None and m.patchset_sha == "shaX"
