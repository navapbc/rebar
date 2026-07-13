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
