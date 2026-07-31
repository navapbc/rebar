"""A binding survives its issue MOVING project — bug 7c26-4ac8-04a3-440e.

A Jira issue's KEY changes when the issue is moved between projects; its numeric
``id`` never does. Old keys are normally stacked in Jira's ``moved_issue_key`` table
so the old key still resolves, but a Data-Center-specific Atlassian KB documents
third-party apps moving issues via post-functions/automations failing to update that
table — after which the OLD KEY STOPS RESOLVING ENTIRELY. rebar's bindings are keyed
on the key, so the absence probe reads that 404 as a deletion, and at GRACE it retires
the binding: a live issue silently detached from its local ticket.

These cells are driven END-TO-END through ``compute_binding_walk_mutations`` — the
actual absence probe — rather than by poking a store entry, because the defect IS the
probe's interpretation of a 404. A test that only asserted ``note_absent_or_rekey``
re-keys when called would pass while the walk never called it.

Every cell here FAILS against the pre-7c26 code: the walk called ``note_absent``
unconditionally, so a moved issue accrued grace and retired.

Follows the reconciler test-tree loader convention (spec_from_file_location).
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path

import pytest

_SRC_DIR = Path(__file__).resolve().parents[3] / "src" / "rebar" / "_engine" / "rebar_reconciler"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _SRC_DIR / filename)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_walk = _load("_binding_walk_for_7c26", "binding_walk.py")
_bs = _load("_binding_store_for_7c26", "binding_store.py")
_classify = _load("_classify_for_7c26", "classify.py")
_mutation = _load("_mutation_for_7c26", "mutation.py")
_ob = _load("_ob_for_7c26", "outbound_differ.py")
BindingStore = _bs.BindingStore

_OLD_KEY = "OLD-7"
_NEW_KEY = "NEW-42"
_NUMERIC_ID = "10321"


def compute(*args, **kwargs):
    """Invoke the walk with the engine sibling modules injected (avoids the pytest
    test-package import shadow on ``rebar_reconciler.classify``)."""
    kwargs.setdefault("classify_mod", _classify)
    kwargs.setdefault("mutation_mod", _mutation)
    kwargs.setdefault("outbound_differ_mod", _ob)
    return _walk.compute_binding_walk_mutations(*args, **kwargs)


class MovedIssueClient:
    """A client whose issue MOVED: the old key 404s, the numeric id still resolves.

    Models the KB's broken-``moved_issue_key`` case — the one where the old key does
    NOT redirect — because that is the case rebar cannot currently survive. Counts
    lookups so a test can assert the recovery path is not entered on the happy path.
    """

    def __init__(self, current_key: str = _NEW_KEY, resolves: bool = True) -> None:
        self.current_key = current_key
        self.resolves = resolves
        self.by_id_lookups: list[str] = []

    def get_issue_by_rest(self, remote_id: str):
        self.by_id_lookups.append(remote_id)
        if not self.resolves or remote_id != _NUMERIC_ID:
            raise AssertionError(f"unexpected/unresolvable lookup for {remote_id!r}")
        return {"id": _NUMERIC_ID, "key": self.current_key, "fields": {"summary": "moved"}}


def _store_with_moved_binding(tmp_path: Path, *, with_id: bool = True) -> BindingStore:
    """A confirmed binding on the OLD key, optionally carrying the captured id.

    ``with_id=False`` is the LEGACY shape — every binding written before this fix —
    and must keep behaving exactly as it did.
    """
    bs = BindingStore(tmp_path / ".tickets-tracker")
    bs.bind_confirm("loc-moved", _OLD_KEY)
    if with_id:
        bs.record_jira_id("loc-moved", _NUMERIC_ID)
    bs.save()
    return bs


def _archived_reader(local_id: str = "loc-moved"):
    ticket = {"ticket_id": local_id, "status": "archived", "archived": True}
    return lambda lid: ticket if lid == local_id else None


def _probe_old_key_gone(_client, key):
    """The direct GET a move actually produces: the OLD key is gone, and any current
    key resolves. Deliberately key-AWARE — a probe that 404s unconditionally would
    also 404 the re-keyed binding on the next pass, which is a real absence and
    correctly retires, so it could never demonstrate survival."""
    return _ob._DELETED if key == _OLD_KEY else {"summary": "moved", "status": "To Do"}


def _walk_once(bs: BindingStore, client, *, probe=_probe_old_key_gone):
    return compute(
        bs,
        {},  # the moved issue is absent from this project-scoped window
        active_local_ids=set(),
        client=client,
        local_reader=_archived_reader(),
        max_acting_fraction=1.0,
        probe_get=probe,
    )


# ── AC1 — the binding SURVIVES the move, and is re-keyed ──────────────────────


def test_a_moved_issue_keeps_its_binding_and_is_rekeyed(tmp_path: Path) -> None:
    """THE DEFECT. The old key 404s; the recorded numeric id still resolves, under a
    NEW key. The binding must be preserved and re-keyed — not read as a deletion."""
    bs = _store_with_moved_binding(tmp_path)
    client = MovedIssueClient()

    _walk_once(bs, client)

    assert bs.get_jira_key("loc-moved") == _NEW_KEY, (
        "a moved issue's binding must be re-keyed to its CURRENT key; pre-7c26 the "
        "404 on the old key was recorded as an absence and the key was left stale"
    )
    assert bs.is_bound("loc-moved"), "the binding must survive the move"
    assert client.by_id_lookups == [_NUMERIC_ID], (
        "the recovery must ask by the immutable numeric id exactly once"
    )


# ── AC2 — the absence probe must NOT retire a reachable issue ────────────────


def test_the_absence_probe_does_not_retire_an_issue_reachable_by_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The silent detachment this ticket exists to stop. GRACE is lowered to 1 so a
    SINGLE unrecovered 404 would retire; the issue is reachable by id, so none may."""
    monkeypatch.setenv("RECONCILER_ABSENT_RETIRE_GRACE", "1")
    bs = _store_with_moved_binding(tmp_path)
    client = MovedIssueClient()

    # Three passes: the first re-keys, and the two after it probe the NEW key, which
    # resolves — so the pair never re-enters the 404 branch at all.
    for _ in range(3):
        _walk_once(bs, client)

    assert not bs.is_retired(_OLD_KEY), "a reachable issue must never be retired"
    assert not bs.is_retired(_NEW_KEY), "nor under its current key"
    assert bs.get_jira_key("loc-moved") == _NEW_KEY
    retired_path = tmp_path / ".tickets-tracker" / ".bridge_state" / "bindings-retired.json"
    assert not retired_path.exists(), "no retirement file should have been written at all"


