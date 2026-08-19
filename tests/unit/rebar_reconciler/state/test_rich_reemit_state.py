"""Story 3388 — the ``rich_sha`` / ``rich_reemit`` pair that bounds a lossy body.

The Data Center codec is one-way and lossy, so a description is not guaranteed to
reach a codec fixed point. The differ can decide the local body still differs from
the baseline, push an identical wire, and decide the same thing again next pass.
Under the plain wire that cannot happen, so nothing in the existing design detects
it.

Two pieces of inline binding state close that hole, and both are pinned here:

* ``rich_sha`` — the digest of the description wire we last pushed. Change-gated
  and fixed-size, because it rides on every binding in every committed version of
  the store (epic ``0303``'s churn discipline: a per-pass timestamp there is what
  produced the 12.62 KB anti-pattern).
* ``rich_reemit`` — how many times in a row that identical wire has gone out.
  Never stored while it is zero, so a healthy body adds nothing at all.

At ``RICH_REEMIT_OBSERVE_AT`` the apply path reads the body back ONCE and hands
what Jira actually stored to ``_advance_baselines`` as a ``synced_fields``
overlay. The route matters as much as the value: ``_advance_baselines`` stays the
SOLE baseline writer, so the observation reaches the baseline through
``merge_baseline`` exactly like every other confirmed write, and there is no
second place that can write a baseline out from under it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

_ENGINE = Path(__file__).resolve().parents[4] / "src" / "rebar" / "_engine"
if str(_ENGINE) not in sys.path:  # pragma: no cover - import bootstrap
    sys.path.insert(0, str(_ENGINE))

from rebar_reconciler import apply_handlers, peer_state  # noqa: E402
from rebar_reconciler.binding_store import BindingStore  # noqa: E402
from rebar_reconciler.reconcile import _advance_baselines  # noqa: E402

_WIRE = "h1. Heading\n\n* alpha\n"
_OTHER_WIRE = "h1. Heading\n\n* alpha\n* beta\n"


class _Cfg:
    """Minimal stand-in for the typed config's reconciler section."""

    def __init__(self, value: str) -> None:
        self.reconciler = type("R", (), {"rich_text_cutover": value})()


@pytest.fixture
def set_flag(monkeypatch: pytest.MonkeyPatch):
    """Set ``reconciler.rich_text_cutover`` as the resolver will read it."""

    def _set(value: str) -> None:
        import rebar.config

        monkeypatch.setattr(rebar.config, "load_config", lambda *a, **k: _Cfg(value))

    return _set


class _Client:
    """A transport whose only job is to record how often the body was read back."""

    def __init__(self, description: Any = "OBSERVED body", boom: bool = False) -> None:
        self.description = description
        self.boom = boom
        self.gets: list[str] = []

    def get_issue_by_rest(self, jira_key: str) -> dict[str, Any]:
        self.gets.append(jira_key)
        if self.boom:
            raise RuntimeError("transport down")
        return {"fields": {"description": self.description}}


def _store(tmp_path: Path) -> BindingStore:
    s = BindingStore(tmp_path / ".tickets-tracker")
    s.bind_confirm("loc-1", "REB-1")
    return s


def _entry(store: BindingStore) -> dict[str, Any]:
    return store.all_bindings()["loc-1"]


def _ctx(store: BindingStore, client: _Client, tmp_path: Path) -> apply_handlers.BatchApplyContext:
    return apply_handlers.BatchApplyContext(
        client=client, repo_root=tmp_path, pass_id="p1", binding_store=store
    )


def _push(ctx: apply_handlers.BatchApplyContext, wire: Any = _WIRE) -> None:
    """One CONFIRMED description push, exactly as ``handle_update`` records it."""
    synced = {"description": wire}
    ctx.synced_fields.setdefault("loc-1", {}).update(synced)
    apply_handlers._observe_rich_reemit(ctx, "loc-1", "REB-1", synced)


# --- 1. the digest --------------------------------------------------------------


def test_rich_sha_is_eight_bytes_and_deterministic() -> None:
    """8 bytes is the whole point: this rides on every binding, every version."""
    sha = peer_state.rich_sha(_WIRE)
    assert len(sha) == 16  # 16 hex chars == 8 bytes
    assert int(sha, 16) >= 0  # hex, not an arbitrary string
    assert sha == peer_state.rich_sha(_WIRE)
    assert sha != peer_state.rich_sha(_OTHER_WIRE)


