"""S2b HELD-OUT oracle (withheld from the implementer): the FFO TOCTOU guard, bounded
re-drive, the not-fast-forward submit refusal, and the stack hand-back. Observable behaviour
only — recorded client calls, outcome strings, re_drive_count, and the injected hooks."""

from __future__ import annotations

import pytest
from _fakes import RecordingClient, change_info

pytestmark = pytest.mark.unit


def _rec_rebase():
    calls = []

    def rebase(client, wip):
        calls.append(wip.change_id)

    rebase.calls = calls
    return rebase


def _await_sets_tested(client, wip):
    """Model S2c: after a re-drive, record the (now) current sha as the tested sha."""
    for mid in wip.chain_member_ids:
        cur = client.get_change(mid).get("current_revision")
        wip.tested_shas[mid] = cur


# --- is_landable (TOCTOU) -------------------------------------------------
def test_is_landable_false_when_not_submittable():
    from autolander.loop import WipChain, is_landable

    c = change_info("Ins", 301, autosubmit_date="t", submittable=False)
    client = RecordingClient(changes={"Ins": c})
    wip = WipChain(
        change_id="Ins", chain_member_ids=["Ins"], tested_shas={"Ins": c["current_revision"]}
    )
    assert is_landable(client, wip) is False


def test_is_landable_false_on_sha_mismatch():
    from autolander.loop import WipChain, is_landable

    c = change_info(
        "Ism", 302, autosubmit_date="t", submittable=True
    )  # current_revision == "rev302"
    client = RecordingClient(changes={"Ism": c})
    wip = WipChain(change_id="Ism", chain_member_ids=["Ism"], tested_shas={"Ism": "STALE_SHA"})
    assert is_landable(client, wip) is False


# --- bounded re-drive -----------------------------------------------------
def test_redrive_then_land():
    from autolander.loop import WipChain, drive_to_submit

    bad = change_info("Icd", 303, autosubmit_date="t", submittable=False)
    good = change_info("Icd", 303, autosubmit_date="t", submittable=True)
    client = RecordingClient(change_seq={"Icd": [bad, good]})
    wip = WipChain(
        change_id="Icd", chain_member_ids=["Icd"], tested_shas={"Icd": good["current_revision"]}
    )
    rebase = _rec_rebase()

    outcome = drive_to_submit(client, wip, rebase=rebase, await_verified=_await_sets_tested)

    assert outcome == "submitted"
    assert wip.re_drive_count == 1
    assert rebase.calls == ["Icd"], "one re-drive rebase before landing"
    assert any(call[0] == "submit" for call in client.calls)


def test_exhaustion_hands_back():
    from autolander.loop import HANDBACK_NEEDS_REBASE, MAX_RE_DRIVE, WipChain, drive_to_submit

    stuck = change_info("Istuck", 304, autosubmit_date="t", submittable=False)  # never landable
    client = RecordingClient(changes={"Istuck": stuck})
    wip = WipChain(
        change_id="Istuck",
        chain_member_ids=["Istuck"],
        tested_shas={"Istuck": stuck["current_revision"]},
    )
    handbacks = []

    outcome = drive_to_submit(
        client,
        wip,
        rebase=lambda c, w: None,
        await_verified=lambda c, w: None,
        record_handback=lambda reason, w: handbacks.append((reason, w.change_id)),
    )

    assert outcome == "handed_back"
    assert wip.re_drive_count == MAX_RE_DRIVE
    assert not any(call[0] == "submit" for call in client.calls), (
        "must NEVER submit a non-landable stack"
    )
    # hand-back: Autosubmit removed (vote 0) + a comment, and S3's marker hook fired
    sr = [call for call in client.calls if call[0] == "set_review" and call[1] == "Istuck"]
    assert sr, "hand-back must remove Autosubmit via set_review"
    labels = sr[-1][2]["labels"] or {}
    assert labels.get("Autosubmit") == 0
    assert sr[-1][2]["message"], "hand-back posts a comment naming the reason"
    assert handbacks == [(HANDBACK_NEEDS_REBASE, "Istuck")]


def test_not_fast_forward_submit_refusal_redrives():
    from autolander.gerrit import GerritError
    from autolander.loop import WipChain, drive_to_submit

    c = change_info("Iff", 305, autosubmit_date="t", submittable=True)
    client = RecordingClient(
        changes={"Iff": c},
        submit_error=GerritError(409, "change is not fast-forward; rebase and try again"),
    )
    wip = WipChain(
        change_id="Iff", chain_member_ids=["Iff"], tested_shas={"Iff": c["current_revision"]}
    )
    rebase = _rec_rebase()

    outcome = drive_to_submit(client, wip, rebase=rebase, await_verified=_await_sets_tested)

    assert outcome == "submitted", "a not-fast-forward refusal must trigger a re-drive, not a crash"
    assert rebase.calls == ["Iff"]
    assert wip.re_drive_count == 1
    assert len([call for call in client.calls if call[0] == "submit"]) == 2


# --- hand-back mechanics --------------------------------------------------
def test_hand_back_removes_label_and_comments_every_member():
    from autolander.loop import HANDBACK_NEEDS_REBASE, WipChain, hand_back

    client = RecordingClient()
    wip = WipChain(change_id="Itop", chain_member_ids=["Ibot", "Imid", "Itop"])
    calls = []

    hand_back(client, wip, record_handback=lambda reason, w: calls.append(reason))

    removed = {
        c[1]
        for c in client.calls
        if c[0] == "set_review" and (c[2]["labels"] or {}).get("Autosubmit") == 0
    }
    assert removed == {"Ibot", "Imid", "Itop"}, (
        "Autosubmit removed from EVERY member (never partial)"
    )
    assert calls == [HANDBACK_NEEDS_REBASE]
