"""Managed locally does not prove a link ever reached the peer. Honor inbound removal
only with positive peer-confirmation evidence; otherwise keep the net-active local
dependency. Tests exercise the real store through the applier."""

from __future__ import annotations

import importlib
import logging
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def apply_records():
    return importlib.import_module("rebar.reconciler.apply_inbound_records")


@pytest.fixture
def pc():
    return importlib.import_module("rebar.reconciler.peer_confirmations")


@pytest.fixture
def store(tmp_path: Path, monkeypatch):
    """A real rebar store with two linked tickets."""
    import rebar

    repo = tmp_path / "repo"
    repo.mkdir()
    for argv in (
        ("git", "init", "-q", "-b", "main"),
        ("git", "config", "user.email", "t@example.com"),
        ("git", "config", "user.name", "T"),
        ("git", "commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(argv, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    a = str(rebar.create_ticket("task", "a4bd source", repo_root=repo))
    b = str(rebar.create_ticket("task", "a4bd target", repo_root=repo))
    rebar.link(a, b, "blocks", repo_root=repo)
    return repo, a, b


def _deps(repo, local_id):
    """The ticket's net-active ``(relation, target)`` set, read back from the store."""
    import rebar

    ticket = rebar.show_ticket(local_id, repo_root=repo)
    return {(d.get("relation"), d.get("target_id")) for d in (ticket.get("deps") or [])}


def _remove_payload(target_id, relation="blocks"):
    return {"links": [{"action": "remove", "target_id": target_id, "relation": relation}]}


# ---------------------------------------------------------------------------
# The core discrimination
# ---------------------------------------------------------------------------


def test_declines_removal_of_a_never_confirmed_link(apply_records, store, caplog):
    """AC1. No confirmation record => the link stays and nothing is written."""
    repo, a, b = store
    assert ("blocks", b) in _deps(repo, a)

    with caplog.at_level(logging.WARNING):
        applied = apply_records._inbound_update_apply_links(_remove_payload(b), a, repo)

    assert applied == 0
    assert ("blocks", b) in _deps(repo, a), "a never-confirmed link was removed"
    assert any("no peer-confirmation record" in r.getMessage() for r in caplog.records)


def test_confirmed_link_still_converges(apply_records, pc, store):
    """AC2. With evidence, e39f's relation-scoped removal still works."""
    repo, a, b = store
    confirm = pc.open_store(repo)
    confirm.record(a, b, "blocks", link_id="10042")
    confirm.save()

    applied = apply_records._inbound_update_apply_links(_remove_payload(b), a, repo)

    assert applied == 1
    assert ("blocks", b) not in _deps(repo, a)


def test_confirmation_is_relation_scoped_not_pair_scoped(apply_records, pc, store):
    """A confirmation for a DIFFERENT relation on the same pair must not license removal.

    One ordered pair can hold two net-active relations, so pair-scoped evidence would
    authorise deleting the wrong link — exactly the asymmetry e39f closed on the write
    side and this story must not reintroduce on the evidence side.
    """
    repo, a, b = store
    confirm = pc.open_store(repo)
    confirm.record(a, b, "relates_to")
    confirm.save()

    applied = apply_records._inbound_update_apply_links(_remove_payload(b), a, repo)

    assert applied == 0
    assert ("blocks", b) in _deps(repo, a)


def test_backfilled_confirmation_permits_removal(apply_records, pc, store):
    """AC5. A grandfathered record is honoured, so the first post-upgrade pass
    declines no legitimate removal — the regression the S4 backfill exists to prevent."""
    repo, a, b = store
    confirm = pc.open_store(repo)
    confirm.record(
        a,
        b,
        "blocks",
        link_id=None,
        direction=pc.DIRECTION_BACKFILL,
        source_kind=pc.SOURCE_BACKFILL,
    )
    confirm.save()

    applied = apply_records._inbound_update_apply_links(_remove_payload(b), a, repo)

    assert applied == 1
    assert ("blocks", b) not in _deps(repo, a)


def test_decline_is_idempotent_across_passes(apply_records, store):
    """AC4. The differ re-emits the record every pass; each pass declines and writes nothing."""
    repo, a, b = store

    for _ in range(3):
        assert apply_records._inbound_update_apply_links(_remove_payload(b), a, repo) == 0

    assert ("blocks", b) in _deps(repo, a)


def test_vendor_type_collision_scenario_is_protected(apply_records, store, caplog):
    """A local blocks link deduped against an inward same-type peer link was never
    pushed. Its removal must be declined without positive peer confirmation, leaving
    the local dependency active."""
    repo, a, b = store

    with caplog.at_level(logging.WARNING):
        applied = apply_records._inbound_update_apply_links(_remove_payload(b, "blocks"), a, repo)

    assert applied == 0
    assert ("blocks", b) in _deps(repo, a), "the epic's motivating regression reproduced"


# ---------------------------------------------------------------------------
# Fail-open and reporting
# ---------------------------------------------------------------------------


def test_unopenable_store_declines_nothing(apply_records, store, monkeypatch):
    """AC6. Evidence is a safety optimisation, never a precondition.

    A None store must restore pre-a4bd behaviour exactly — failing closed would freeze
    inbound removal convergence on any unreadable sidecar.
    """
    repo, a, b = store
    monkeypatch.setattr(apply_records, "_open_peer_confirmation_store", lambda _r: None)

    applied = apply_records._inbound_update_apply_links(_remove_payload(b), a, repo)

    assert applied == 1
    assert ("blocks", b) not in _deps(repo, a)


def test_pass_summary_reports_the_decline_count(apply_records, store, caplog):
    """AC7. Declines are operator-visible, not log-only trivia."""
    repo, a, b = store

    with caplog.at_level(logging.INFO):
        apply_records._inbound_update_apply_links(_remove_payload(b), a, repo)

    messages = [r.getMessage() for r in caplog.records]
    assert any("declined 1 inbound link removal" in m for m in messages), messages


def test_removal_of_an_absent_relation_is_still_a_logged_no_op(apply_records, pc, store, caplog):
    """e39f's invariant survives: a named relation with no net-active link removes nothing.

    Confirmed here, so the decline is not what makes it a no-op — the absence is.
    """
    repo, a, b = store
    confirm = pc.open_store(repo)
    confirm.record(a, b, "relates_to")
    confirm.save()

    with caplog.at_level(logging.INFO):
        applied = apply_records._inbound_update_apply_links(
            _remove_payload(b, "relates_to"), a, repo
        )

    assert applied == 0
    assert ("blocks", b) in _deps(repo, a), "an unrelated relation's link was removed"


def test_add_records_are_unaffected_by_the_decline(apply_records, store):
    """The ADD branch must be untouched: this story changes only the REMOVE path."""
    repo, a, b = store
    import rebar

    rebar.unlink(a, b, "blocks", repo_root=repo)
    assert ("blocks", b) not in _deps(repo, a)

    payload = {"links": [{"action": "add", "target_id": b, "relation": "blocks"}]}
    applied = apply_records._inbound_update_apply_links(payload, a, repo)

    assert applied == 1
    assert ("blocks", b) in _deps(repo, a)
