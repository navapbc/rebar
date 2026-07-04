"""Binding-store-driven acting walk — drift classes A + C (epic 3006-e198).

RED-first cells for the ONE shared walk that heals:

* **class A (444d)** — a locally archived/deleted ticket drives its live Jira issue
  to Done in one pass (``TERMINAL_TRANSITION``); an already-Done issue is a no-op.
* **class C (13eb)** — a confirmed binding whose Jira issue is a confirmed 404
  retires after GRACE consecutive misses (reversible soft-delete), even though the
  local ticket is archived (invisible to the active-snapshot differ). Regression
  guards: an out-of-window ALIVE key never retires; a transport error defers
  WITHOUT advancing grace; the circuit breaker refuses a mass-retire/transition
  pass BEFORE any mutation.

Follows the reconciler test-tree loader convention (spec_from_file_location).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parents[3] / "src" / "rebar" / "_engine" / "rebar_reconciler"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _SRC_DIR / filename)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_walk = _load("_binding_walk_under_test", "binding_walk.py")
_bs = _load("_binding_store_for_walk", "binding_store.py")
_classify = _load("_classify_for_walk", "classify.py")
_mutation = _load("_mutation_for_walk", "mutation.py")
_ob = _load("_ob_for_walk", "outbound_differ.py")
BindingStore = _bs.BindingStore


def compute(*args, **kwargs):
    """Invoke the walk with the engine sibling modules injected (avoids the
    pytest test-package import shadow on ``rebar_reconciler.classify``)."""
    kwargs.setdefault("classify_mod", _classify)
    kwargs.setdefault("mutation_mod", _mutation)
    kwargs.setdefault("outbound_differ_mod", _ob)
    return _walk.compute_binding_walk_mutations(*args, **kwargs)


# A non-None sentinel so the walk takes the direct-GET branch; the injected
# probe_get ignores it, so its identity is irrelevant.
_CLIENT = object()


def _store(tmp_path: Path, bindings: dict[str, str]) -> BindingStore:
    bs = BindingStore(tmp_path / ".tickets-tracker")
    for local_id, jira_key in bindings.items():
        bs.bind_confirm(local_id, jira_key)
    bs.save()
    return bs


def _archived_reader(states: dict[str, dict | None]):
    """Return a local_reader over a fixed ``local_id → ticket dict | None`` map."""
    return lambda lid: states.get(lid)


def _archived_local(local_id: str) -> dict:
    return {"ticket_id": local_id, "status": "archived", "archived": True}


# ── class A — terminal transition ────────────────────────────────────────────


def test_class_a_archived_local_drives_jira_to_done(tmp_path: Path) -> None:
    """DD1(444d): archived-local bound to a live 'To Do' Jira issue → one
    outbound update {status: Done} in a single pass."""
    bs = _store(tmp_path, {"loc-A": "REB-464"})
    curr = {"REB-464": {"status": "To Do", "summary": "x"}}
    result = compute(
        bs,
        curr,
        active_local_ids=set(),  # archived → not in the active snapshot
        client=None,  # present in-window, no GET needed
        local_reader=_archived_reader({"loc-A": _archived_local("loc-A")}),
        max_acting_fraction=1.0,
    )
    assert len(result.mutations) == 1, "exactly one terminal-transition mutation"
    m = result.mutations[0]
    assert m.target == "REB-464"
    assert m.action.value == "update"
    assert m.direction.value == "outbound"
    assert m.payload["changed_fields"]["status"] == "Done"
    assert m.provenance["drift_class"] == "A"
    assert not result.retired


def test_class_a_already_done_is_noop(tmp_path: Path) -> None:
    """A locally-terminal ticket whose Jira issue is ALREADY Done needs no
    transition (idempotent steady state)."""
    bs = _store(tmp_path, {"loc-A2": "REB-9"})
    curr = {"REB-9": {"status": "Done"}}
    result = compute(
        bs,
        curr,
        active_local_ids=set(),
        client=None,
        local_reader=_archived_reader({"loc-A2": _archived_local("loc-A2")}),
        max_acting_fraction=1.0,
    )
    assert result.mutations == [], "already-Done issue must not be re-transitioned"


def test_active_local_binding_is_skipped(tmp_path: Path) -> None:
    """A binding whose local id is in the ACTIVE snapshot is owned by the field
    differ; the walk must not double-process it."""
    bs = _store(tmp_path, {"loc-live": "REB-1"})
    curr = {"REB-1": {"status": "To Do"}}
    result = compute(
        bs,
        curr,
        active_local_ids={"loc-live"},
        client=None,
        local_reader=_archived_reader({}),  # never consulted
        max_acting_fraction=1.0,
    )
    assert result.mutations == []
    assert not result.retired


# ── class C — probe → grace → retire (reversible) ────────────────────────────


def test_class_c_confirmed_404_retires_after_grace(tmp_path: Path) -> None:
    """DD1/DD3(13eb): archived-local bound to a DELETED Jira key (confirmed 404)
    retires after GRACE (=3) consecutive misses; entry lands in
    bindings-retired.json (reversible)."""
    bs = _store(tmp_path, {"loc-C": "REB-530"})
    reader = _archived_reader({"loc-C": _archived_local("loc-C")})

    def probe_404(_client, _key):
        return _ob._DELETED

    retired = None
    for i in range(3):
        result = compute(
            bs,
            {},  # REB-530 absent from the window
            active_local_ids=set(),
            client=_CLIENT,
            local_reader=reader,
            max_acting_fraction=1.0,
            probe_get=probe_404,
        )
        if i < 2:
            assert not bs.is_retired("REB-530"), f"must not retire before grace (pass {i})"
        retired = result.retired
    assert bs.is_retired("REB-530"), "must retire at GRACE consecutive confirmed-404s"
    assert "REB-530" in retired
    retired_path = tmp_path / ".tickets-tracker" / ".bridge_state" / "bindings-retired.json"
    data = json.loads(retired_path.read_text())
    assert "REB-530" in data["retired"]
    assert data["retired"]["REB-530"]["local_id"] == "loc-C"


def test_class_c_out_of_window_but_alive_never_retires(tmp_path: Path) -> None:
    """Regression (ADR 0028 §1 / L17): a key absent from the window but ALIVE on a
    direct GET (200) is NOT deleted — never retires, and its absence counter is
    reset. Because the local is archived, it instead drives the live issue to
    Done (class A)."""
    bs = _store(tmp_path, {"loc-alive": "REB-77"})
    reader = _archived_reader({"loc-alive": _archived_local("loc-alive")})

    def probe_alive(_client, _key):
        return {"status": "To Do", "summary": "still here"}

    for _ in range(4):
        result = compute(
            bs,
            {},
            active_local_ids=set(),
            client=_CLIENT,
            local_reader=reader,
            max_acting_fraction=1.0,
            probe_get=probe_alive,
        )
    assert not bs.is_retired("REB-77"), "an alive out-of-window key must never retire"
    assert len(result.mutations) == 1, "alive + archived-local → terminal transition"
    assert result.mutations[0].payload["changed_fields"]["status"] == "Done"


def test_class_c_transport_error_defers_without_advancing_grace(tmp_path: Path) -> None:
    """DD2(13eb): a transport error is not evidence of deletion — defer, and NEVER
    advance the grace counter (else a Jira outage would mass-retire)."""
    bs = _store(tmp_path, {"loc-T": "REB-88"})
    reader = _archived_reader({"loc-T": _archived_local("loc-T")})

    def probe_transport(_client, _key):
        return _ob._TRANSPORT_ERROR

    for _ in range(5):
        compute(
            bs,
            {},
            active_local_ids=set(),
            client=_CLIENT,
            local_reader=reader,
            max_acting_fraction=1.0,
            probe_get=probe_transport,
        )
    assert not bs.is_retired("REB-88"), "transport error must never retire"
    # Grace counter must be untouched: a single real 404 afterwards is miss #1.
    entry = bs.all_bindings()["loc-T"]
    assert int(entry.get("absent_404_count", 0)) == 0


# ── circuit breaker — refuse mass-retire/transition BEFORE any mutation ───────


def test_breaker_refuses_mass_retire_before_mutation(tmp_path: Path) -> None:
    """13eb guardrail #4 / snapshot={}: when a fetch regression would retire/
    transition more than max_acting_fraction of bindings, the breaker refuses the
    whole pass — zero mutations, zero retirements, no counter advance."""
    bindings = {f"loc-{i}": f"REB-{i}" for i in range(10)}
    bs = _store(tmp_path, bindings)
    reader = _archived_reader({lid: _archived_local(lid) for lid in bindings})
    # Pre-seed every counter to GRACE-1 (two 404s): the NEXT empty-window pass
    # would cross grace and retire ALL ten at once — exactly the mass-retire a
    # fetch/JQL regression causes. The breaker must refuse it before any retire.
    for key in bindings.values():
        bs.note_absent(key)
        bs.note_absent(key)

    def probe_404(_client, _key):
        return _ob._DELETED

    result = compute(
        bs,
        {},  # empty window — every bound key now crosses grace → mass-retire
        active_local_ids=set(),
        client=_CLIENT,
        local_reader=reader,
        max_acting_fraction=0.10,  # 10/10 acting ≫ 10% → refuse
        probe_get=probe_404,
    )
    assert result.refused is True
    assert result.mutations == []
    assert result.retired == []
    for key in bindings.values():
        assert not bs.is_retired(key), "no binding may retire on a refused pass"
    # Counters must not advance on a refused pass (stay at the pre-seeded GRACE-1).
    for lid in bindings:
        assert int(bs.all_bindings()[lid].get("absent_404_count", 0)) == 2


def test_census_records_acting_and_breaker(tmp_path: Path) -> None:
    """Every pass emits a decision census (counts + acting_pct + breaker verdict)."""
    bs = _store(tmp_path, {"loc-A": "REB-1", "loc-B": "REB-2"})
    curr = {"REB-1": {"status": "To Do"}, "REB-2": {"status": "Done"}}
    reader = _archived_reader(
        {"loc-A": _archived_local("loc-A"), "loc-B": _archived_local("loc-B")}
    )
    result = compute(
        bs,
        curr,
        active_local_ids=set(),
        client=None,
        local_reader=reader,
        max_acting_fraction=1.0,
    )
    assert result.census["counts"]["terminal_transition"] == 1
    assert result.census["counts"]["noop"] == 1
    assert result.census["acting_count"] == 1
    assert "breaker" in result.census


# ── dry-run — plan without side effects ──────────────────────────────────────


def test_dry_run_predicts_retire_without_mutating_store(tmp_path: Path) -> None:
    """persist=False computes the plan (predicted retirements) but performs NO
    binding-store side effect — no counter advance, no retirement."""
    bs = _store(tmp_path, {"loc-C": "REB-530"})
    reader = _archived_reader({"loc-C": _archived_local("loc-C")})
    # Pre-seed the counter to grace-1 so this single pass WOULD retire live.
    bs.note_absent("REB-530")
    bs.note_absent("REB-530")

    def probe_404(_client, _key):
        return _ob._DELETED

    result = compute(
        bs,
        {},
        active_local_ids=set(),
        client=_CLIENT,
        local_reader=reader,
        max_acting_fraction=1.0,
        probe_get=probe_404,
        persist=False,
    )
    assert "REB-530" in result.retired, "dry-run predicts the retirement"
    assert not bs.is_retired("REB-530"), "dry-run must NOT actually retire the binding"


def test_no_client_absent_in_window_defers(tmp_path: Path) -> None:
    """No client → an off-window key cannot be probed; it stays ABSENT_IN_WINDOW
    and is deferred (never retired, never transitioned)."""
    bs = _store(tmp_path, {"loc-X": "REB-99"})
    reader = _archived_reader({"loc-X": _archived_local("loc-X")})
    result = compute(
        bs,
        {},
        active_local_ids=set(),
        client=None,
        local_reader=reader,
        max_acting_fraction=1.0,
    )
    assert result.mutations == []
    assert not bs.is_retired("REB-99")


# ── class B — adopt an unbound Jira-native issue ─────────────────────────────


def _empty_store(tmp_path: Path) -> BindingStore:
    return _store(tmp_path, {})


def test_class_b_unbound_present_adopts(tmp_path: Path) -> None:
    """DD1(5854): a present Jira key with NO binding is ADOPTED — an (inbound,
    create) mutation whose payload carries the raw fields for the create AND for
    the baseline seed. Level-triggered: this fires regardless of prev_snapshot
    (the exact cell today's suite never builds — key in BOTH snapshots, unbound)."""
    bs = _empty_store(tmp_path)
    fields = {"summary": "native issue", "status": "To Do", "priority": {"name": "High"}}
    result = compute(
        bs,
        {"REB-532": fields},
        active_local_ids=set(),
        client=None,
        local_reader=_archived_reader({}),
        max_acting_fraction=1.0,
    )
    assert result.adopted == ["REB-532"]
    assert len(result.mutations) == 1
    m = result.mutations[0]
    assert m.direction.value == "inbound"
    assert m.action.value == "create"
    assert m.target == "REB-532"
    assert m.provenance["drift_class"] == "B"
    # The raw snapshot entry rides the payload both as create fields and as the
    # baseline seed source (echo suppression).
    assert m.payload["fields"] == fields
    assert m.payload["jira_fields"] == fields


def test_class_b_retired_key_skips_adopt(tmp_path: Path) -> None:
    """DD2(5854) / ADR 0027 §4a: a RETIRED key (its binding was GC'd by class C)
    must NOT be re-adopted — that is the delete/re-adopt loop the two heals must
    not create together. Classifier routes it to SKIP_RETIRED."""
    bs = _store(tmp_path, {"loc-R": "REB-R"})
    # Retire REB-R via GRACE consecutive 404s (class-C path), leaving it unbound
    # AND in the retired set.
    for _ in range(3):
        bs.note_absent("REB-R")
    assert bs.is_retired("REB-R")
    assert bs.get_local_id("REB-R") is None

    result = compute(
        bs,
        {"REB-R": {"summary": "resurrected?", "status": "To Do"}},
        active_local_ids=set(),
        client=None,
        local_reader=_archived_reader({}),
        max_acting_fraction=1.0,
    )
    assert result.adopted == [], "a retired key must never be re-adopted"
    assert result.mutations == []


def test_class_b_labeled_key_not_double_bound(tmp_path: Path) -> None:
    """DD2(5854) / L10: an unbound key that already carries a rebar-id label is
    identity-bound; adopting would double-create the phantom jira-dig-NNNN and can
    trip the L11 quarantine. The unbound arm stands down."""
    bs = _empty_store(tmp_path)
    fields = {"summary": "marked", "status": "To Do", "labels": ["rebar-id:jira-dig-5029"]}
    result = compute(
        bs,
        {"REB-777": fields},
        active_local_ids=set(),
        client=None,
        local_reader=_archived_reader({}),
        max_acting_fraction=1.0,
    )
    assert result.adopted == []
    assert result.mutations == []


def test_class_b_mass_adopt_tripped_by_breaker(tmp_path: Path) -> None:
    """The breaker is a mass-ADOPT guard too: against a real binding population, a
    fetch returning a flood of unbound issues (ADOPT is acting) is refused before
    any create. (Cold-start with zero bindings is instead gated by MODE_CAPS in the
    apply path — the fraction breaker needs a denominator.)"""
    # Five confirmed bindings (all active → skipped by the bound arm) give the
    # breaker a denominator of 5; twenty unbound keys would adopt at 400% ≫ 10%.
    bs = _store(tmp_path, {f"loc-b{i}": f"REB-b{i}" for i in range(5)})
    curr = {f"REB-n{i}": {"summary": f"n{i}", "status": "To Do"} for i in range(20)}
    result = compute(
        bs,
        curr,
        active_local_ids={f"loc-b{i}" for i in range(5)},
        client=None,
        local_reader=_archived_reader({}),
        max_acting_fraction=0.10,
    )
    assert result.refused is True
    assert result.mutations == []
    assert result.adopted == []


def test_class_b_bound_key_not_readopted(tmp_path: Path) -> None:
    """A key that IS bound is owned by the field differ — the unbound arm skips it."""
    bs = _store(tmp_path, {"loc-1": "REB-1"})
    result = compute(
        bs,
        {"REB-1": {"summary": "bound", "status": "To Do"}},
        active_local_ids={"loc-1"},  # active + bound → field differ owns it
        client=None,
        local_reader=_archived_reader({}),
        max_acting_fraction=1.0,
    )
    assert result.adopted == []
