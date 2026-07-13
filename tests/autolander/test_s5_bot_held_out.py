"""S5c/S5d HELD-OUT oracle (withheld from the implementer): heartbeat-absent, the
emergency-stop sentinel, the AUTOLANDER_HANDBACK marker, and the recovery snapshot
round-trip. (Crash-recovery reconciliation is in test_crash_recovery.py.)"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.unit


def test_heartbeat_age_is_huge_when_absent(tmp_path):
    from autolander.loop import heartbeat_age_s

    # no heartbeat written yet -> must read as very stale (so lander_down trips), not 0
    assert heartbeat_age_s(tmp_path, now=1000.0) > 90


def test_emergency_stop_sentinel(tmp_path):
    from autolander.loop import EMERGENCY_STOP_FILE, is_emergency_stopped

    assert is_emergency_stopped(tmp_path) is False
    (tmp_path / EMERGENCY_STOP_FILE).write_text("stop")
    assert is_emergency_stopped(tmp_path) is True


def test_handback_marker_format(capsys):
    from autolander.loop import MARKER_HANDBACK, emit_marker

    emit_marker(MARKER_HANDBACK, {"stack_id": "topic-x", "reason": "needs_rebase"})
    line = capsys.readouterr().out.strip()
    token, _, payload = line.partition(" ")
    assert token == MARKER_HANDBACK
    assert json.loads(payload)["reason"] == "needs_rebase"


def test_recovery_snapshot_round_trip(tmp_path):
    from autolander.loop import WipChain, load_recovery, write_recovery

    wip = WipChain(
        change_id="Itip",
        chain_member_ids=["Ibot", "Itip"],
        tested_shas={"Ibot": "sB", "Itip": "sT"},
        phase="submitting",
        re_drive_count=2,
    )
    write_recovery(tmp_path, wip)
    restored, acknowledged = load_recovery(tmp_path)

    assert restored is not None
    assert acknowledged is True  # default (no hand-back in progress)
    assert restored.change_id == "Itip"
    assert restored.chain_member_ids == ["Ibot", "Itip"]
    assert restored.tested_shas == {"Ibot": "sB", "Itip": "sT"}
    assert restored.phase == "submitting"
    assert restored.re_drive_count == 2


def test_load_recovery_none_when_absent(tmp_path):
    from autolander.loop import load_recovery

    assert load_recovery(tmp_path) is None