def test_the_absence_counter_is_reset_by_a_rekey(tmp_path: Path) -> None:
    """A move must not leave accrued grace behind: an issue that 404'd twice before
    the move was noticed would otherwise sit one miss from retirement."""
    bs = _store_with_moved_binding(tmp_path)
    bs.note_absent(_OLD_KEY)
    bs.note_absent(_OLD_KEY)
    assert bs.all_bindings()["loc-moved"]["absent_404_count"] == 2

    bs.note_absent_or_rekey(_OLD_KEY, MovedIssueClient())

    assert bs.all_bindings()["loc-moved"]["absent_404_count"] == 0, (
        "re-keying proves the issue is alive, so the consecutive-404 counter must reset"
    )


# ── AC3 — the reverse index follows the re-key, in the same operation ────────


def test_the_reverse_index_follows_the_rekey(tmp_path: Path) -> None:
    """A stale reverse entry would re-detach the pair on the next pass: the old key
    would still resolve to this local id, so an adopt/dedup path could double-bind."""
    bs = _store_with_moved_binding(tmp_path)

    _walk_once(bs, MovedIssueClient())

    assert bs.get_local_id(_NEW_KEY) == "loc-moved", "the new key must resolve to the pair"
    assert bs.get_local_id(_OLD_KEY) is None, (
        "the OLD key must stop resolving in the same operation as the re-key"
    )


def test_the_rekey_is_persisted_not_merely_in_memory(tmp_path: Path) -> None:
    """The re-key must survive the process: a store re-read from disk must show the
    new key. An in-memory-only re-key would be undone by the next pass's load."""
    bs = _store_with_moved_binding(tmp_path)
    _walk_once(bs, MovedIssueClient())

    reloaded = BindingStore(tmp_path / ".tickets-tracker")
    assert reloaded.get_jira_key("loc-moved") == _NEW_KEY
    assert reloaded.get_local_id(_OLD_KEY) is None
    assert reloaded.get_jira_id("loc-moved") == _NUMERIC_ID


# ── AC4 — a LEGACY binding (no captured id) behaves exactly as before ────────


