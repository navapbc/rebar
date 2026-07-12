"""S2b happy-path (the ONLY tests the implementer sees): a landable wipChain submits on the
first try. Re-drive / TOCTOU / hand-back edge cases are held out."""

from __future__ import annotations

import pytest
from _fakes import RecordingClient, change_info

pytestmark = pytest.mark.unit


def _noop(client, wip):  # injected rebase / await_verified that should NOT be needed here
    raise AssertionError("re-drive must not happen for an already-landable wip")


def test_landable_wip_submits_first_try():
    from autolander.loop import WipChain, drive_to_submit

    c = change_info("Ilandable", 200, autosubmit_date="t", submittable=True)
    client = RecordingClient(changes={"Ilandable": c})
    wip = WipChain(
        change_id="Ilandable",
        chain_member_ids=["Ilandable"],
        tested_shas={"Ilandable": c["current_revision"]},
    )

    outcome = drive_to_submit(client, wip, rebase=_noop, await_verified=_noop)

    assert outcome == "submitted"
    assert any(call[0] == "submit" and call[1] == "Ilandable" for call in client.calls)
    assert wip.re_drive_count == 0


def test_is_landable_true_when_submittable_and_sha_unchanged():
    from autolander.loop import WipChain, is_landable

    c = change_info("Ix", 201, autosubmit_date="t", submittable=True)
    client = RecordingClient(changes={"Ix": c})
    wip = WipChain(
        change_id="Ix", chain_member_ids=["Ix"], tested_shas={"Ix": c["current_revision"]}
    )

    assert is_landable(client, wip) is True
