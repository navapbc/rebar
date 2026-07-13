"""S3 HELD-OUT oracle (withheld from the implementer): marker self-invalidation, the
auto-recheck state machine (post-once + both terminal outcomes), CI-fail (strip all, no
comment), label-removal 404/409 no-op, restart re-drive, and stack owner. Observable
behaviour only."""

from __future__ import annotations

import pytest
from _autolander_fakes import RecordingClient, change_info

pytestmark = pytest.mark.unit


def test_marker_self_invalidates_on_new_patchset(tmp_path):
    from autolander.failure import MarkerStore, NeedsRebaseMarker

    # stored marker's SHA is stale vs the change's CURRENT patchset -> ignored
    current = change_info("Ic", 601, revision="shaNEW")
    client = RecordingClient(changes={"Ic": current})
    store = MarkerStore(tmp_path)
    store.upsert(
        NeedsRebaseMarker(change_id="Ic", patchset_sha="shaOLD", stack_id="Ic", change_ids=["Ic"])
    )

    assert store.get_valid(client, "Ic") is None, "a new patchset means the rebase was resolved"


def test_auto_recheck_posts_recheck_once_and_passes_on_plus1():
    from autolander.failure import OUTCOME_VERIFIED, RECHECK_COMMENT, auto_recheck
    from autolander.loop import WipChain

    c = change_info("Ir", 602, verified=False)
    client = RecordingClient(changes={"Ir": c})
    wip = WipChain(change_id="Ir", chain_member_ids=["Ir"])

    outcome = auto_recheck(client, wip, await_terminal_verified=lambda cl, w: 1)

    assert outcome == OUTCOME_VERIFIED
    assert wip.rechecking is False, "flag cleared on a passing recheck"
    rechecks = [
        c for c in client.calls if c[0] == "set_review" and c[2].get("message") == RECHECK_COMMENT
    ]
    assert len(rechecks) == 1, "post `recheck` exactly ONCE per patchset"


def test_auto_recheck_reports_ci_failed_on_minus1():
    from autolander.failure import OUTCOME_CI_FAILED, auto_recheck
    from autolander.loop import WipChain

    c = change_info("Ir2", 603, verified=False)
    client = RecordingClient(changes={"Ir2": c})
    wip = WipChain(change_id="Ir2", chain_member_ids=["Ir2"])

    assert auto_recheck(client, wip, await_terminal_verified=lambda cl, w: -1) == OUTCOME_CI_FAILED


def test_handle_ci_fail_strips_all_members_and_posts_no_comment():
    from autolander.failure import OUTCOME_CI_FAILED, handle_ci_fail
    from autolander.loop import WipChain

    client = RecordingClient(changes={"Ib": change_info("Ib", 604), "It": change_info("It", 605)})
    wip = WipChain(change_id="It", chain_member_ids=["Ib", "It"])

    outcome = handle_ci_fail(client, wip)

    assert outcome == OUTCOME_CI_FAILED
    removed = {
        c[1]
        for c in client.calls
        if c[0] == "set_review" and (c[2]["labels"] or {}).get("Autosubmit") == 0
    }
    assert removed == {"Ib", "It"}
    # NO bot comment on CI-fail (native -1 + run logs are the signal)
    assert all((c[2].get("message") in (None, "")) for c in client.calls if c[0] == "set_review")


def test_label_removal_treats_404_as_noop():
    from autolander.failure import remove_autosubmit_from_stack
    from autolander.gerrit import GerritError
    from autolander.loop import WipChain

    client = RecordingClient(
        changes={"Ia": change_info("Ia", 606), "Ib": change_info("Ib", 607)},
        set_review_errors={"Ia": GerritError(404, "not found")},
    )
    wip = WipChain(change_id="Ib", chain_member_ids=["Ia", "Ib"])

    remove_autosubmit_from_stack(client, wip)  # must NOT raise despite Ia's 404

    # it still attempted removal on BOTH members (404 on one does not abort the rest)
    attempted = {c[1] for c in client.calls if c[0] == "set_review"}
    assert attempted == {"Ia", "Ib"}


def test_reconcile_on_restart_redrives_unacknowledged(tmp_path):
    from autolander.failure import MarkerStore, NeedsRebaseMarker, reconcile_on_restart

    current = change_info("Iu", 608, revision="shaU")
    client = RecordingClient(changes={"Iu": current})
    store = MarkerStore(tmp_path)
    store.upsert(
        NeedsRebaseMarker(
            change_id="Iu",
            patchset_sha="shaU",
            stack_id="Iu",
            change_ids=["Iu"],
            acknowledged=False,
        )
    )

    redriven = reconcile_on_restart(client, store)

    assert "Iu" in redriven
    assert any(
        c[0] == "set_review" and (c[2]["labels"] or {}).get("Autosubmit") == 0 for c in client.calls
    )
    assert store.unacknowledged() == [], "all markers acknowledged after restart re-drive"


def test_stack_owner_account_reads_tip_owner():
    from autolander.failure import stack_owner_account
    from autolander.loop import WipChain

    tip = change_info("Itip", 609, owner_account=4242)
    client = RecordingClient(changes={"Itip": tip})
    wip = WipChain(change_id="Itip", chain_member_ids=["Ibot", "Itip"])

    assert stack_owner_account(client, wip) == 4242


def test_rolling_transition_log_and_handback_marker(tmp_path, capsys):
    """A rolling per-transition log line is appended (debug trail) AND an AUTOLANDER_HANDBACK
    marker is emitted, even though the authoritative needs_rebase record is single-slot."""
    from autolander.failure import TRANSITION_LOG, MarkerStore, handle_rebase_conflict
    from autolander.loop import MARKER_HANDBACK, WipChain

    client = RecordingClient(changes={"Itip": change_info("Itip", 550, revision="sT")})
    wip = WipChain(change_id="Itip", chain_member_ids=["Itip"])
    store = MarkerStore(tmp_path)

    handle_rebase_conflict(client, wip, store, stack_id="topic-y", now="2026-07-13")

    log = (tmp_path / TRANSITION_LOG).read_text()
    assert "needs_rebase" in log and "Itip" in log, "a rolling per-transition line is appended"
    assert MARKER_HANDBACK in capsys.readouterr().out, "hand-back emits the observability marker"