def test_rich_sha_takes_cloud_adf_dicts_and_ignores_key_order() -> None:
    """Cloud's wire is an ADF dict, DC's a string; only the CONTENT may move the digest.

    Without the sorted-key serialization a re-encode that emitted the same document
    with its keys in another order would read as a changed body and reset the
    counter, which is precisely the loop this state exists to detect.
    """
    adf = {"type": "doc", "version": 1, "content": [{"type": "paragraph"}]}
    reordered = {"content": [{"type": "paragraph"}], "version": 1, "type": "doc"}
    assert peer_state.rich_sha(adf) == peer_state.rich_sha(reordered)
    assert peer_state.rich_sha(adf) != peer_state.rich_sha({"type": "doc", "version": 2})


# --- 2. the counter -------------------------------------------------------------


def test_counter_stays_absent_for_a_body_that_converges(tmp_path: Path) -> None:
    """The healthy case: pushed once, never again. Zero is stored as ABSENCE."""
    store = _store(tmp_path)
    entry = _entry(store)
    assert peer_state.note_rich_emit(entry, _WIRE) == 0
    assert entry["rich_sha"] == peer_state.rich_sha(_WIRE)
    assert "rich_reemit" not in entry


def test_counter_climbs_only_while_the_wire_is_unchanged(tmp_path: Path) -> None:
    store = _store(tmp_path)
    entry = _entry(store)
    assert [peer_state.note_rich_emit(entry, _WIRE) for _ in range(3)] == [0, 1, 2]
    assert entry["rich_reemit"] == 2
    # A genuinely edited body must NOT inherit the previous body's history.
    assert peer_state.note_rich_emit(entry, _OTHER_WIRE) == 0
    assert "rich_reemit" not in entry
    assert entry["rich_sha"] == peer_state.rich_sha(_OTHER_WIRE)


def test_an_entry_mutation_survives_the_store_save_reload_round_trip(tmp_path: Path) -> None:
    """``all_bindings`` copies only the OUTER mapping — the contract the writer relies on.

    If that ever became a deep copy the state would be written to a throwaway dict
    and silently lost, so this pins the property rather than the implementation
    detail's current spelling.
    """
    store = _store(tmp_path)
    peer_state.note_rich_emit(_entry(store), _WIRE)
    store.save()
    reloaded = BindingStore(tmp_path / ".tickets-tracker")
    assert reloaded.all_bindings()["loc-1"]["rich_sha"] == peer_state.rich_sha(_WIRE)


# --- 3. churn: a no-op pass must write nothing ----------------------------------


def test_a_pass_that_pushes_no_description_changes_zero_entries(tmp_path: Path) -> None:
    """The AC's cost bar, asserted on the bytes the store would commit."""
    store = _store(tmp_path)
    peer_state.note_rich_emit(_entry(store), _WIRE)
    store.save()
    path = tmp_path / ".tickets-tracker" / ".bridge_state" / "bindings.json"
    before = path.read_bytes()

    # A pass with no confirmed description push never reaches note_rich_emit.
    reloaded = BindingStore(tmp_path / ".tickets-tracker")
    reloaded.save()
    assert path.read_bytes() == before


def test_the_state_pair_costs_well_under_the_per_version_budget(tmp_path: Path) -> None:
    """≤0.07 KB per stable version, measured as the serialized per-binding delta."""
    store = _store(tmp_path)
    entry = _entry(store)
    baseline_bytes = len(json.dumps(entry))
    peer_state.note_rich_emit(entry, _WIRE)
    peer_state.note_rich_emit(entry, _WIRE)
    assert len(json.dumps(entry)) - baseline_bytes <= 70


# --- 4. the counter ends when the baseline body moves ---------------------------


@pytest.mark.parametrize("writer", ["set_baseline", "merge_baseline"])
def test_a_refreshed_body_ends_the_reemit_episode(tmp_path: Path, writer: str) -> None:
    """Fresh evidence of what Jira stores IS what the counter was waiting for."""
    store = _store(tmp_path)
    entry = _entry(store)
    store.set_baseline("loc-1", {"description": "OLD body"})
    peer_state.note_rich_emit(entry, _WIRE)
    peer_state.note_rich_emit(entry, _WIRE)
    assert entry["rich_reemit"] == 1

    getattr(store, writer)("loc-1", {"description": "MOVED body"})
    assert "rich_reemit" not in entry
    # The digest records the last wire we SENT; a baseline refresh does not change it.
    assert entry["rich_sha"] == peer_state.rich_sha(_WIRE)


