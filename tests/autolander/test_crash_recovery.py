"""S5d crash-recovery / idempotency oracle (HELD OUT): a bot killed mid-flight must, on
restart, reconcile its recovery.json against LIVE Gerrit before resuming — never
double-submit an already-merged stack, never strand one on a stale snapshot."""

from __future__ import annotations

import json
import threading
import time
import urllib.request

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


def test_heartbeat_freshness_through_status_endpoint_and_land(tmp_path):
    """END-TO-END heartbeat liveness: a fresh heartbeat is served as `heartbeat_age_s` through
    the REAL status HTTP server (GET /autolander/status) — the same document `land` reads — so a
    concurrent `land` sees the bot as live (proceeds to set Autosubmit). When the heartbeat
    writer stalls past 90 s, the SAME endpoint reports the container UNHEALTHY (healthcheck_ok
    False, matching the Dockerfile HEALTHCHECK) AND a concurrent `land` returns LANDER_DOWN."""
    from autolander import land
    from autolander.loop import healthcheck_ok, make_status_server, write_heartbeat

    write_heartbeat(tmp_path, time.time())  # fresh
    state = {"wip": None, "phase_since": time.monotonic()}
    httpd = make_status_server(state, RecordingClient(changes={}), tmp_path, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    def status_reader():
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/autolander/status", timeout=5
        ) as resp:
            return json.loads(resp.read().decode())

    try:
        # --- fresh: endpoint reports a young heartbeat; healthcheck healthy; land is live ---
        assert status_reader()["heartbeat_age_s"] < 15
        assert healthcheck_ok(tmp_path, time.time()) is True
        assert land.heartbeat_fresh(status_reader) is True

        # A concurrent land against a live bot PROCEEDS: it sets Autosubmit (records "Ix") and
        # reaches a terminal outcome (the change is already MERGED) rather than LANDER_DOWN.
        sa: list[str] = []
        merged = RecordingClient(
            changes={"Ix": change_info("Ix", 7, status="MERGED", revision="sx")}
        )
        outcome, _ = land.land(
            "Ix",
            gerrit=merged,
            status_reader=status_reader,
            marker_lookup=lambda _c: None,
            set_autosubmit=lambda c: sa.append(c),
            clock=lambda: 0.0,
            sleep=lambda _s: None,
        )
        assert sa == ["Ix"], "a live bot => land proceeds to set Autosubmit"
        assert outcome == land.MERGED and outcome != land.LANDER_DOWN

        # --- stalled writer: backdate the heartbeat past the 90 s bound ---
        write_heartbeat(tmp_path, time.time() - 100)
        assert status_reader()["heartbeat_age_s"] >= 90
        assert healthcheck_ok(tmp_path, time.time()) is False  # HEALTHCHECK -> unhealthy
        assert land.heartbeat_fresh(status_reader) is False

        # A concurrent land now reports LANDER_DOWN (returns before touching gerrit/clock).
        down_sa: list[str] = []
        outcome_down, _ = land.land(
            "Ix",
            gerrit=RecordingClient(changes={}),
            status_reader=status_reader,
            marker_lookup=lambda _c: None,
            set_autosubmit=lambda c: down_sa.append(c),
            clock=lambda: 0.0,
            sleep=lambda _s: None,
        )
        assert outcome_down == land.LANDER_DOWN
        assert down_sa == [], "a down bot => land never orphans an Autosubmit label"
    finally:
        httpd.shutdown()


def test_drain_then_autoheal_restart_midway_recovers_cleanly(tmp_path):
    """SIGTERM drain ⇄ autoheal race: handle_sigterm writes recovery.json FIRST (before draining),
    so a `docker restart` (hard kill) mid-drain leaves a durable snapshot. On restart,
    reconcile_recovery reconciles it against live Gerrit — an interrupted submit (still NEW)
    re-selects with NO blind re-submit, and a submit that actually MERGED mid-interruption is
    simply cleared. Neither path double-submits or strands the stack."""
    from autolander.loop import (
        RECOVERY_FILE,
        WipChain,
        handle_sigterm,
        reconcile_recovery,
    )

    wip = WipChain(
        change_id="It",
        chain_member_ids=["Ib", "It"],
        tested_shas={"It": "sT", "Ib": "sB"},
        phase="submitting",
    )
    state = {"wip": wip, "phase_since": 0.0}
    stopping = {"v": False, "drain_deadline": None}

    # SIGTERM: recovery snapshot is durable IMMEDIATELY (written FIRST), THEN drain begins.
    handle_sigterm(state, stopping, tmp_path, now=0.0)
    assert (tmp_path / RECOVERY_FILE).exists(), "recovery.json written FIRST, before draining"
    assert stopping["v"] is True
    assert stopping["drain_deadline"] == 120

    # --- autoheal `docker restart` mid-drain (hard kill): drain never finished. On restart the
    # tip is STILL open (submit never completed) -> re-select, never blind re-submit. ---
    client = RecordingClient(
        changes={
            "It": change_info("It", 912, status="NEW", revision="sT", submittable=True),
            "Ib": change_info("Ib", 911, status="NEW", revision="sB"),
        }
    )
    disp = reconcile_recovery(client, tmp_path)
    assert "submitting" in disp
    assert not any(c[0] == "submit" for c in client.calls), "never blind re-submit on restart"
    assert not (tmp_path / RECOVERY_FILE).exists(), "recovery cleared after reconcile"

    # --- benign race: the tip actually MERGED during the interrupted submit. Re-snapshot (the
    # in-flight wip is unchanged) and reconcile -> cleared as merged, still NO submit call. ---
    handle_sigterm(state, stopping, tmp_path, now=0.0)
    assert (tmp_path / RECOVERY_FILE).exists()
    merged_client = RecordingClient(
        changes={
            "It": change_info("It", 912, status="MERGED", revision="sT"),
            "Ib": change_info("Ib", 911, status="NEW", revision="sB"),
        }
    )
    disp_merged = reconcile_recovery(merged_client, tmp_path)
    assert "merged" in disp_merged.lower()
    assert not any(c[0] == "submit" for c in merged_client.calls)
    assert not (tmp_path / RECOVERY_FILE).exists()
