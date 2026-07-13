"""S5d crash-recovery / idempotency oracle (HELD OUT): a bot killed mid-flight must, on
restart, reconcile its recovery.json against LIVE Gerrit before resuming — never
double-submit an already-merged stack, never strand one on a stale snapshot."""

from __future__ import annotations

import pytest
from _autolander_fakes import RecordingClient, change_info

pytestmark = pytest.mark.unit


def test_reconcile_none_when_no_recovery(tmp_path):
    from autolander.loop import reconcile_recovery

    client = RecordingClient(changes={})
    assert reconcile_recovery(client, tmp_path) is None


def test_reconcile_clears_when_tip_already_merged(tmp_path):
    """Killed during/after submit: the tip is already MERGED on restart -> clear recovery,
    NEVER re-submit (idempotent)."""
    from autolander.loop import RECOVERY_FILE, WipChain, reconcile_recovery, write_recovery

    merged = change_info("Itip", 900, status="MERGED", revision="sT")
    client = RecordingClient(changes={"Itip": merged})
    wip = WipChain(
        change_id="Itip", chain_member_ids=["Itip"], tested_shas={"Itip": "sT"}, phase="submitting"
    )
    write_recovery(tmp_path, wip)

    disposition = reconcile_recovery(client, tmp_path)

    assert disposition is not None and "merged" in disposition.lower()
    # NEVER re-submit an already-merged stack
    assert not any(c[0] == "submit" for c in client.calls)
    # recovery cleared (nothing to resume)
    assert not (tmp_path / RECOVERY_FILE).exists()


def test_reconcile_discards_on_sha_drift(tmp_path):
    """Killed mid-rebase and main moved: the recorded tested SHA no longer matches the
    change's current patchset -> discard the snapshot and re-select (do not resume a stale
    tree). Recovery is cleared; no submit."""
    from autolander.loop import RECOVERY_FILE, WipChain, reconcile_recovery, write_recovery

    drifted = change_info("Itip", 901, status="NEW", revision="sNEW")  # current SHA != recorded
    client = RecordingClient(changes={"Itip": drifted})
    wip = WipChain(
        change_id="Itip",
        chain_member_ids=["Itip"],
        tested_shas={"Itip": "sOLD"},
        phase="awaiting_verified",
    )
    write_recovery(tmp_path, wip)

    disposition = reconcile_recovery(client, tmp_path)

    assert disposition is not None and "merged" not in disposition.lower()
    assert not any(c[0] == "submit" for c in client.calls)
    assert not (tmp_path / RECOVERY_FILE).exists(), "stale snapshot discarded"


@pytest.mark.parametrize("phase", ["rebasing", "awaiting_verified", "submitting", "selecting"])
def test_reconcile_reselects_per_phase_when_interrupted(tmp_path, phase):
    """Killed mid-drive at any pre-terminal phase (SHA still current, not merged): discard the
    snapshot and re-select — never blind-resubmit, never strand."""
    from autolander.loop import RECOVERY_FILE, WipChain, reconcile_recovery, write_recovery

    open_change = change_info("Iph", 910, status="NEW", revision="sPH", submittable=True)
    client = RecordingClient(changes={"Iph": open_change})
    wip = WipChain(
        change_id="Iph", chain_member_ids=["Iph"], tested_shas={"Iph": "sPH"}, phase=phase
    )
    write_recovery(tmp_path, wip, acknowledged=True)

    disp = reconcile_recovery(client, tmp_path)

    assert phase in disp and "merged" not in disp.lower()
    assert not any(c[0] == "submit" for c in client.calls), "never blind re-submit on restart"
    assert not (tmp_path / RECOVERY_FILE).exists()


def test_reconcile_completes_unacknowledged_handback(tmp_path):
    """Killed mid-hand-back (Autosubmit removal not finished, acknowledged=False): on restart
    the removal is re-driven to completion for every member (idempotent), then cleared."""
    from autolander.loop import RECOVERY_FILE, WipChain, reconcile_recovery, write_recovery

    bot = change_info("Ib", 911, status="NEW", revision="sB")
    top = change_info("It", 912, status="NEW", revision="sT")
    client = RecordingClient(changes={"Ib": bot, "It": top})
    wip = WipChain(change_id="It", chain_member_ids=["Ib", "It"], phase="submitting")
    write_recovery(tmp_path, wip, acknowledged=False)  # hand-back was in progress

    disp = reconcile_recovery(client, tmp_path)

    removed = {
        c[1]
        for c in client.calls
        if c[0] == "set_review" and (c[2]["labels"] or {}).get("Autosubmit") == 0
    }
    assert removed == {"Ib", "It"}, (
        "unacknowledged hand-back re-drives Autosubmit removal on ALL members"
    )
    assert "hand-back" in disp.lower()
    assert not (tmp_path / RECOVERY_FILE).exists()


def test_heartbeat_freshness_integrated_via_status(tmp_path):
    """Integrated heartbeat-freshness proof: after write_heartbeat, the status document's
    heartbeat_age_s (the field `land`'s lander_down reads) is fresh (< the 90s bound)."""
    from autolander.loop import WipChain, build_status, heartbeat_age_s, write_heartbeat

    write_heartbeat(tmp_path, now=1000.0)
    age = heartbeat_age_s(tmp_path, now=1005.0)
    status = build_status(
        WipChain(change_id="", chain_member_ids=[], phase="idle"),
        heartbeat_age_s=age,
        waiting_count=0,
        time_in_phase_s=5,
    )
    assert status["heartbeat_age_s"] == 5 and status["heartbeat_age_s"] < 90