def test_a_baseline_write_for_another_field_does_not_end_the_episode(tmp_path: Path) -> None:
    """Otherwise an unrelated field churning beside a body would mask the loop forever."""
    store = _store(tmp_path)
    entry = _entry(store)
    store.set_baseline("loc-1", {"description": "OLD body", "status": "To Do"})
    peer_state.note_rich_emit(entry, _WIRE)
    peer_state.note_rich_emit(entry, _WIRE)

    store.merge_baseline("loc-1", {"status": "Done"})
    assert entry["rich_reemit"] == 1


# --- 5. the flag, read twice ----------------------------------------------------


@pytest.mark.parametrize("value", ["off", "cloud", "dc", "both", "nonsense"])
def test_the_core_flag_read_agrees_with_the_codecs(value: str, set_flag) -> None:
    """Core and the adapter answer the same question from two independent reads.

    ``apply_handlers`` cannot call ``rich_text.cutover_clients``: the vendor
    package's dependency direction is one-way, and importing it from core would
    invert the layering (the same reason ``config.local_to_jira_status`` is a
    second literal of the adapter's status map rather than an import). What keeps
    a second read honest there is a parity test, so this is that test — including
    the unparseable value, where both must fail CLOSED.
    """
    from rebar_reconciler.adapters.jira_family.rich_text import cutover_clients

    set_flag(value)
    assert apply_handlers._rich_cutover_active() == bool(cutover_clients())


