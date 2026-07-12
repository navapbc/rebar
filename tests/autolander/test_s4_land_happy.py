"""S4 happy-path (the ONLY tests the implementer sees): the exit-code table, the two
simplest derive_outcome cases, and a fresh-heartbeat land that sets Autosubmit and merges.
Liveness / precedence / timeout / land-status edges are held out."""

from __future__ import annotations

import pytest
from _autolander_fakes import RecordingClient, change_info

pytestmark = pytest.mark.unit


def _fresh_status():
    return {"heartbeat_age_s": 5}


def test_exit_code_table_is_complete_and_pinned():
    from autolander import land as L

    for outcome, code in {
        L.MERGED: 0,
        L.NEEDS_REBASE: 1,
        L.CI_FAILED: 2,
        L.REVIEW_FAILED: 3,
        L.NOT_REQUESTED: 4,
        L.ABANDONED: 5,
        L.LANDER_DOWN: 6,
        L.ERROR: 7,
        L.PENDING: 75,
        L.TIMED_OUT: 124,
    }.items():
        assert L.EXIT_CODES[outcome] == code
        assert L.exit_code_for(outcome) == code


def test_derive_outcome_merged_and_needs_rebase():
    from autolander import land as L

    merged = change_info("Im", 700, status="MERGED")
    assert L.derive_outcome(merged, None) == L.MERGED
    # a valid S3 needs_rebase marker wins and is NOT derivable from native state
    assert L.derive_outcome(change_info("In", 701), object()) == L.NEEDS_REBASE


def test_land_fresh_heartbeat_sets_autosubmit_and_returns_merged():
    from autolander import land as L

    merged = change_info("Ilm", 702, autosubmit_date="t", status="MERGED")
    client = RecordingClient(changes={"Ilm": merged})
    set_calls = []
    outcome, _detail = L.land(
        "Ilm",
        gerrit=client,
        status_reader=_fresh_status,
        marker_lookup=lambda c: None,
        set_autosubmit=lambda c: set_calls.append(c),
        clock=lambda: 0,
        sleep=lambda _s: None,
    )
    assert outcome == L.MERGED
    assert set_calls == ["Ilm"], "a fresh heartbeat sets Autosubmit under the agent's identity"
