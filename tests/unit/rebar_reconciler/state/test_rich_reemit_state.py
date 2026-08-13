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
