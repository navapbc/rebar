"""S5c/S5d happy-path (the ONLY tests the implementer sees): the marker line format, the
heartbeat round-trip, and the status JSON shape. Emergency-stop, recovery/reconcile, and the
heartbeat-absent edge are held out."""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.unit


def test_emit_marker_writes_token_space_json(capsys):
    from autolander.loop import MARKER_ERROR, emit_marker

    emit_marker(MARKER_ERROR, {"change": "Ix", "phase": "submitting"})
    line = capsys.readouterr().out.strip()
    token, _, payload = line.partition(" ")
    assert token == MARKER_ERROR
    assert json.loads(payload) == {"change": "Ix", "phase": "submitting"}


def test_heartbeat_round_trip(tmp_path):
    from autolander.loop import heartbeat_age_s, write_heartbeat

    write_heartbeat(tmp_path, now=1000.0)
    assert heartbeat_age_s(tmp_path, now=1007.0) == 7


def test_build_status_shape():
    from autolander.loop import WipChain, build_status

    wip = WipChain(change_id="Itip", chain_member_ids=["Itip"], phase="submitting")
    s = build_status(wip, heartbeat_age_s=3, waiting_count=2, time_in_phase_s=41)
    assert s["heartbeat_age_s"] == 3
    assert s["waiting_count"] == 2
    assert s["phase"] == "submitting"
    assert s["change"] == "Itip"
