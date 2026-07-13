"""S2b HELD-OUT oracle (withheld from the implementer): the FFO TOCTOU guard, bounded
re-drive, the not-fast-forward submit refusal, and the stack hand-back. Observable behaviour
only — recorded client calls, outcome strings, re_drive_count, and the injected hooks."""

from __future__ import annotations

import pytest
from _autolander_fakes import RecordingClient, change_info

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


# --- FIX 15a1: the FFO submit-reject predicate + routing ---------------------
def test_is_fast_forward_reject_matches_both_wordings():
    from autolander.gerrit import GerritError
    from autolander.loop import _is_fast_forward_reject

    gerrit_submit = GerritError(
        409,
        "Failed to submit 2 changes ... Project policy requires all submissions to be a "
        "fast-forward. Please rebase the change locally and upload again for review.",
    )
    git_push = GerritError(409, "change is not fast-forward; rebase and try again")
    unrelated = GerritError(409, "conflict: merge failed")

    assert _is_fast_forward_reject(gerrit_submit) is True, "Gerrit FFO submit refusal"
    assert _is_fast_forward_reject(git_push) is True, "git push wording"
    assert _is_fast_forward_reject(unrelated) is False, "unrelated 409 must not match"


# The REAL Gerrit FFO SUBMIT refusal — deliberately WITHOUT the substring "not fast-forward"
# (that is git's PUSH wording). Bug 15a1: the old guard only matched "not fast-forward", so
# this real message was re-raised, looped as AUTOLANDER_ERROR, and re-selected forever.
_GERRIT_FFO_SUBMIT_MSG = (
    "Failed to submit 2 changes ... Project policy requires all submissions to be a "
    "fast-forward. Please rebase the change locally and upload again for review."
)
_GIT_PUSH_MSG = "change is not fast-forward; rebase and try again"


@pytest.mark.parametrize("message", [_GERRIT_FFO_SUBMIT_MSG, _GIT_PUSH_MSG])
def test_drive_candidate_routes_ffo_submit_reject_to_rebase(tmp_path, capsys, message):
    from autolander.failure import MarkerStore
    from autolander.gerrit import GerritError
    from autolander.loop import Candidate, WipChain, _drive_candidate

    # A single on-tip, fresh-Verified change; the FIRST submit refuses with the FFO message,
    # forcing the TOCTOU rebase path; the re-driven submit then lands it (RecordingClient raises
    # submit_error ONCE, then succeeds).
    new = change_info("Iff", 705, autosubmit_date="t", submittable=True, verified=True)
    merged = change_info(
        "Iff", 705, autosubmit_date="t", submittable=True, verified=True, status="MERGED"
    )
    client = RecordingClient(
        change_seq={"Iff": [new, merged]}, submit_error=GerritError(409, message)
    )
    wip = WipChain(change_id="Iff", chain_member_ids=["Iff"])
    cand = Candidate(
        change_id="Iff", number=705, autosubmit_date="t", kind="single", member_ids=["Iff"]
    )

    _drive_candidate(client, wip, cand, MarkerStore(tmp_path), tmp_path)

    # routed to a rebase (the FFO refusal is NOT re-raised as a hard error)...
    assert ("rebase", "Iff", {"on_behalf_of_uploader": True}) in client.mutating_calls()
    # ...the submit was retried after the rebase (2 attempts: the refusal + the landing)...
    assert len([c for c in client.calls if c[0] == "submit"]) == 2
    # ...and nothing was emitted as a high-visibility error.
    assert "AUTOLANDER_ERROR" not in capsys.readouterr().err
