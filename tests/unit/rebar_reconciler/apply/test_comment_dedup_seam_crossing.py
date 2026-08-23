"""Seam-crossing dedup test: the real envelope through record, then re-diff SAME store.

Ticket vanitied-kitschy-mantis (9dc3-7d6c-83d7-41c7), filed from the escape analysis
on marshy-chummy-coot (2399-5e49-6b28-4d59): an instrumented run (hard-wiring
``BindingStore.is_comment_mapped`` to ``False`` on every loaded module copy) showed
the dedup suite could not observe the PRIMARY skip being disabled end-to-end — the
differ tests seed map state via a private stub store, the apply tests seed synthetic
id-bearing payloads, and each layer's test seeds the invariant the other layer is
supposed to establish.

These tests cross the seam with NO seeded invariants:

- the transport return is produced by the REAL ``acli_cli_ops.add_comment`` (stubbed
  only at the ``_run_acli`` subprocess seam) fed the RECORDED ACLI batch envelope;
- the recording runs through the REAL ``dispatch_apply_phases._record_comment_id``
  against a REAL ``BindingStore``;
- the re-diff runs the REAL ``outbound_comments._diff_comments`` against that SAME
  store instance.

The Jira-side body diverges from every local body, so the SECONDARY body-equality
skip can never mask an inert PRIMARY id-identity skip. A control comment that was
never recorded must still be emitted, and a store whose ``is_comment_mapped`` is
forced inert must re-emit the recorded comment — so a hard-wired-False guard is
distinguishable from a store that was never written.

Test-only change; no production code (ticket non-goal).
"""

from __future__ import annotations

import json

import pytest

from .test_comment_id_acli_envelope import ACLI_SUCCESS_ENVELOPE, _load

_KEY_POSTED = 1787251802516868001
_KEY_CONTROL = 1787251802516868002
_BODY_POSTED = "## Posted\n\nA **rich** body that will not survive ADF round-tripping."
_BODY_CONTROL = "## Control\n\nA second body, never posted, that must keep emitting."

_JIRA_KEY = "REB-1861"


@pytest.fixture(scope="module")
def binding_store_mod():
    return _load("_binding_store_for_dedup_seam", "binding_store.py")


@pytest.fixture
def store(binding_store_mod, tmp_path):
    return binding_store_mod.BindingStore(tmp_path / ".tickets-tracker")


@pytest.fixture
def transport_result(monkeypatch):
    """What the REAL Cloud transport hands the apply phase for a live ACLI post."""
    from rebar_reconciler.adapters.jira import acli_cli_ops, acli_subprocess

    class _Completed:
        stdout = json.dumps(ACLI_SUCCESS_ENVELOPE)
        stderr = ""
        returncode = 0

    monkeypatch.setattr(acli_subprocess, "_run_acli", lambda *a, **k: _Completed())
    return acli_cli_ops.add_comment(_JIRA_KEY, _BODY_POSTED, acli_cmd=["acli"])


def _ticket_and_remote():
    """A two-comment local ticket and a Jira snapshot whose body diverges from both."""
    ticket = {
        "comments": [
            {"body": _BODY_POSTED, "timestamp": _KEY_POSTED},
            {"body": _BODY_CONTROL, "timestamp": _KEY_CONTROL},
        ]
    }
    remote = {
        _JIRA_KEY: {"comment": {"comments": [{"body": "diverged beyond recognition"}], "total": 1}}
    }
    return ticket, remote


def _diff(ticket, remote, store):
    from rebar_reconciler.outbound_comments import _diff_comments

    return _diff_comments(ticket, _JIRA_KEY, remote, binding_store=store)


def _record_first_pending(store, pending, transport_result):
    from rebar_reconciler.dispatch_apply_phases import _record_comment_id

    entry = next(e for e in pending if e["local_comment_key"] == _KEY_POSTED)
    _record_comment_id(store, entry, transport_result)


def test_recorded_post_is_skipped_and_unrecorded_control_still_emits(store, transport_result):
    """Post -> record -> re-diff on ONE store: skip engages ONLY for the recorded key."""
    ticket, remote = _ticket_and_remote()

    first = _diff(ticket, remote, store)
    assert sorted(e["local_comment_key"] for e in first) == [_KEY_POSTED, _KEY_CONTROL], (
        "precondition: both comments are pending before anything is recorded"
    )

    _record_first_pending(store, first, transport_result)

    second = _diff(ticket, remote, store)
    keys = [e["local_comment_key"] for e in second]
    assert _KEY_POSTED not in keys, (
        "the comment that landed in Jira was re-emitted from the SAME store that "
        "recorded it — the apply-to-differ seam is broken (the aa7b duplicate loop)"
    )
    assert keys == [_KEY_CONTROL], (
        "the never-posted control comment must still be emitted; a skip that engages "
        "for unrecorded keys is over-deduplication (dropped comments, not duplicates)"
    )


def test_inert_is_comment_mapped_reemits_the_recorded_comment(store, transport_result):
    """A hard-wired-False guard is observable: the recorded comment comes back.

    This is the discriminating power the escape analysis found missing — with the
    guard inert the seam test above MUST fail, and this variant pins that dependency
    so it cannot silently rot: same store, same recording, only the guard disabled.
    """
    ticket, remote = _ticket_and_remote()
    first = _diff(ticket, remote, store)
    _record_first_pending(store, first, transport_result)

    store.is_comment_mapped = lambda key: False  # the instrumented escape, reproduced

    second = _diff(ticket, remote, store)
    assert _KEY_POSTED in [e["local_comment_key"] for e in second], (
        "with is_comment_mapped inert the PRIMARY skip cannot fire and the diverged "
        "body defeats the SECONDARY skip — if the recorded comment did NOT reappear, "
        "something other than is_comment_mapped is deduplicating and this suite is "
        "not actually exercising the guard"
    )
