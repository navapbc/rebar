"""Drive-glue oracle (loop._drive_candidate / _terminal_verified_vote) — the operational
sequencing that composes the tested S2/S3 pieces. Regression tests for two bugs the live
dogfood surfaced: a rebase 409 must hand back (not escape), and the auto-recheck must treat
the clear-vote's transient Verified=0 as 'still running', not a failure."""

from __future__ import annotations

import pytest
from _autolander_fakes import RecordingClient, change_info

pytestmark = pytest.mark.unit


def test_drive_rebase_409_records_needs_rebase_and_strips(tmp_path):
    from autolander.failure import TRANSITION_LOG, MarkerStore
    from autolander.gerrit import GerritError
    from autolander.loop import KIND_SINGLE, Candidate, WipChain, _drive_candidate

    # not landable (submittable False) -> the drive rebases; the rebase 409s (textual conflict)
    c = change_info("Icon", 700, submittable=False, revision="sC")
    client = RecordingClient(
        changes={"Icon": c}, rebase_error=GerritError(409, "conflict during rebase")
    )
    wip = WipChain(change_id="Icon", chain_member_ids=["Icon"])
    cand = Candidate(
        change_id="Icon", number=700, autosubmit_date="t", kind=KIND_SINGLE, member_ids=["Icon"]
    )
    store = MarkerStore(tmp_path)

    _drive_candidate(client, wip, cand, store, tmp_path)  # must NOT raise

    # needs_rebase recorded + Autosubmit removed from the stack (handle_rebase_conflict ran)
    assert store.get_valid(client, "Icon") is not None
    assert any(
        c[0] == "set_review" and (c[2]["labels"] or {}).get("Autosubmit") == 0 for c in client.calls
    )
    assert (tmp_path / TRANSITION_LOG).exists()
    assert not any(c[0] == "submit" for c in client.calls), "never submit on a rebase conflict"


def test_terminal_verified_waits_through_transient_zero():
    from autolander.loop import WipChain, _terminal_verified_vote

    # clear-vote resets Verified->0 (transient), THEN CI casts the terminal +1
    running = change_info("Iw", 701, verified=False)  # Verified all[value 0], no approved/rejected
    passed = change_info("Iw", 701, verified=True)
    client = RecordingClient(change_seq={"Iw": [running, passed]})
    wip = WipChain(change_id="Iw", chain_member_ids=["Iw"])

    ticks = iter([0, 0, 10, 20, 30])
    vote = _terminal_verified_vote(
        client,
        wip,
        time_fn=lambda: next(ticks, 999),
        sleep_fn=lambda _s: None,
        timeout_s=1800,
        poll_s=15,
    )
    assert vote == 1, (
        "a transient Verified=0 must be treated as recheck-running, then +1 on terminal"
    )


def test_terminal_verified_times_out_to_minus1():
    from autolander.loop import WipChain, _terminal_verified_vote

    stuck = change_info("Ih", 702, verified=False)  # never terminal
    client = RecordingClient(changes={"Ih": stuck})
    wip = WipChain(change_id="Ih", chain_member_ids=["Ih"])
    ticks = iter([0, 100_000, 100_000])
    assert (
        _terminal_verified_vote(
            client,
            wip,
            time_fn=lambda: next(ticks, 100_000),
            sleep_fn=lambda _s: None,
            timeout_s=1800,
        )
        == -1
    )
