"""Verify comment identity handling for ACLI batch envelopes.

ACLI returns work-item results whose `results[].id` is an issue key, not a
comment ID. `acli_cli_ops.add_comment` normalizes a successful envelope to
`{"id": None, "acli_envelope": ...}` and passes DC/REST resource IDs through.
Unrecognized shapes raise.

After success, `_record_comment_id` maps the local comment key even when no
remote comment ID exists. `_diff_comments` then emits nothing despite body
round-tripping. The hermetic test stubs `_run_acli` and uses a `BindingStore`.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

_SRC_DIR = Path(__file__).resolve().parents[4] / "src" / "rebar" / "reconciler"

#: The envelope shape verified live against ``acli 1.3.19-stable``.
ACLI_SUCCESS_ENVELOPE = {
    "results": [{"status": "SUCCESS", "message": "Comment added", "id": "REB-1861"}],
    "totalCount": 1,
    "successCount": 1,
}

#: RECORDED, not invented (ticket 3235-8aaf-e288-48f2 AC2): the byte-exact payload
#: captured live on 2026-08-22 from ``acli 1.3.19-stable`` via the safe probe
#: ``acli jira workitem comment create --key NONEXIST-999999 --body ... --json``
#: (a nonexistent key — the genuine envelope comes back with exit=0 and nothing is
#: mutated). Note ``results[].id`` is the WORK ITEM key, and the process still
#: exits 0 on FAILURE (bug 44de).
ACLI_FAILURE_ENVELOPE = {
    "results": [
        {
            "status": "FAILURE",
            "message": "Issue does not exist or you do not have permission to see it.",
            "id": "NONEXIST-999999",
        }
    ],
    "totalCount": 1,
    "successCount": 0,
}

#: What ``add_comment`` returns for a SUCCESS envelope post-normalization.
NORMALIZED_SUCCESS = {"id": None, "acli_envelope": ACLI_SUCCESS_ENVELOPE}

_KEY = 1787251802516868001
_BODY = "## Heading\n\nA **rich** body that will not survive ADF round-tripping."


@pytest.fixture(scope="module")
def binding_store_mod():
    return importlib.import_module("rebar.reconciler.binding_store")


@pytest.fixture
def store(binding_store_mod, tmp_path):
    return binding_store_mod.BindingStore(tmp_path / ".tickets-tracker")


@pytest.fixture
def acli_result(monkeypatch):
    """The value the REAL ``acli_cli_ops.add_comment`` returns for a live ACLI post."""
    from rebar.reconciler.adapters.jira import acli_cli_ops, acli_subprocess

    class _Completed:
        stdout = json.dumps(ACLI_SUCCESS_ENVELOPE)
        stderr = ""
        returncode = 0

    monkeypatch.setattr(acli_subprocess, "_run_acli", lambda *a, **k: _Completed())
    result = acli_cli_ops.add_comment("REB-1861", _BODY, acli_cmd=["acli"])
    # Fixture precondition (ticket 3235-8aaf): the envelope is NORMALIZED at the
    # transport boundary — no top-level comment id (ACLI echoes none), the raw
    # envelope preserved under an explicitly-named key.
    assert result == NORMALIZED_SUCCESS
    assert result.get("id") is None
    return result


def test_acli_envelope_post_is_recorded_in_the_comment_map(store, acli_result):
    """A successful post with NO echoed comment id still maps the local key."""
    from rebar.reconciler.dispatch_apply_phases import _record_comment_id

    assert store.is_comment_mapped(_KEY) is False, "precondition: key starts unmapped"

    _record_comment_id(store, {"local_comment_key": _KEY, "body": _BODY}, acli_result)

    assert store.is_comment_mapped(_KEY) is True, (
        "a comment that landed in Jira was not recorded in the comment map, so the "
        "PRIMARY id-identity skip can never fire and the comment will be re-posted"
    )

    # The VALUE must say plainly that no comment id is known. In particular the
    # envelope's ``results[].id`` is the WORK ITEM KEY ("REB-1861"); storing that
    # would satisfy is_comment_mapped while lying about what the map holds.
    from rebar.reconciler.binding_lifecycle import UNKNOWN_COMMENT_ID

    recorded = store.comment_id_for(_KEY)
    assert recorded == UNKNOWN_COMMENT_ID, f"expected the no-id sentinel, got {recorded!r}"
    assert recorded != ACLI_SUCCESS_ENVELOPE["results"][0]["id"], (
        "the work item key was stored as if it were a Jira comment id"
    )


def test_recorded_mapping_survives_a_reopen(binding_store_mod, tmp_path, acli_result):
    """The write-ahead save persists it, so the NEXT hourly pass sees it."""
    from rebar.reconciler.dispatch_apply_phases import _record_comment_id

    tracker = tmp_path / ".tickets-tracker"
    _record_comment_id(
        binding_store_mod.BindingStore(tracker),
        {"local_comment_key": _KEY, "body": _BODY},
        acli_result,
    )

    assert binding_store_mod.BindingStore(tracker).is_comment_mapped(_KEY) is True


def test_no_reemission_after_an_acli_post_even_when_the_body_diverges(store, acli_result):
    """End-to-end: post, record, re-diff -> zero mutations.

    The Jira side carries a body that does NOT string-match the local one (the
    round-trip divergence that defeats the SECONDARY body-equality skip), so this
    passes ONLY when the PRIMARY id-identity skip engages.
    """
    from rebar.reconciler.dispatch_apply_phases import _record_comment_id
    from rebar.reconciler.outbound_comments import _diff_comments

    ticket = {"comments": [{"body": _BODY, "timestamp": _KEY}]}
    diverged = {"REB-1861": {"comment": {"comments": [{"body": "totally different"}], "total": 1}}}

    first = _diff_comments(ticket, "REB-1861", diverged, binding_store=store)
    assert len(first) == 1, "precondition: the comment is pending on the first pass"
    assert first[0]["local_comment_key"] == _KEY

    _record_comment_id(store, first[0], acli_result)

    second = _diff_comments(ticket, "REB-1861", diverged, binding_store=store)
    assert second == [], (
        "the comment was re-emitted after it had already landed in Jira -- this is the "
        "hourly duplicate loop that filled REB-1861 to Jira's 5000-comment cap"
    )


def test_a_transport_supplied_comment_id_is_still_recorded_verbatim(store):
    """The DC/REST path echoes a real comment id; it must be preserved, not replaced."""
    from rebar.reconciler.dispatch_apply_phases import _record_comment_id

    _record_comment_id(store, {"local_comment_key": _KEY, "body": _BODY}, {"id": "10001"})

    assert store.comment_id_for(_KEY) == "10001"


def _add_comment_with_stdout(monkeypatch, stdout: str):
    """Drive the REAL ``acli_cli_ops.add_comment`` with *stdout* stubbed at ``_run_acli``."""
    from rebar.reconciler.adapters.jira import acli_cli_ops, acli_subprocess

    class _Completed:
        stderr = ""
        returncode = 0

    _Completed.stdout = stdout
    monkeypatch.setattr(acli_subprocess, "_run_acli", lambda *a, **k: _Completed())
    return acli_cli_ops.add_comment("REB-1861", _BODY, acli_cmd=["acli"])


def test_success_envelope_is_normalized_not_passed_through_raw(monkeypatch):
    """Ticket 3235-8aaf AC1: the SUCCESS envelope becomes an honest comment resource."""
    result = _add_comment_with_stdout(monkeypatch, json.dumps(ACLI_SUCCESS_ENVELOPE))

    assert result == NORMALIZED_SUCCESS
    assert result["id"] is None, "ACLI echoes no comment id; the resource must say so plainly"
    assert "results" not in result, (
        "the raw batch envelope leaked through: results[].id is the WORK ITEM key and a "
        "consumer reading it as a comment id re-creates bug aa7b-5e47-6d3d-4615"
    )


def test_resource_shaped_payload_passes_through_verbatim(monkeypatch):
    """The DC/REST-shaped comment resource (top-level id) is not rewritten."""
    result = _add_comment_with_stdout(monkeypatch, '{"id": "10001"}')

    assert result == {"id": "10001"}


def test_unrecognized_payload_raises_instead_of_returning_unusable(monkeypatch):
    """A shape that is neither a resource nor the envelope fails loudly (sibling idiom)."""
    with pytest.raises(RuntimeError, match="comment create"):
        _add_comment_with_stdout(monkeypatch, '["not", "a", "comment"]')


def test_recorded_failure_envelope_raises_acli_mutation_error():
    """The RECORDED live failure envelope (exit=0!) trips the bug-44de guard.

    Asserted against ``ACLI_FAILURE_ENVELOPE`` — captured live, provenance in the
    constant's comment — through the exact checker ``_run_acli`` runs on every
    completed mutation, so a real post of this payload raises before ``add_comment``
    ever parses it.
    """
    from rebar.reconciler.adapters.jira.acli_subprocess import (
        AcliMutationError,
        _check_mutation_failure,
    )

    with pytest.raises(AcliMutationError, match="does not exist"):
        _check_mutation_failure(
            json.dumps(ACLI_FAILURE_ENVELOPE),
            ["acli", "jira", "workitem", "comment", "create"],
        )


_KEY_POSTED = 1787251802516868001


_KEY_CONTROL = 1787251802516868002


_BODY_POSTED = "## Posted\n\nA **rich** body that will not survive ADF round-tripping."


_BODY_CONTROL = "## Control\n\nA second body, never posted, that must keep emitting."


_JIRA_KEY = "REB-1861"


@pytest.fixture
def transport_result(monkeypatch):
    """What the REAL Cloud transport hands the apply phase for a live ACLI post."""
    from rebar.reconciler.adapters.jira import acli_cli_ops, acli_subprocess

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
    from rebar.reconciler.outbound_comments import _diff_comments

    return _diff_comments(ticket, _JIRA_KEY, remote, binding_store=store)


def _record_first_pending(store, pending, transport_result):
    from rebar.reconciler.dispatch_apply_phases import _record_comment_id

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
