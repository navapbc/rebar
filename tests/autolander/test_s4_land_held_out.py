"""S4 HELD-OUT oracle (withheld from the implementer): the heartbeat-first liveness gate
(stale/unreachable → lander_down, no label set), the full fallback-precedence rule, the
--wait timeout, ABANDONED, and land-status pending/not_requested. Observable behaviour only."""

from __future__ import annotations

import pytest
from _autolander_fakes import RecordingClient, change_info

pytestmark = pytest.mark.unit


def _reader(age):
    return lambda: {"heartbeat_age_s": age}


def _unreachable():
    raise ConnectionError("status endpoint refused")


# --- heartbeat-first liveness gate ----------------------------------------
def test_stale_heartbeat_returns_lander_down_without_setting_label():
    from autolander import land as L

    client = RecordingClient(changes={"Ix": change_info("Ix", 800)})
    set_calls = []
    outcome, detail = L.land(
        "Ix",
        gerrit=client,
        status_reader=_reader(200),  # > 90s stale
        marker_lookup=lambda c: None,
        set_autosubmit=lambda c: set_calls.append(c),
        clock=lambda: 0,
        sleep=lambda _s: None,
    )
    assert outcome == L.LANDER_DOWN
    assert set_calls == [], "must NOT set Autosubmit when the bot is already down"


def test_unreachable_status_endpoint_is_lander_down_not_error():
    from autolander import land as L

    client = RecordingClient(changes={"Ix": change_info("Ix", 801)})
    outcome, _ = L.land(
        "Ix",
        gerrit=client,
        status_reader=_unreachable,
        marker_lookup=lambda c: None,
        set_autosubmit=lambda c: None,
        clock=lambda: 0,
        sleep=lambda _s: None,
    )
    assert outcome == L.LANDER_DOWN


# --- fallback precedence --------------------------------------------------
def test_precedence_marker_needs_rebase_wins():
    from autolander import land as L

    # native looks like a CI fail, but a valid S3 needs_rebase marker WINS
    native = change_info("Ip", 802, verified=False)
    native["labels"]["Verified"] = {"rejected": {"_account_id": 2000}, "all": [{"value": -1}]}
    assert L.derive_outcome(native, object()) == L.NEEDS_REBASE


def test_precedence_native_ci_and_review():
    from autolander import land as L

    ci = change_info("Ic", 803)
    ci["labels"]["Verified"] = {"rejected": {}, "all": [{"value": -1}]}
    assert L.derive_outcome(ci, None) == L.CI_FAILED

    rv = change_info("Ir", 804)
    rv["labels"]["LLM-Review"] = {"rejected": {}, "all": [{"value": -1}]}
    assert L.derive_outcome(rv, None) == L.REVIEW_FAILED


def test_precedence_both_minus1_ci_failed_wins():
    from autolander import land as L

    both = change_info("Ib", 805)
    both["labels"]["Verified"] = {"rejected": {}, "all": [{"value": -1}]}
    both["labels"]["LLM-Review"] = {"rejected": {}, "all": [{"value": -1}]}
    assert L.derive_outcome(both, None) == L.CI_FAILED


def test_abandoned_maps_to_abandoned():
    from autolander import land as L

    assert L.derive_outcome(change_info("Ia", 806, status="ABANDONED"), None) == L.ABANDONED
    assert L.exit_code_for(L.ABANDONED) == 5


def test_not_yet_terminal_returns_none():
    from autolander import land as L

    assert L.derive_outcome(change_info("Iopen", 807), None) is None


# --- --wait timeout -------------------------------------------------------
def test_wait_times_out_when_never_terminal():
    from autolander import land as L

    client = RecordingClient(changes={"Iw": change_info("Iw", 808, autosubmit_date="t")})
    ticks = iter([0, 0, 100, 100_000, 100_000, 100_000])
    outcome, _ = L.land(
        "Iw",
        gerrit=client,
        status_reader=_reader(5),
        marker_lookup=lambda c: None,
        set_autosubmit=lambda c: None,
        clock=lambda: next(ticks),
        sleep=lambda _s: None,
        poll_s=30,
        timeout_s=1800,
    )
    assert outcome == L.TIMED_OUT
    assert L.exit_code_for(L.TIMED_OUT) == 124


# --- land-status ----------------------------------------------------------
def test_land_status_not_requested_on_untouched_change():
    from autolander import land as L

    client = RecordingClient(changes={"Iu": change_info("Iu", 809)})  # no Autosubmit, open
    outcome, _ = L.land_status(
        "Iu", gerrit=client, status_reader=_reader(5), marker_lookup=lambda c: None
    )
    assert outcome == L.NOT_REQUESTED
    assert L.exit_code_for(L.NOT_REQUESTED) == 4


def test_land_status_pending_while_bot_driving():
    from autolander import land as L

    # Autosubmit set + not yet terminal -> pending
    client = RecordingClient(changes={"Id": change_info("Id", 810, autosubmit_date="t")})
    outcome, _ = L.land_status(
        "Id", gerrit=client, status_reader=_reader(5), marker_lookup=lambda c: None
    )
    assert outcome == L.PENDING
    assert L.exit_code_for(L.PENDING) == 75


def test_help_cites_the_contract_doc():
    from autolander import land as L

    parser = L.build_parser()
    assert "land-contract" in parser.format_help() or "land-contract" in (parser.epilog or "")