def test_a_legacy_binding_without_an_id_still_retires_at_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No migration: a binding written before this fix simply has no fallback. It must
    keep retiring at GRACE — the fix must not accidentally make deletions unretirable."""
    monkeypatch.setenv("RECONCILER_ABSENT_RETIRE_GRACE", "2")
    bs = _store_with_moved_binding(tmp_path, with_id=False)
    client = MovedIssueClient()

    _walk_once(bs, client)
    assert not bs.is_retired(_OLD_KEY), "not before grace"
    _walk_once(bs, client)

    assert bs.is_retired(_OLD_KEY), "a legacy binding must retire at GRACE exactly as before"
    assert client.by_id_lookups == [], (
        "with no captured id there is nothing to ask by, so no lookup may be attempted"
    )


def test_a_genuine_deletion_still_retires_even_with_a_captured_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recovery must not MASK deletions. When the id lookup also fails, the issue
    really is gone and the absence must be recorded exactly as it always was."""
    monkeypatch.setenv("RECONCILER_ABSENT_RETIRE_GRACE", "2")
    bs = _store_with_moved_binding(tmp_path)
    client = MovedIssueClient(resolves=False)

    _walk_once(bs, client)
    _walk_once(bs, client)

    assert bs.is_retired(_OLD_KEY), (
        "an id that does not resolve is a real deletion; a recovery that cannot be "
        "PROVEN must fall through to the unchanged absence bookkeeping"
    )


def test_an_unchanged_key_is_recorded_as_an_absence(tmp_path: Path) -> None:
    """If the id resolves to the SAME key, nothing moved — the 404 was a real absence
    (or a transient the probe already classified) and must still be counted."""
    bs = _store_with_moved_binding(tmp_path)
    client = MovedIssueClient(current_key=_OLD_KEY)

    rekeyed = bs.note_absent_or_rekey(_OLD_KEY, client)

    assert rekeyed is False
    assert bs.all_bindings()["loc-moved"]["absent_404_count"] == 1


# ── AC5 — the happy path must not pay for the recovery path ─────────────────


def test_the_id_fallback_is_not_attempted_while_the_key_resolves(tmp_path: Path) -> None:
    """A fallback firing on the happy path would double every absence probe's cost.
    The probe returns 200, so the recovery must never be entered."""
    bs = _store_with_moved_binding(tmp_path)
    client = MovedIssueClient()

    def probe_alive(_client, _key):
        return {"summary": "alive but out of window", "status": "To Do"}

    _walk_once(bs, client, probe=probe_alive)

    assert client.by_id_lookups == [], (
        "no by-id lookup may be issued while the key resolves normally"
    )
    assert bs.get_jira_key("loc-moved") == _OLD_KEY, "an alive key is not re-keyed"


# ── AC6 — the SHARED store's existing key-taking surface is untouched ────────


def test_no_existing_jira_key_taking_method_changed_signature() -> None:
    """This store is SHARED WITH CLOUD. The fix is additive by construction: the
    write-ahead and absence methods the live Cloud bridge calls must be byte-identical
    in signature, so the capture could not be a new parameter on any of them."""
    expected = {
        "get_jira_key": ["self", "local_id"],
        "get_local_id": ["self", "jira_key"],
        "bind_confirm": ["self", "local_id", "jira_key"],
        "record_pending_key": ["self", "local_id", "jira_key"],
        "bind_pending": ["self", "local_id"],
        "unbind": ["self", "local_id"],
        "note_absent": ["self", "jira_key"],
        "clear_absent": ["self", "jira_key"],
        "set_last_get": ["self", "jira_key", "pass_id"],
        "is_retired": ["self", "jira_key"],
    }
    for name, params in expected.items():
        actual = list(inspect.signature(getattr(BindingStore, name)).parameters)
        assert actual == params, (
            f"BindingStore.{name} must keep its pre-7c26 signature (shared with the "
            f"live Cloud bridge); got {actual}"
        )


def test_the_capture_is_a_separate_additive_method() -> None:
    """The two new members exist and are the ONLY new surface the capture needs."""
    assert callable(BindingStore.record_jira_id)
    assert callable(BindingStore.get_jira_id)
    assert list(inspect.signature(BindingStore.record_jira_id).parameters) == [
        "self",
        "local_id",
        "jira_id",
    ]


# ── capture — the id is recorded, and never invented ─────────────────────────


def test_get_jira_id_is_none_for_a_legacy_binding(tmp_path: Path) -> None:
    """None means "not captured yet" and is VALID — it is what disables the fallback
    for pre-fix bindings without any migration."""
    bs = _store_with_moved_binding(tmp_path, with_id=False)
    assert bs.get_jira_id("loc-moved") is None
    assert bs.get_jira_id("no-such-local-id") is None


def test_record_jira_id_ignores_an_unbound_id_or_an_empty_value(tmp_path: Path) -> None:
    """A create response with no ``id`` must not write an empty marker that would make
    the fallback issue a lookup for ``""``."""
    bs = _store_with_moved_binding(tmp_path, with_id=False)
    bs.record_jira_id("loc-moved", "")
    assert bs.get_jira_id("loc-moved") is None
    bs.record_jira_id("not-bound", "999")
    assert "not-bound" not in bs.all_bindings()