def test_both_flag_readers_fail_closed_when_config_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config fault must never switch the observation on."""
    import rebar.config

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise rebar.config.ConfigError("unreadable")

    monkeypatch.setattr(rebar.config, "load_config", _boom)
    assert apply_handlers._rich_cutover_active() is False

    # A config object predating the key must also fail CLOSED rather than raise:
    # the engine ships as package data and can meet an older rebar, and callers
    # substitute partial config objects.
    partial = type("Cfg", (), {"reconciler": type("R", (), {})()})()
    monkeypatch.setattr(rebar.config, "load_config", lambda *a, **k: partial)
    assert apply_handlers._rich_cutover_active() is False


# --- 6. the bounded observation ------------------------------------------------


def test_the_plain_wire_path_is_untouched_when_the_flag_is_off(tmp_path: Path, set_flag) -> None:
    """Defaults OFF: no digest, no counter, no GET — byte-identical to before."""
    set_flag("off")
    store, client = _store(tmp_path), _Client()
    ctx = _ctx(store, client, tmp_path)
    for _ in range(4):
        _push(ctx)
    assert client.gets == []
    assert "rich_sha" not in _entry(store)


def test_a_looping_body_is_read_back_exactly_once(tmp_path: Path, set_flag) -> None:
    """One GET per divergence episode — the threshold is an equality, not a floor."""
    set_flag("dc")
    store, client = _store(tmp_path), _Client()
    ctx = _ctx(store, client, tmp_path)

    _push(ctx)
    _push(ctx)
    assert client.gets == []  # still explicable as one pass of lag

    _push(ctx)
    assert client.gets == ["REB-1"]
    assert ctx.synced_fields["loc-1"]["description"] == "OBSERVED body"

    _push(ctx)  # the episode does not re-charge
    assert client.gets == ["REB-1"]


def test_the_observation_reaches_the_baseline_through_advance_baselines(
    tmp_path: Path, set_flag
) -> None:
    """The sole-writer invariant: the overlay lands via ``_advance_baselines`` alone.

    Nothing in the apply path writes a baseline; it only records what it synced.
    Driving the REAL advance here is what proves the observed value actually
    arrives — and arrives by the same route as every other confirmed write.
    """
    set_flag("dc")
    store, client = _store(tmp_path), _Client()
    ctx = _ctx(store, client, tmp_path)
    for _ in range(3):
        _push(ctx)
    assert store.get_baseline("loc-1") in (None, {})  # untouched by the apply path

    _advance_baselines(store, {}, ctx.synced_fields)
    assert store.get_baseline("loc-1")["description"] == "OBSERVED body"


def test_a_failed_read_back_leaves_the_pushed_value_in_place(tmp_path: Path, set_flag) -> None:
    """Fail-open: losing the observation costs convergence speed, never the pass."""
    set_flag("dc")
    store, client = _store(tmp_path), _Client(boom=True)
    ctx = _ctx(store, client, tmp_path)
    for _ in range(3):
        _push(ctx)
    assert client.gets == ["REB-1"]
    assert ctx.synced_fields["loc-1"]["description"] == _WIRE


def test_a_store_predating_the_fields_degrades_instead_of_raising(tmp_path: Path, set_flag) -> None:
    """An older store / test double must not take the pass down mid-apply."""
    set_flag("dc")

    class _OldStore:
        def all_bindings(self) -> dict[str, dict]:
            raise AttributeError("no such thing here")

    client = _Client()
    ctx = apply_handlers.BatchApplyContext(
        client=client, repo_root=tmp_path, pass_id="p1", binding_store=_OldStore()
    )
    for _ in range(3):
        _push(ctx)
    assert client.gets == []
    assert ctx.synced_fields["loc-1"]["description"] == _WIRE


# ===========================================================================
# RP-02 S2 T3 (morose-selfaware-unicorn) — the narrow rich-emission seam.
#
# Production reaches a binding entry through the SHALLOW `all_bindings()` query
# and mutates it in place. That works only because the outer copy shares inner
# entries, and it bypasses lifecycle policy entirely. This adds a named facade
# operation as the supported mutation seam. The caller cutover is RP-02 S3 T3.
#
# The oracle is DIFFERENTIAL: the new operation must agree with the legacy
# sequence on hash, counter and persisted bytes, for changed / unchanged /
# missing input. Critically it must perform NO save of its own — the production
# caller never saves per emit and relies on the pass's later unconditional save,
# so adding one would be write amplification, not parity.
# ===========================================================================

import json as _json  # noqa: E402
from pathlib import Path as _Path  # noqa: E402
from typing import Any as _Any  # noqa: E402

import pytest as _pytest  # noqa: E402

from rebar_reconciler import peer_state as _ps  # noqa: E402
from rebar_reconciler.binding_store import BindingStore as _Store  # noqa: E402


def _tracker_dir(root: _Path) -> _Path:
    return root / ".tickets-tracker"


def _live(root: _Path) -> _Path:
    return _tracker_dir(root) / ".bridge_state" / "bindings.json"


def _bound_store(root: _Path) -> _Store:
    store = _Store(_tracker_dir(root))
    store.bind_confirm("loc-A", "DIG-A")
    store.save()
    return store


def _legacy_note(store: _Store, local_id: str, wire: _Any) -> int | None:
    """The sequence production runs today: shallow query, then mutate in place."""
    entry = store.all_bindings().get(local_id)
    if not isinstance(entry, dict):
        return None
    return _ps.note_rich_emit(entry, wire)


def test_narrow_operation_matches_the_legacy_counter_progression(tmp_path: _Path) -> None:
    """0 on a first push, then 1, then 2 for the same wire — and the equality threshold the
    caller compares against is reached at exactly the same emit as before."""
    legacy_root, new_root = tmp_path / "legacy", tmp_path / "new"
    legacy, new = _bound_store(legacy_root), _bound_store(new_root)
    wire = {"type": "doc", "content": [{"text": "hello"}]}

    legacy_counts = [_legacy_note(legacy, "loc-A", wire) for _ in range(3)]
    new_counts = [new.note_rich_emit("loc-A", wire) for _ in range(3)]

    assert legacy_counts == [0, 1, 2]
    assert new_counts == legacy_counts
    assert new_counts[-1] == _ps.RICH_REEMIT_OBSERVE_AT


def test_narrow_operation_persists_the_same_bytes_as_the_legacy_sequence(
    tmp_path: _Path,
) -> None:
    """Byte equivalence is asserted after ONE save, not per emit: neither path saves during
    emission, so the comparison is 'same state, then the pass's single save'."""
    legacy_root, new_root = tmp_path / "legacy", tmp_path / "new"
    legacy, new = _bound_store(legacy_root), _bound_store(new_root)
    wire = "h1. Heading\n\nbody"

    for _ in range(2):
        _legacy_note(legacy, "loc-A", wire)
        new.note_rich_emit("loc-A", wire)
    legacy.save()
    new.save()

    assert _live(new_root).read_bytes() == _live(legacy_root).read_bytes()
    entry = _json.loads(_live(new_root).read_text(encoding="utf-8"))["bindings"]["loc-A"]
    assert entry["rich_sha"] == _ps.rich_sha(wire)
    assert entry["rich_reemit"] == 1


