"""Bug dc33 (puny-earthy-wolf): the auto-lander must annotate the rebar ticket named by the
landed commit's `rebar-ticket:` trailer — and NEVER fall back to the Gerrit Change-Id.

Covers the pure trailer parser, the closer's skip-on-missing-trailer behaviour (no
`rebar.comment`, no `AUTOLANDER_ERROR`), and that `ancestor_atomic_submit`'s success path
selects the trailer id off each landed commit."""

from __future__ import annotations

import pytest
from _autolander_fakes import RecordingClient, change_info

pytestmark = pytest.mark.unit


def test_trailer_parsed_case_insensitive_and_last_wins():
    from autolander.loop import ticket_id_from_commit_message

    # canonical trailer
    assert (
        ticket_id_from_commit_message(
            "autolander: dc33: fix\n\nbody\n\nrebar-ticket: 38be-a778-43f5-4a4e\n"
        )
        == "38be-a778-43f5-4a4e"
    )
    # case-insensitive key
    assert (
        ticket_id_from_commit_message("subject\n\nRebar-Ticket: 38be-a778-43f5-4a4e")
        == "38be-a778-43f5-4a4e"
    )
    # last occurrence wins
    assert (
        ticket_id_from_commit_message(
            "subject\n\nrebar-ticket: first-id\nSigned-off-by: x\nrebar-ticket: last-id\n"
        )
        == "last-id"
    )


def test_trailer_absent_returns_none():
    from autolander.loop import ticket_id_from_commit_message

    assert ticket_id_from_commit_message("subject\n\nno trailer here\n") is None
    assert ticket_id_from_commit_message("") is None
    assert ticket_id_from_commit_message(None) is None  # type: ignore[arg-type]


def test_closer_skips_without_trailer_and_never_uses_change_id(monkeypatch, capsys):
    """A landed commit with no `rebar-ticket:` trailer -> the closer must NOT call
    `rebar.comment` (so the Gerrit Change-Id is never used as a ticket id) and must NOT emit
    AUTOLANDER_ERROR (a missing trailer is a low-severity note, not a failure)."""
    import autolander.loop as loop
    from autolander.loop import AUTOLANDER_ERROR, close_ticket_via_rebar

    called: list = []

    class _FakeRebar:
        @staticmethod
        def comment(tid, msg):
            called.append((tid, msg))

    # If close_ticket_via_rebar tried to annotate, it would `import rebar` and call comment.
    monkeypatch.setitem(__import__("sys").modules, "rebar", _FakeRebar())

    close_ticket_via_rebar("I0123abcChangeId", ticket_id=None)

    assert called == [], "must NOT call rebar.comment when there is no trailer"
    err = capsys.readouterr().err
    assert AUTOLANDER_ERROR not in err, "a missing trailer is not an AUTOLANDER_ERROR"
    assert "skipping ticket annotation" in err
    assert loop is not None  # module import sanity


def test_closer_annotates_the_trailer_ticket(monkeypatch):
    """With a trailer + a configured tracker + a successful freshen, the closer annotates THAT
    ticket id (not the Change-Id)."""
    import autolander.loop as loop
    from autolander.loop import close_ticket_via_rebar

    called: list = []

    class _FakeRebar:
        @staticmethod
        def comment(tid, msg):
            called.append((tid, msg))

    monkeypatch.setitem(__import__("sys").modules, "rebar", _FakeRebar())
    monkeypatch.setenv("REBAR_TRACKER_DIR", "/tmp/fake-tracker")
    monkeypatch.setattr(loop, "_freshen_ticket_store", lambda tracker: None)

    close_ticket_via_rebar("I0123abcChangeId", ticket_id="dc33")

    assert len(called) == 1
    assert called[0][0] == "dc33", "must annotate the rebar-ticket trailer id, not the Change-Id"


