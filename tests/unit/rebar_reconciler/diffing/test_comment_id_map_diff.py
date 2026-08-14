"""Append-only comment diff keyed on the persistent map (emersed-specific-mutt).

``_diff_comments`` must identify an already-mirrored comment by its persistent
identity — the COMMENT event's HLC ``timestamp`` (``local_comment_key``) mapped
to a Jira comment ID, or an inbound-origin ``jira_comment_id`` on the entry — not
by body equality. The PRIMARY outbound skip is "the key is mapped OR the entry
carries a ``jira_comment_id``"; the historical body-equality test is DEMOTED to a
secondary belt-and-suspenders skip that only guards a lost map write. Every
emitted mutation carries its ``local_comment_key`` so the enactment site can
record the returned Jira ID against it.

Hermetic: a stub binding store, a synthetic Jira snapshot, no network.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SRC_DIR = Path(__file__).resolve().parents[4] / "src" / "rebar" / "_engine" / "rebar_reconciler"
_OC = _SRC_DIR / "outbound_comments.py"


def _load():
    spec = importlib.util.spec_from_file_location("outbound_comments_commentid_test", _OC)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def oc():
    return _load()


class _StubBindingStore:
    def __init__(self, mapped=()):
        self._mapped = set(mapped)

    def is_comment_mapped(self, key: str) -> bool:
        return key in self._mapped

    def record_comment_id(self, key: str, _jira_comment_id) -> None:
        """Mirror BindingStore.record_comment_id so an idempotent second pass can
        consult the same store the enactment site would have written."""
        self._mapped.add(str(key))


def _snapshot(jira_key: str, bodies=()) -> dict:
    comments = [{"body": b} for b in bodies]
    return {jira_key: {"comment": {"comments": comments, "total": len(comments)}}}


_K1 = "1785800303802040001-aaaa"
_K2 = "1785800375812118001-bbbb"


def test_comment_duplicate_bodies_distinct_ids(oc):
    """Two comments with the SAME body but distinct HLC keys both sync — identity
    is the key, not the body — and each mutation carries its own key."""
    ticket = {
        "comments": [
            {"body": "same text", "timestamp": _K1},
            {"body": "same text", "timestamp": _K2},
        ]
    }
    out = oc._diff_comments(ticket, "DIG-1", _snapshot("DIG-1"), binding_store=_StubBindingStore())
    assert len(out) == 2
    keys = {m.get("local_comment_key") for m in out}
    assert keys == {_K1, _K2}


def test_diff_skips_mapped_comment(oc):
    """A comment whose HLC key is already mapped is not re-posted (append-only)."""
    ticket = {"comments": [{"body": "hello", "timestamp": _K1}]}
    out = oc._diff_comments(
        ticket, "DIG-1", _snapshot("DIG-1"), binding_store=_StubBindingStore(mapped={_K1})
    )
    assert out == []


def test_diff_skips_comment_with_jira_comment_id(oc):
    """An inbound-origin comment (carries jira_comment_id) is never pushed back."""
    ticket = {"comments": [{"body": "from jira", "timestamp": _K1, "jira_comment_id": "9"}]}
    out = oc._diff_comments(ticket, "DIG-1", _snapshot("DIG-1"), binding_store=_StubBindingStore())
    assert out == []


def test_diff_emits_local_comment_key(oc):
    """A genuinely new comment emits one add carrying its local_comment_key."""
    ticket = {"comments": [{"body": "brand new", "timestamp": _K1}]}
    out = oc._diff_comments(ticket, "DIG-1", _snapshot("DIG-1"), binding_store=_StubBindingStore())
    assert len(out) == 1
    assert out[0]["local_comment_key"] == _K1
    assert out[0]["action"] == "add"
    assert "brand new" in out[0]["body"]


def test_body_equality_secondary_skip(oc):
    """Unmapped + no jira_comment_id, but the body is already in Jira: the demoted
    body-equality test still suppresses a re-post (guards a lost map write)."""
    ticket = {"comments": [{"body": "already there", "timestamp": _K1}]}
    out = oc._diff_comments(
        ticket,
        "DIG-1",
        _snapshot("DIG-1", ["already there"]),
        binding_store=_StubBindingStore(),
    )
    assert out == []


def test_diff_works_without_binding_store(oc):
    """binding_store is optional — legacy callers still get body-equality dedup."""
    ticket = {"comments": [{"body": "new one", "timestamp": _K1}]}
    out = oc._diff_comments(ticket, "DIG-1", _snapshot("DIG-1"))
    assert len(out) == 1
    assert out[0]["local_comment_key"] == _K1


def test_comment_dedup_key_uses_real_codec(oc):
    """AC: the a32a ``_resolve_codec`` injection default flips from the identity
    no-op to the real AdfCodec so the local dedup key is normalized the way the
    landed Cloud wire would be (DC comment-diff dedup-key migration 3388)."""
    codec = oc._resolve_codec(None)
    assert type(codec).__name__ == "AdfCodec"


def test_comment_bidirectional_identity_no_double_sync(oc):
    """AC: the inbound-origin ``jira_comment_id`` (bug 85a1) and the outbound
    comment_ids map are EACH authoritative for their own direction and never
    compete. The outbound differ skips when EITHER the HLC key is mapped OR the
    entry carries a ``jira_comment_id`` — while a genuinely new local comment
    (neither mapped nor inbound-origin) still syncs exactly once."""
    ticket = {
        "comments": [
            {"body": "outbound already mapped", "timestamp": _K1},
            {"body": "pulled from jira", "timestamp": _K2, "jira_comment_id": "77"},
            {"body": "brand new local", "timestamp": "1785800400000000001-cccc"},
        ]
    }
    out = oc._diff_comments(
        ticket, "DIG-1", _snapshot("DIG-1"), binding_store=_StubBindingStore(mapped={_K1})
    )
    assert len(out) == 1
    assert out[0]["local_comment_key"] == "1785800400000000001-cccc"
    assert "brand new local" in out[0]["body"]


def test_comment_append_only(oc):
    """AC: comment sync is append-only. A new comment mirrors once; an EDIT of an
    already-mirrored comment (same HLC key, changed body) is skipped, and the diff
    never emits a delete/edit action — only ``add`` for genuinely new keys."""
    # Same key already mapped: an edited body must NOT re-sync (no edit push).
    edited = {"comments": [{"body": "edited text now", "timestamp": _K1}]}
    out_edit = oc._diff_comments(
        edited, "DIG-1", _snapshot("DIG-1"), binding_store=_StubBindingStore(mapped={_K1})
    )
    assert out_edit == []

    # A genuinely new comment mirrors exactly once, as an add.
    fresh = {"comments": [{"body": "a new one", "timestamp": _K2}]}
    out_new = oc._diff_comments(
        fresh, "DIG-1", _snapshot("DIG-1"), binding_store=_StubBindingStore()
    )
    assert [m["action"] for m in out_new] == ["add"]


def test_comment_sync_idempotent(oc):
    """AC: a repeat reconcile pass creates no duplicate comments. Pass 1 emits the
    add and the enactment site records the returned id into the store; pass 2 over
    the SAME store emits nothing (the PRIMARY mapped-key skip)."""
    store = _StubBindingStore()
    ticket = {"comments": [{"body": "only once", "timestamp": _K1}]}

    first = oc._diff_comments(ticket, "DIG-1", _snapshot("DIG-1"), binding_store=store)
    assert len(first) == 1
    # Simulate the enactment site persisting the returned Jira id write-ahead.
    store.record_comment_id(first[0]["local_comment_key"], "10001")

    second = oc._diff_comments(ticket, "DIG-1", _snapshot("DIG-1"), binding_store=store)
    assert second == []


def test_comment_crash_between_add_and_map_save_no_duplicate(oc):
    """AC: crash-durability. If the process died AFTER add_comment landed the body
    in Jira but BEFORE ``record_comment_id`` persisted the map entry, the map has
    no key — yet the DEMOTED body-equality test (a secondary belt-and-suspenders
    skip) still recognizes the already-landed body and does not re-post."""
    ticket = {"comments": [{"body": "landed but unmapped", "timestamp": _K1}]}
    # Body IS present in Jira (add_comment succeeded) but the store is empty
    # (record_comment_id never ran) -> secondary body-equality skip suppresses it.
    out = oc._diff_comments(
        ticket,
        "DIG-1",
        _snapshot("DIG-1", ["landed but unmapped"]),
        binding_store=_StubBindingStore(),
    )
    assert out == []


def test_dc_comment_diff_converges_after_migration(oc):
    """AC: after the 3388 dedup-key migration, the DC comment-diff converges with
    zero duplicate re-post. Injecting the REAL DC ``WikiTextCodec`` (the migrated
    dedup normalizer, replacing the a32a ``_IdentityCodec``), a comment whose body
    already landed in Jira is recognized and skipped, while a new one still emits."""
    from rebar_reconciler.adapters.jira_family.rich_text import WikiTextCodec

    dc_codec = WikiTextCodec()
    already = {"comments": [{"body": "converged body", "timestamp": _K1}]}
    out_converged = oc._diff_comments(
        already, "DIG-1", _snapshot("DIG-1", ["converged body"]), codec=dc_codec
    )
    assert out_converged == [], "migrated DC dedup key must converge (no re-post)"

    fresh = {"comments": [{"body": "unseen dc body", "timestamp": _K2}]}
    out_new = oc._diff_comments(fresh, "DIG-1", _snapshot("DIG-1"), codec=dc_codec)
    assert len(out_new) == 1


def test_map_comments_for_create_carries_key(oc):
    """The create-path mapping tags each outbound comment with its local key."""
    ticket = {"comments": [{"body": "c1", "timestamp": _K1}, {"body": "c2", "timestamp": _K2}]}
    out = oc._map_comments_for_create(ticket)
    assert [m["local_comment_key"] for m in out] == [_K1, _K2]