def test_a_changed_wire_resets_the_counter_and_rewrites_the_digest(tmp_path: _Path) -> None:
    """Change-gated in BOTH directions: a genuinely edited body must not inherit the previous
    body's re-emit history, or an edit would be mistaken for a non-converging loop."""
    store = _bound_store(tmp_path)
    store.note_rich_emit("loc-A", "first")
    store.note_rich_emit("loc-A", "first")

    assert store.note_rich_emit("loc-A", "second") == 0
    store.save()

    entry = _json.loads(_live(tmp_path).read_text(encoding="utf-8"))["bindings"]["loc-A"]
    assert entry["rich_sha"] == _ps.rich_sha("second")
    assert "rich_reemit" not in entry, "a differing wire clears the counter, absent means zero"


def test_a_converging_body_never_stores_the_counter(tmp_path: _Path) -> None:
    """Epic 0303 churn discipline: a body pushed once and never again keeps the count at 0,
    so `rich_reemit` is never written and a no-op pass stays at zero changed entries."""
    store = _bound_store(tmp_path)

    assert store.note_rich_emit("loc-A", "converged") == 0
    store.save()

    entry = _json.loads(_live(tmp_path).read_text(encoding="utf-8"))["bindings"]["loc-A"]
    assert "rich_reemit" not in entry


def test_a_missing_binding_is_nonfatal_and_distinguishable(tmp_path: _Path) -> None:
    """Today's caller resolves the entry and simply returns when it is absent. The operation
    must be nonfatal and return something a caller can tell apart from a count — 0 would be
    read as 'first push of this wire' and trigger real work for a binding that is not there."""
    store = _bound_store(tmp_path)

    result = store.note_rich_emit("loc-MISSING", "wire")

    assert result is None
    assert result != 0
    assert _legacy_note(store, "loc-MISSING", "wire") is None


def test_the_operation_performs_no_save_at_either_layer(tmp_path: _Path) -> None:
    """No per-emit save on ANY path, at EITHER layer.

    The production caller mutates in place and relies on the pass's later unconditional
    save, so introducing a save here would be write amplification — a whole-store rewrite
    plus fsync per description push — rather than parity.

    Both the facade and the repository save are counted. Watching only the facade was a
    mutation-proven tautology: the policy owner holds the repository directly, so a
    `self._repo.save()` slipped into the operation never touches `BindingStore.save` and
    was invisible to the narrower spy.
    """
    from rebar_reconciler.binding_repository import BindingRepository

    store = _bound_store(tmp_path)
    saves: list[str] = []
    real_store_save = type(store).save
    real_repo_save = BindingRepository.save

    def store_save(self: _Any) -> None:
        saves.append("facade")
        real_store_save(self)

    def repo_save(self: _Any) -> None:
        saves.append("repository")
        real_repo_save(self)

    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(type(store), "save", store_save)
        mp.setattr(BindingRepository, "save", repo_save)
        store.note_rich_emit("loc-A", "w1")
        store.note_rich_emit("loc-A", "w1")
        store.note_rich_emit("loc-A", "w2")
        store.note_rich_emit("loc-MISSING", "w3")

    assert saves == [], f"the narrow operation must never save; the pass owns that (got {saves})"


def test_all_bindings_copy_depth_is_unchanged_by_the_new_seam(tmp_path: _Path) -> None:
    """The narrow operation is ADDITIVE. `all_bindings()` stays shallow so existing callers
    keep working; only the later caller cutover removes the mutation-through-query pattern."""
    store = _bound_store(tmp_path)

    snapshot = store.all_bindings()
    assert snapshot is not store.all_bindings()
    store.note_rich_emit("loc-A", "wire")

    assert store.all_bindings()["loc-A"]["rich_sha"] == _ps.rich_sha("wire")
    assert snapshot["loc-A"]["rich_sha"] == _ps.rich_sha("wire"), (
        "inner entries stay shared — that is the shallow contract this slice preserves"
    )