def test_closer_pages_when_tracker_unset(monkeypatch, capsys):
    """A MISSING tickets store (REBAR_TRACKER_DIR unset) is a real deploy misconfiguration: emit
    AUTOLANDER_ERROR, never freshen, never call rebar.comment (bug dc33 follow-up)."""
    import autolander.loop as loop
    from autolander.loop import AUTOLANDER_ERROR, close_ticket_via_rebar

    called: list = []
    freshened: list = []

    class _FakeRebar:
        @staticmethod
        def comment(tid, msg):
            called.append((tid, msg))

    monkeypatch.setitem(__import__("sys").modules, "rebar", _FakeRebar())
    monkeypatch.delenv("REBAR_TRACKER_DIR", raising=False)
    monkeypatch.setattr(loop, "_freshen_ticket_store", lambda tracker: freshened.append(tracker))

    close_ticket_via_rebar("I0123abcChangeId", ticket_id="dc33")

    assert called == [], "must NOT annotate when the tracker is unset"
    assert freshened == [], "must NOT freshen when the tracker is unset"
    err = capsys.readouterr().err
    assert AUTOLANDER_ERROR in err, "an unset tracker must page"
    assert "not configured" in err


def test_closer_skips_without_paging_when_freshen_raises(monkeypatch, capsys):
    """A transient freshen failure must NOT page (the landing already succeeded and it self-heals
    on the next landing): no rebar.comment, no AUTOLANDER_ERROR — just a warning."""
    import autolander.loop as loop
    from autolander.loop import AUTOLANDER_ERROR, close_ticket_via_rebar

    called: list = []

    class _FakeRebar:
        @staticmethod
        def comment(tid, msg):
            called.append((tid, msg))

    def _boom(tracker):
        raise RuntimeError("network flake")

    monkeypatch.setitem(__import__("sys").modules, "rebar", _FakeRebar())
    monkeypatch.setenv("REBAR_TRACKER_DIR", "/tmp/fake-tracker")
    monkeypatch.setattr(loop, "_freshen_ticket_store", _boom)

    close_ticket_via_rebar("I0123abcChangeId", ticket_id="dc33")

    assert called == [], "must NOT annotate when the freshen failed"
    err = capsys.readouterr().err
    assert AUTOLANDER_ERROR not in err, "a transient refresh failure must not page"
    assert "refresh failed (transient)" in err


def test_closer_pages_when_comment_raises_on_fresh_store(monkeypatch, capsys):
    """A rebar.comment failure on a FRESHENED store is genuine (ticket absent / push perms) and
    must page via AUTOLANDER_ERROR."""
    import autolander.loop as loop
    from autolander.loop import AUTOLANDER_ERROR, close_ticket_via_rebar

    class _FakeRebar:
        @staticmethod
        def comment(tid, msg):
            raise RuntimeError("ticket not found")

    monkeypatch.setitem(__import__("sys").modules, "rebar", _FakeRebar())
    monkeypatch.setenv("REBAR_TRACKER_DIR", "/tmp/fake-tracker")
    monkeypatch.setattr(loop, "_freshen_ticket_store", lambda tracker: None)

    close_ticket_via_rebar("I0123abcChangeId", ticket_id="dc33")

    err = capsys.readouterr().err
    assert AUTOLANDER_ERROR in err, "a comment failure on a fresh store must page"
    assert "dc33" in err


def test_ancestor_atomic_submit_selects_trailer_id_on_merge():
    """The success path fetches each landed commit's message and passes the parsed trailer id
    to the closer — proving the Gerrit Change-Id is never substituted."""
    from autolander.loop import WipChain, ancestor_atomic_submit

    merged = change_info(
        "IchangeIdNotTicket",
        500,
        verified=True,
        status="MERGED",
        message="autolander: land it\n\nrebar-ticket: dc33\n",
    )
    client = RecordingClient(changes={"IchangeIdNotTicket": merged})
    wip = WipChain(
        change_id="IchangeIdNotTicket",
        chain_member_ids=["IchangeIdNotTicket"],
        tested_shas={"IchangeIdNotTicket": merged["current_revision"]},
    )
    seen: list = []

    outcome = ancestor_atomic_submit(
        client, wip, close_ticket=lambda cid, *, ticket_id=None: seen.append((cid, ticket_id))
    )

    assert outcome == "merged"
    assert seen == [("IchangeIdNotTicket", "dc33")], (
        "closer must receive the trailer ticket id, not the Gerrit Change-Id"
    )