def test_the_narrow_operation_does_not_expose_the_owners(tmp_path: _Path) -> None:
    """The facade stays authoritative: adding a seam must not hand out a writable owner."""
    from rebar_reconciler.binding_lifecycle import BindingLifecycle
    from rebar_reconciler.binding_repository import BindingRepository

    store = _bound_store(tmp_path)

    for name in dir(store):
        if name.startswith("_"):
            continue
        member = getattr(store, name, None)
        assert not isinstance(member, (BindingLifecycle, BindingRepository)), name


# --- 9. RP-02 S3 T3: production no longer mutates through the query --------------
#
# S2 T3 added ``BindingStore.note_rich_emit`` as the named door onto this state; this
# section is the oracle for the PRODUCTION caller finally walking through it. The tests
# above are the compatibility half and must pass UNCHANGED — the cutover is required to be
# behaviour-preserving, so a change in the counter progression, the single read-back, the
# baseline route or the fail-open branches would be a regression, not an improvement.


class _AllBindingsWatcher(_Store):
    """A real store that counts every ``all_bindings()`` call made against it.

    A counting subclass rather than a poisoned return value, because the interesting
    assertion is that the shallow query is not CONSULTED at all. Poisoning what it returns
    would only prove the mutation fails, and ``_observe_rich_reemit`` swallows exceptions
    by design — the failure would be indistinguishable from a transport fault.
    """

    def __init__(self, tracker_dir: _Path) -> None:
        super().__init__(tracker_dir)
        self.all_bindings_calls = 0

    def all_bindings(self) -> dict[str, dict]:
        self.all_bindings_calls += 1
        return super().all_bindings()


def _watching_store(tmp_path: _Path) -> _AllBindingsWatcher:
    store = _AllBindingsWatcher(tmp_path / ".tickets-tracker")
    store.bind_confirm("loc-1", "REB-1")
    return store


def test_the_production_push_never_reaches_a_binding_through_all_bindings(
    tmp_path: _Path, set_flag
) -> None:
    """The point of this task. ``all_bindings()`` is a READ-shaped query, and reaching a
    live entry through it to write is an unowned write seam — the store cannot enforce any
    invariant on a mutation it never saw.
    """
    set_flag("dc")
    store = _watching_store(tmp_path)
    ctx = _ctx(store, _Client(), tmp_path)

    _push(ctx)

    assert store.all_bindings_calls == 0


def test_the_production_push_records_through_the_narrow_operation(
    tmp_path: _Path, set_flag, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not merely "does not use the query" but "uses the named door" — the two are
    different claims, and only the second one survives someone deleting the call.

    This binds to the method NAME deliberately. "Record rich-emission state through the
    facade's narrow operation" is the acceptance criterion, not an implementation detail
    that happens to satisfy it, and ``note_rich_emit`` is a public method on the store that
    the reconciler, the adapters and ``bridge fsck`` all bind to. Renaming it IS a contract
    change and should break a test; what must not break this is a change to how the
    operation computes its answer, which the spy passes straight through.
    """
    set_flag("dc")
    store = _watching_store(tmp_path)
    seen: list[tuple[str, Any]] = []
    real = type(store).note_rich_emit

    def _spy(inner: Any, local_id: str, wire: Any) -> Any:
        seen.append((local_id, wire))
        return real(inner, local_id, wire)

    monkeypatch.setattr(type(store), "note_rich_emit", _spy)
    ctx = _ctx(store, _Client(), tmp_path)

    _push(ctx)

    assert seen == [("loc-1", _WIRE)]


def test_the_cutover_preserves_the_read_back_threshold(tmp_path: _Path, set_flag) -> None:
    """The compatibility claim, restated against the watching store so it is pinned on the
    post-cutover path specifically and not only through the shared fixtures above."""
    set_flag("dc")
    store = _watching_store(tmp_path)
    client = _Client()
    ctx = _ctx(store, client, tmp_path)

    _push(ctx)
    _push(ctx)
    assert client.gets == []

    _push(ctx)
    assert client.gets == ["REB-1"]
    assert store.all_bindings_calls == 0


def test_a_missing_binding_still_skips_the_read_back_after_the_cutover(
    tmp_path: _Path, set_flag
) -> None:
    """The fail-open branch that the narrow operation reports as ``None`` rather than 0.

    ``0`` would read as "first push of this wire" and, on a threshold of one, could trigger
    a read-back for a binding that is not there. An unbound id must simply do nothing.
    """
    set_flag("dc")
    store = _AllBindingsWatcher(tmp_path / ".tickets-tracker")
    client = _Client()
    ctx = _ctx(store, client, tmp_path)

    synced = {"description": _WIRE}
    ctx.synced_fields.setdefault("loc-absent", {}).update(synced)
    apply_handlers._observe_rich_reemit(ctx, "loc-absent", "REB-9", synced)

    assert client.gets == []
    assert store.all_bindings_calls == 0


def test_the_call_site_makes_no_false_claim_about_the_facade(tmp_path: _Path) -> None:
    """The docstring justified mutation-through-query with a claim that S2 T3 falsified.

    It said ``binding_store.py`` "sits at the module-size cap and cannot carry a narrower
    accessor". A narrower accessor now exists AND that file is no longer at the cap, so
    the sentence was doubly wrong and is exactly the kind of stale justification that
    keeps a workaround alive after its reason has gone.

    Asserted as source text because there is no other way: the absence of a false comment
    has no runtime behaviour to observe. That makes this a deliberately narrow assertion —
    it pins one retired sentence, not the docstring's wording — and its value is
    preventing the justification from being restored alongside a revert of the cut, which
    is exactly how this workaround survived its own obsolescence the first time.
    """
    source = (_ENGINE / "rebar_reconciler" / "apply_handlers.py").read_text(encoding="utf-8")

    assert "cannot carry a narrower accessor" not in source


# --- 10. the production mutation-through-query census ---------------------------

#: Production modules permitted to call ``all_bindings()``, each with the reason it is a
#: READ. The census is an allowlist rather than a ban because the query is legitimate for
#: iteration — what it must never be is a write seam. Keying on classification instead of
#: absence means a NEW caller cannot appear silently: it fails this test and someone has to
#: decide, in writing, which kind it is. Every entry below was verified by reading the call
#: site: each iterates or snapshots, and every write in those modules goes through a named
#: facade method (``set_baseline``, ``merge_baseline``, ``unbind``).
_ALL_BINDINGS_READERS = {
    "_engine/rebar_reconciler/binding_walk.py": (
        "iterates entries to classify off-snapshot bindings; phase 1 is read-only by design"
    ),
    "_engine/rebar_reconciler/reconcile_helpers.py": (
        "iterates to advance baselines; writes go through set_baseline / merge_baseline"
    ),
    "_engine/rebar_reconciler/reconcile_check.py": (
        "iterates to build the discrepancy report for a read-only command"
    ),
    "_commands/bridge_repair.py": (
        "before/after snapshots of the outer map, used as a refusal guard; the prune "
        "itself goes through unbind()"
    ),
}


def _production_all_bindings_callers() -> set[str]:
    """Every production module containing a real ``x.all_bindings()`` CALL.

    Parsed, not grepped. A substring search cannot tell a call from a mention, and this
    codebase discusses ``all_bindings()`` in prose constantly — the shallow-copy contract
    is documented on ``peer_state``, on the facade's own method, and on the lifecycle
    owner that replaced it as the write door. Those are the files most likely to talk about
    it and least likely to misuse it, so a text match would fill the allowlist with
    docstrings and leave no room to notice a real new caller.
    """
    import ast

    root = _Path(__file__).resolve().parents[4] / "src" / "rebar"
    callers: set[str] = set()
    for path in root.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover — a parse failure is its own gate's problem
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "all_bindings"
            ):
                callers.add(path.relative_to(root).as_posix())
                break
    return callers


def test_no_production_module_outside_the_read_allowlist_calls_all_bindings() -> None:
    """AC3, as a standing gate rather than a one-off inspection.

    ``apply_handlers.py`` leaving this set IS the deliverable of this task, so its absence
    is asserted by the equality below rather than as a separate check.
    """
    assert _production_all_bindings_callers() == set(_ALL_BINDINGS_READERS)


def test_every_allowlisted_reader_carries_a_written_reason() -> None:
    """An allowlist without reasons decays into a list of exceptions nobody can audit."""
    assert all(reason.strip() for reason in _ALL_BINDINGS_READERS.values())
