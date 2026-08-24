"""Held-out behavioral oracle for RP-03 S2 T1 — immutable observations + pure ticket plans.

This oracle pins the DETERMINISTIC VALUE-CONSTRUCTION contract of the new shadow
planning layer (lifecycle intents, inter-ticket dependencies, caps ordering, and
preview rendering are OUT OF SCOPE for T1 and owned by later S2 tasks). Three modules
and one refactor are under test (the ticket's file impact):

``rebar_reconciler.observation``
    ``ObservationVersion(pass_id: str, fingerprint: str)`` — a frozen, hashable identity.
    ``Observation`` — a frozen, provider-neutral snapshot of one reconcile pass's inputs
    (version, local_snapshot, remote_snapshot, binding_view, mode, selection, limits,
    payload); every Mapping field is deep-frozen and equality is structural.
    ``build_observation(*, pass_id, local_snapshot, remote_snapshot, binding_view, mode,
    selection, limits, payload=None) -> Observation`` — pure; the version ``fingerprint``
    is ``content_hash`` over the SUBSTANTIVE inputs (everything except ``pass_id``), so
    two passes over identical data share a fingerprint while ``pass_id`` still
    distinguishes their version identity.

``rebar_reconciler.ticket_plan``
    ``PlanDisposition(str, Enum)`` — ``mutate`` / ``defer`` / ``noop``. T1 only ever emits
    ``mutate``; ``defer`` and ``noop`` are the reserved forward-compat surface later S2
    tasks (lifecycle/scope deferral) extend.
    ``TicketPlan`` — a frozen per-ticket plan (identity, mutations, diagnostics,
    disposition, observation_version, payload). ``__eq__`` covers every field; ``__hash__``
    covers the hashable subset (identity, mutations, diagnostics, disposition,
    observation_version).

``rebar_reconciler.ticket_planner``
    ``plan_pass(*, pass_id, local_snapshot, remote_snapshot, binding_view, mode, selection,
    limits, mutations, diagnostics_by_target=None, plan_payload_by_target=None,
    observation_payload=None) -> tuple[Observation, tuple[TicketPlan, ...]]`` — a PURE
    function that groups the already-computed ``Mutation`` values by ``Mutation.target``
    into per-ticket plans, performing ZERO I/O.

``rebar_reconciler.run_differs``
    After it accumulates ``ctx.mutations`` (legacy, still authoritative), it attaches a
    deterministic shadow ``ctx.observation`` and ``ctx.ticket_plans`` derived purely from
    those mutations and the pass inputs — without disturbing ``ctx.mutations``.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
RECON_DIR = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler"

if "rebar_reconciler" not in sys.modules:  # pragma: no cover - import bootstrap
    _pkg = types.ModuleType("rebar_reconciler")
    _pkg.__path__ = [str(RECON_DIR)]
    sys.modules["rebar_reconciler"] = _pkg


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, RECON_DIR / filename)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def mutation_mod():
    return _load("mutation_ticket_plan_test", "mutation.py")


@pytest.fixture(scope="module")
def observation_mod():
    return _load("observation_ticket_plan_test", "observation.py")


@pytest.fixture(scope="module")
def ticket_plan_mod():
    return _load("ticket_plan_ticket_plan_test", "ticket_plan.py")


@pytest.fixture(scope="module")
def planner_mod():
    return _load("ticket_planner_ticket_plan_test", "ticket_planner.py")


# ── Fixtures: frozen, provider-neutral pass inputs ──────────────────────────────


def _mutations(mutation_mod):
    """A small, deterministic, deliberately UNSORTED mixed-direction mutation set
    spanning three ticket targets (one target carries two mutations)."""
    d = mutation_mod.MutationDirection
    a = mutation_mod.MutationAction
    return [
        mutation_mod.Mutation(
            direction=d.outbound,
            action=a.update,
            target="REB-200",
            payload={"summary": "two"},
            provenance={"src": "outbound"},
        ),
        mutation_mod.Mutation(
            direction=d.outbound,
            action=a.create,
            target="local-9",
            payload={"summary": "one"},
            provenance={"src": "outbound"},
        ),
        mutation_mod.Mutation(
            direction=d.inbound,
            action=a.update,
            target="REB-200",
            payload={"labels": ["x"]},
            provenance={"src": "inbound"},
        ),
        mutation_mod.Mutation(
            direction=d.inbound,
            action=a.clean_label,
            target="REB-300",
            payload={"labels": []},
            provenance={"src": "inbound"},
        ),
    ]


def _pass_inputs():
    """The substantive, frozen provider-neutral inputs of one reconcile pass."""
    return dict(
        local_snapshot={"local-9": {"summary": "one"}, "REB-200": {"summary": "two"}},
        remote_snapshot={"REB-200": {"summary": "old"}, "REB-300": {"labels": ["x"]}},
        binding_view={"REB-200": "local-2"},
        mode="live",
        selection={"kind": "all", "ids": []},
        limits={"max_changes": 100},
    )


def _plan_pass(planner_mod, mutation_mod, **overrides):
    kwargs = dict(pass_id="pass-alpha", mutations=_mutations(mutation_mod), **_pass_inputs())
    kwargs.update(overrides)
    return planner_mod.plan_pass(**kwargs)


# ════════════════════════════════════════════════════════════════════════════════
# HAPPY PATH — the minimal specification of correct value construction.
# ════════════════════════════════════════════════════════════════════════════════


def test_plan_pass_groups_mutations_by_target(planner_mod, mutation_mod):
    """plan_pass returns one immutable TicketPlan per Mutation.target, each carrying
    exactly that target's mutations, disposition ``mutate``, and the observation version."""
    observation, plans = _plan_pass(planner_mod, mutation_mod)

    by_identity = {p.identity: p for p in plans}
    assert set(by_identity) == {"local-9", "REB-200", "REB-300"}

    # REB-200 owns both of its mutations; the single-mutation targets own one each.
    assert {m.target for m in by_identity["REB-200"].mutations} == {"REB-200"}
    assert len(by_identity["REB-200"].mutations) == 2
    assert len(by_identity["local-9"].mutations) == 1
    assert len(by_identity["REB-300"].mutations) == 1

    # Every T1 plan is a mutate plan tied to the pass observation version.
    for plan in plans:
        assert plan.disposition.value == "mutate"
        assert plan.observation_version == observation.version


def test_plan_pass_partitions_all_mutations_without_loss(planner_mod, mutation_mod):
    """Every input mutation lands in exactly one plan; none is dropped or duplicated."""
    mutations = _mutations(mutation_mod)
    _observation, plans = _plan_pass(planner_mod, mutation_mod, mutations=mutations)

    gathered = [m for plan in plans for m in plan.mutations]

    # Mutation identity is (direction, action, target); compare on that triple.
    def key(m):
        return (m.direction.value, m.action.value, m.target)

    assert sorted(map(key, gathered)) == sorted(map(key, mutations))


def test_plan_pass_is_deterministic(planner_mod, mutation_mod):
    """Identical frozen inputs produce EQUAL immutable observations and ticket plans
    (AC1) — including plan order."""
    obs_a, plans_a = _plan_pass(planner_mod, mutation_mod)
    obs_b, plans_b = _plan_pass(planner_mod, mutation_mod)

    assert obs_a == obs_b
    assert obs_a.version == obs_b.version
    assert plans_a == plans_b
    assert [p.identity for p in plans_a] == [p.identity for p in plans_b]


def test_plan_pass_emits_deterministic_sorted_order(planner_mod, mutation_mod):
    """The EMISSION order is explicitly sorted, not merely convergent (AC1): targets are
    emitted in sorted order and, within a target, mutations follow the total canonical
    ``_mutation_sort_key`` — regardless of the differ's (deliberately unsorted) input
    order. A convergence-twin comparison of two identical runs cannot see this."""
    _observation, plans = _plan_pass(planner_mod, mutation_mod)

    # Targets emitted in sorted order (uppercase 'REB-*' before lowercase 'local-*').
    identities = [p.identity for p in plans]
    assert identities == sorted(identities) == ["REB-200", "REB-300", "local-9"]

    # REB-200's two mutations were supplied outbound/update THEN inbound/update; the
    # planner re-orders them by (direction.value, action.value, ...) → inbound first.
    by_id = {p.identity: p for p in plans}
    reb200_order = [(m.direction.value, m.action.value) for m in by_id["REB-200"].mutations]
    assert reb200_order == [("inbound", "update"), ("outbound", "update")]


def test_within_plan_tiebreak_is_payload_stable(planner_mod, mutation_mod):
    """Two mutations on ONE target sharing ``(direction, action)`` — distinguished only by
    ``payload`` (excluded from Mutation identity) — are ordered by the canonical tie-break,
    deterministically and independent of input order (AC1)."""
    d = mutation_mod.MutationDirection
    a = mutation_mod.MutationAction

    def _pair(order):
        muts = [
            mutation_mod.Mutation(
                direction=d.outbound,
                action=a.update,
                target="REB-9",
                payload={"summary": "aaa"},
                provenance={"src": "outbound"},
            ),
            mutation_mod.Mutation(
                direction=d.outbound,
                action=a.update,
                target="REB-9",
                payload={"summary": "zzz"},
                provenance={"src": "outbound"},
            ),
        ]
        muts = muts if order == "fwd" else list(reversed(muts))
        _obs, plans = _plan_pass(planner_mod, mutation_mod, mutations=muts)
        return [dict(m.payload)["summary"] for m in plans[0].mutations]

    # Both input orders converge on the same canonical output order.
    assert _pair("fwd") == _pair("rev") == ["aaa", "zzz"]


def test_existing_mutation_serialization_is_byte_identical(planner_mod, mutation_mod):
    """Routing mutations through the planner preserves the existing canonical manifest
    bytes and hash exactly (AC3): serializing the union of every plan's mutations equals
    serializing the original input list."""
    mutations = _mutations(mutation_mod)
    _observation, plans = _plan_pass(planner_mod, mutation_mod, mutations=mutations)

    gathered = [m for plan in plans for m in plan.mutations]
    json_before, hash_before = mutation_mod.serialize_manifest(mutations)
    json_after, hash_after = mutation_mod.serialize_manifest(gathered)
    assert json_after == json_before
    assert hash_after == hash_before


# ════════════════════════════════════════════════════════════════════════════════
# HELD OUT — edge/contract/E2E cases withheld from the implementer.
# ════════════════════════════════════════════════════════════════════════════════


def test_observation_version_fingerprint_tracks_data_identity(planner_mod, mutation_mod):
    """The version fingerprint is a pure function of the SUBSTANTIVE inputs: identical
    data → identical fingerprint even under a different pass_id; any changed datum →
    a different fingerprint (AC1, observation/version identity)."""
    obs_1, _ = _plan_pass(planner_mod, mutation_mod, pass_id="pass-alpha")
    obs_2, _ = _plan_pass(planner_mod, mutation_mod, pass_id="pass-beta")

    # Same data, different pass_id: same fingerprint, distinct version identity.
    assert obs_1.version.fingerprint == obs_2.version.fingerprint
    assert obs_1.version.pass_id != obs_2.version.pass_id
    assert obs_1.version != obs_2.version

    # A changed substantive input moves the fingerprint.
    obs_3, _ = _plan_pass(
        planner_mod, mutation_mod, remote_snapshot={"REB-200": {"summary": "CHANGED"}}
    )
    assert obs_3.version.fingerprint != obs_1.version.fingerprint


def test_observation_and_plans_are_immutable(planner_mod, mutation_mod, observation_mod):
    """Observations and plans are frozen values; attribute assignment raises and the
    Mapping fields cannot be mutated (AC1, 'immutable')."""
    observation, plans = _plan_pass(planner_mod, mutation_mod)
    plan = plans[0]

    with pytest.raises((AttributeError, TypeError)):
        observation.mode = "check"  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        plan.identity = "other"  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        plan.disposition = None  # type: ignore[misc]

    # The snapshot Mapping is read-only, not a live alias the caller can mutate.
    with pytest.raises(TypeError):
        observation.local_snapshot["local-9"] = {"summary": "mutated"}  # type: ignore[index]

    # mutations are an immutable tuple, not a list.
    assert isinstance(plan.mutations, tuple)


def test_input_mapping_mutation_does_not_leak_into_observation(planner_mod, mutation_mod):
    """The planner defensively copies its inputs: mutating a caller's dict AFTER the call
    does not change the already-built (equal-to-a-fresh-build) observation."""
    inputs = _pass_inputs()
    local = dict(inputs["local_snapshot"])
    inputs["local_snapshot"] = local
    obs_before, _ = planner_mod.plan_pass(
        pass_id="pass-alpha", mutations=_mutations(mutation_mod), **inputs
    )
    fingerprint = obs_before.version.fingerprint

    local["injected-after"] = {"summary": "late"}  # mutate the caller's dict post-call
    obs_fresh, _ = _plan_pass(planner_mod, mutation_mod)
    assert obs_before.version.fingerprint == fingerprint == obs_fresh.version.fingerprint


def test_diagnostics_are_carried_through_per_target(planner_mod, mutation_mod):
    """A TicketPlan carries the per-target diagnostics it was given, as an immutable
    tuple; targets without diagnostics carry an empty tuple (AC2)."""
    _obs, plans = _plan_pass(
        planner_mod,
        mutation_mod,
        diagnostics_by_target={"REB-200": ["stale prev value", "field drift"]},
    )
    by_identity = {p.identity: p for p in plans}
    assert by_identity["REB-200"].diagnostics == ("stale prev value", "field drift")
    assert by_identity["local-9"].diagnostics == ()
    assert isinstance(by_identity["REB-300"].diagnostics, tuple)


def test_provider_payload_extension_stays_open(planner_mod, mutation_mod, ticket_plan_mod):
    """Provider-specific payload extends both the observation and per-plan payload without
    weakening or altering the provider-neutral core fields (AC5)."""
    baseline_obs, baseline_plans = _plan_pass(planner_mod, mutation_mod)
    baseline_by_id = {p.identity: p for p in baseline_plans}

    obs, plans = _plan_pass(
        planner_mod,
        mutation_mod,
        observation_payload={"provider": "jira-dc", "api_version": 2},
        plan_payload_by_target={"REB-200": {"jira_key": "REB-200", "raw_etag": "abc"}},
    )
    by_id = {p.identity: p for p in plans}

    # The extension is visible on the payload channel...
    assert obs.payload["provider"] == "jira-dc"
    assert by_id["REB-200"].payload["raw_etag"] == "abc"

    # ...but the provider-neutral CORE is untouched: same version fingerprint, same
    # grouping, same mutations, same disposition as the payload-free baseline.
    assert obs.version.fingerprint == baseline_obs.version.fingerprint
    assert set(by_id) == set(baseline_by_id)
    assert by_id["REB-200"].mutations == baseline_by_id["REB-200"].mutations
    assert by_id["REB-200"].disposition == baseline_by_id["REB-200"].disposition
    # A target given no extension payload keeps an empty payload.
    assert dict(by_id["local-9"].payload) == {}


def test_planner_performs_zero_io(planner_mod, mutation_mod):
    """Plan construction performs no repository saves, ticket writes, Jira calls,
    subprocess launches, or ambient clock reads (AC4). Poison every such seam for the
    DURATION of the plan_pass call and assert it still produces its deterministic result.

    The poison is scoped with a context manager so it wraps only the code under test —
    poisoning ``time``/``Path``/``subprocess`` globally for the whole test would also
    trip pytest's own capture/teardown machinery, not the planner."""
    import subprocess
    import time
    from unittest import mock

    def _boom(*_a, **_k):
        raise AssertionError("planner touched a forbidden I/O / clock seam")

    with (
        mock.patch.object(time, "time", _boom),
        mock.patch.object(time, "time_ns", _boom),
        mock.patch.object(time, "monotonic", _boom),
        mock.patch.object(subprocess, "run", _boom),
        mock.patch.object(subprocess, "Popen", _boom),
        mock.patch.object(Path, "write_text", _boom),
        mock.patch.object(Path, "read_text", _boom),
        mock.patch.object(Path, "open", _boom),
    ):
        observation, plans = _plan_pass(planner_mod, mutation_mod)

    assert observation.version.pass_id == "pass-alpha"
    assert {p.identity for p in plans} == {"local-9", "REB-200", "REB-300"}


# ── E2E: the run_differs shadow attachment ──────────────────────────────────────

_STUBBED_PHASES = (
    "_run_differs_report_schema_drift",
    "_run_differs_inbound",
    "_run_differs_binding_walk",
)


def _drive_run_differs(run_differs_mod, monkeypatch, *, seed_mutations):
    """Run the real run_differs with every mutation-producing phase stubbed to yield a
    known set, so the assertions see exactly the shadow plan the planner attached.

    ``ctx.mutations`` is finalized from ``ctx.differ.compute_mutations(...)`` after the
    legacy local-state suppression, so we seed there and neutralize the suppression
    pass; the named phase helpers then run as no-ops, leaving the seed intact.
    """
    seed = list(seed_mutations)
    mode_mod = _load("mode_shadow_e2e", "mode.py")
    monkeypatch.setattr(
        run_differs_mod,
        "_run_differs_invariants",
        lambda ctx: (False, set(), []),
    )
    # The suppression drops "local-state" differ arms; keep the seed intact.
    from rebar_reconciler import reconcile_helpers as _rh_mod

    monkeypatch.setattr(_rh_mod, "drop_snapshot_differ_local_state_emissions", lambda m: list(m))
    for name in _STUBBED_PHASES:
        monkeypatch.setattr(run_differs_mod, name, lambda *a, **k: None)
    monkeypatch.setattr(run_differs_mod, "_load_reconcile_backend", lambda: None)
    monkeypatch.setattr(run_differs_mod, "_run_differs_outbound", lambda *a, **k: ([], {}, None))

    ctx = types.SimpleNamespace(
        pass_id="pass-e2e",
        target_mode=mode_mod.Mode.LIVE,
        filter_local_ids=None,
        selection_kind="all",
        selection_ids=None,
        max_changes=100,
        differ=types.SimpleNamespace(compute_mutations=lambda *a, **k: list(seed)),
        invariants_mod=None,
        binding_store=None,
        local_tickets=[],
        prev_snapshot={},
        curr_snapshot={},
        mutations=[],
    )
    run_differs_mod.run_differs(ctx)
    return ctx


def test_run_differs_attaches_deterministic_shadow_plan(monkeypatch, mutation_mod):
    """run_differs attaches a shadow ``observation`` + ``ticket_plans`` derived purely from
    the accumulated mutations, WITHOUT disturbing the authoritative ``ctx.mutations``, and
    does so deterministically across two identical passes."""
    run_differs_mod = _load("run_differs_shadow_plan_e2e", "run_differs.py")
    seed = _mutations(mutation_mod)

    ctx1 = _drive_run_differs(run_differs_mod, monkeypatch, seed_mutations=seed)

    # The legacy mutation list is untouched (still authoritative).
    def key(m):
        return (m.direction.value, m.action.value, m.target)

    assert sorted(map(key, ctx1.mutations)) == sorted(map(key, seed))

    # A shadow plan is attached and groups the same mutations by target. run_differs
    # loads ``observation``/``ticket_plan`` by path through its own loader, so the
    # attached objects' classes are distinct object identities from any test-side
    # re-load — assert the type by name/module (isolation-safe) and lean on the
    # version-linkage + determinism assertions below for the real contract.
    assert type(ctx1.observation).__name__ == "Observation"
    assert type(ctx1.observation).__module__.endswith("observation")
    # ``target_mode`` was a real ``Mode(str, Enum)`` member: the observation records the
    # canonical value ``"live"``, NOT the ``str(Mode.LIVE)`` repr ``"Mode.LIVE"``.
    assert ctx1.observation.mode == "live"
    assert isinstance(ctx1.ticket_plans, tuple)
    assert {p.identity for p in ctx1.ticket_plans} == {"local-9", "REB-200", "REB-300"}
    for plan in ctx1.ticket_plans:
        assert type(plan).__name__ == "TicketPlan"
        assert plan.observation_version == ctx1.observation.version

    # Determinism across an identical second pass.
    ctx2 = _drive_run_differs(run_differs_mod, monkeypatch, seed_mutations=_mutations(mutation_mod))
    assert ctx2.observation.version.fingerprint == ctx1.observation.version.fingerprint
    assert ctx2.ticket_plans == ctx1.ticket_plans


def test_run_differs_shadow_plan_tolerates_legacy_dict_mutations(monkeypatch, mutation_mod):
    """``ctx.mutations`` may MIX typed ``Mutation`` instances with legacy plain-dict
    mutations (documented dual shape — see ``_run_differs_report_schema_drift``). The
    shadow attach must not raise on the dicts, must leave ``ctx.mutations`` (dict
    included) authoritative and unchanged, and must plan only the typed items."""
    run_differs_mod = _load("run_differs_shadow_legacy_dict", "run_differs.py")
    legacy = {
        "action": "repair_property",
        "key": "JIRA-9",
        "follow_on": {"kind": "schema_drift_signal", "target": "labels"},
    }
    typed = _mutations(mutation_mod)
    mixed = [*typed, legacy]

    ctx = _drive_run_differs(run_differs_mod, monkeypatch, seed_mutations=mixed)

    # The legacy dict survives in the authoritative list (nothing dropped or converted).
    assert legacy in list(ctx.mutations)
    # The typed shadow plan groups ONLY the typed Mutations — the dict is excluded.
    assert {p.identity for p in ctx.ticket_plans} == {"local-9", "REB-200", "REB-300"}
    for plan in ctx.ticket_plans:
        for m in plan.mutations:
            assert hasattr(m, "target") and not isinstance(m, dict)


# ════════════════════════════════════════════════════════════════════════════════
# RP-03 S2 T2 — lifecycle intents, inter-ticket dependencies, pre-effect exclusion.
# ════════════════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def operation_outcome_mod():
    return _load("operation_outcome_ticket_plan_test", "operation_outcome.py")


def _mk(mutation_mod, direction, action, target, payload=None, provenance=None):
    d = mutation_mod.MutationDirection
    a = mutation_mod.MutationAction
    return mutation_mod.Mutation(
        direction=getattr(d, direction),
        action=getattr(a, action),
        target=target,
        payload=payload or {},
        provenance=provenance or {"src": direction},
    )


# ── T2 HAPPY PATH — minimal executable spec of the new surface. ──────────────────


def test_ticket_plan_t1_construction_still_defaults(ticket_plan_mod, observation_mod):
    """A TicketPlan built the T1 way (no intents/dependencies/defer_reason kwargs) still
    constructs, and the new fields default to empty tuple / empty tuple / None."""
    version = observation_mod.ObservationVersion(pass_id="p", fingerprint="f")
    plan = ticket_plan_mod.TicketPlan(
        identity="REB-1",
        mutations=(),
        diagnostics=(),
        disposition=ticket_plan_mod.PlanDisposition.mutate,
        observation_version=version,
        payload={},
    )
    assert plan.intents == ()
    assert plan.dependencies == ()
    assert plan.defer_reason is None


def test_lifecycle_intent_kinds_derived_from_actions(planner_mod, mutation_mod, ticket_plan_mod):
    """Each mutating action maps to a provider-neutral lifecycle intent kind on a mutate
    plan: create→bind, update→confirm, delete→retire, repair_property→baseline,
    clean_label→comment_identity."""
    muts = [
        _mk(mutation_mod, "outbound", "create", "REB-c"),
        _mk(mutation_mod, "outbound", "update", "REB-u"),
        _mk(mutation_mod, "outbound", "delete", "REB-d"),
        _mk(mutation_mod, "inbound", "repair_property", "REB-r"),
        _mk(mutation_mod, "inbound", "clean_label", "REB-l"),
    ]
    _obs, plans = _plan_pass(planner_mod, mutation_mod, mutations=muts)
    kinds = {p.identity: tuple(i.kind.value for i in p.intents) for p in plans}
    assert kinds["REB-c"] == ("bind",)
    assert kinds["REB-u"] == ("confirm",)
    assert kinds["REB-d"] == ("retire",)
    assert kinds["REB-r"] == ("baseline",)
    assert kinds["REB-l"] == ("comment_identity",)


def test_dependency_edges_recorded_when_prereq_created_in_pass(planner_mod, mutation_mod):
    """A plan whose mutation declares ``requires_create=[X]`` records X as an explicit
    dependency; when X is created in the same pass the dependent plan stays ``mutate``."""
    muts = [
        _mk(mutation_mod, "outbound", "create", "REB-new"),
        _mk(
            mutation_mod, "outbound", "update", "REB-link", payload={"requires_create": ["REB-new"]}
        ),
    ]
    _obs, plans = _plan_pass(
        planner_mod,
        mutation_mod,
        mutations=muts,
        local_snapshot={},
        remote_snapshot={},
        binding_view={},
    )
    by = {p.identity: p for p in plans}
    assert by["REB-link"].dependencies == ("REB-new",)
    assert by["REB-link"].disposition.value == "mutate"
    assert by["REB-new"].dependencies == ()


def test_selection_ids_excludes_out_of_scope_target(planner_mod, mutation_mod):
    """With ``selection={"kind": "ids", "ids": [...]}`` a target outside the id set is a
    ``defer`` plan tagged ``scope_deferred`` — a pre-effect exclusion."""
    muts = [
        _mk(mutation_mod, "outbound", "update", "REB-in"),
        _mk(mutation_mod, "outbound", "update", "REB-out"),
    ]
    _obs, plans = _plan_pass(
        planner_mod,
        mutation_mod,
        mutations=muts,
        selection={"kind": "ids", "ids": ["REB-in"]},
    )
    by = {p.identity: p for p in plans}
    assert by["REB-in"].disposition.value == "mutate"
    assert by["REB-out"].disposition.value == "defer"
    assert by["REB-out"].defer_reason.value == "scope_deferred"


# ── T2 HELD OUT — edge/contract/E2E cases withheld from the implementer. ─────────
# HELDOUT-START


def test_defer_reason_aligns_with_operation_outcome_disposition(
    ticket_plan_mod, operation_outcome_mod
):
    """The plan-layer ``DeferReason`` vocabulary is drawn from the apply-layer
    ``operation_outcome.Disposition`` names: every DeferReason name+value exists there."""
    disp = operation_outcome_mod.Disposition
    for member in ticket_plan_mod.DeferReason:
        assert member.name in disp.__members__
        assert disp[member.name].value == member.value
    assert {m.value for m in ticket_plan_mod.DeferReason} == {
        "dependency_deferred",
        "scope_deferred",
        "safety_aborted",
        "skipped",
    }


def test_intent_version_equals_observation_version_exactly(planner_mod, mutation_mod):
    """Every lifecycle intent's ``version`` IS the enclosing ``Observation.version``
    identity (ObservationVersion), not an independent counter — including an explicit
    ``rekey`` intent requested via a mutation payload ``lifecycle`` override."""
    muts = [
        _mk(mutation_mod, "outbound", "create", "REB-a"),
        _mk(mutation_mod, "outbound", "update", "REB-b", payload={"lifecycle": "rekey"}),
    ]
    obs, plans = _plan_pass(planner_mod, mutation_mod, mutations=muts)
    all_intents = [i for p in plans for i in p.intents]
    assert all_intents  # intents were produced
    for intent in all_intents:
        assert intent.version == obs.version
        assert intent.version is obs.version or intent.version == obs.version
    kinds = {p.identity: tuple(i.kind.value for i in p.intents) for p in plans}
    assert kinds["REB-b"] == ("rekey",)


def test_blocked_create_before_link_defers_with_no_mutation(planner_mod, mutation_mod):
    """A create-before-link prerequisite that is NOT satisfiable this pass blocks the
    dependent plan: disposition ``defer``, reason ``dependency_deferred``, the dependency
    edge is still recorded, and NO out-of-order mutation is emitted (mutations empty)."""
    muts = [
        _mk(
            mutation_mod,
            "outbound",
            "update",
            "REB-link",
            payload={"requires_create": ["REB-absent"]},
        ),
    ]
    _obs, plans = _plan_pass(
        planner_mod,
        mutation_mod,
        mutations=muts,
        local_snapshot={},
        remote_snapshot={},
        binding_view={},
    )
    by = {p.identity: p for p in plans}
    link = by["REB-link"]
    assert link.disposition.value == "defer"
    assert link.defer_reason.value == "dependency_deferred"
    assert link.dependencies == ("REB-absent",)
    assert link.mutations == ()
    assert link.intents == ()


def test_parent_before_child_dependency(planner_mod, mutation_mod):
    """``requires_parent`` is an explicit parent-before-child dependency edge. Satisfied
    when the parent already exists (binding_view/snapshot); blocked otherwise →
    ``dependency_deferred``."""
    child_ok = [
        _mk(
            mutation_mod,
            "outbound",
            "update",
            "REB-child",
            payload={"requires_parent": "REB-parent"},
        ),
    ]
    _obs, plans_ok = _plan_pass(
        planner_mod,
        mutation_mod,
        mutations=child_ok,
        local_snapshot={},
        remote_snapshot={},
        binding_view={"REB-parent": "local-7"},
    )
    ok = {p.identity: p for p in plans_ok}["REB-child"]
    assert ok.dependencies == ("REB-parent",)
    assert ok.disposition.value == "mutate"

    _obs2, plans_blocked = _plan_pass(
        planner_mod,
        mutation_mod,
        mutations=child_ok,
        local_snapshot={},
        remote_snapshot={},
        binding_view={},
    )
    blocked = {p.identity: p for p in plans_blocked}["REB-child"]
    assert blocked.disposition.value == "defer"
    assert blocked.defer_reason.value == "dependency_deferred"
    assert blocked.mutations == ()


def test_mode_cap_excludes_outbound_with_safety_aborted(planner_mod, mutation_mod):
    """A non-live ``mode`` caps outbound effects: an outbound-bearing plan is excluded with
    ``safety_aborted`` before any effect is eligible, while an inbound-only plan survives."""
    muts = [
        _mk(mutation_mod, "outbound", "update", "REB-out"),
        _mk(mutation_mod, "inbound", "clean_label", "REB-inb"),
    ]
    _obs, plans = _plan_pass(planner_mod, mutation_mod, mutations=muts, mode="check")
    by = {p.identity: p for p in plans}
    assert by["REB-out"].disposition.value == "defer"
    assert by["REB-out"].defer_reason.value == "safety_aborted"
    assert by["REB-inb"].disposition.value == "mutate"


def test_global_limit_excludes_overflow_with_safety_aborted(planner_mod, mutation_mod):
    """The global ``limits['max_changes']`` cap excludes overflow plans (deterministically by
    sorted identity) with ``safety_aborted``; each limit is independently variable."""
    muts = [
        _mk(mutation_mod, "outbound", "update", "REB-1"),
        _mk(mutation_mod, "outbound", "update", "REB-2"),
        _mk(mutation_mod, "outbound", "update", "REB-3"),
    ]
    _obs, plans = _plan_pass(
        planner_mod,
        mutation_mod,
        mutations=muts,
        limits={"max_changes": 1},
        binding_view={},
    )
    by = {p.identity: p for p in plans}
    assert by["REB-1"].disposition.value == "mutate"
    assert by["REB-2"].defer_reason.value == "safety_aborted"
    assert by["REB-3"].defer_reason.value == "safety_aborted"

    # Raising the cap to 3 admits all three (limit varied independently).
    _obs2, plans2 = _plan_pass(
        planner_mod,
        mutation_mod,
        mutations=muts,
        limits={"max_changes": 3},
        binding_view={},
    )
    assert all(p.disposition.value == "mutate" for p in plans2)


def test_skipped_data_conditions_are_deterministic(planner_mod, mutation_mod):
    """Tombstone, index-lag, moved-key, impossible-link, and partial-snapshot data
    conditions each produce a deterministic ``noop`` plan tagged ``skipped`` — each family
    separately assertable (the specific cause is recorded in diagnostics)."""
    for cause in (
        "tombstone",
        "index_lag",
        "moved_key",
        "impossible_link",
        "partial_snapshot",
    ):
        muts = [_mk(mutation_mod, "outbound", "update", "REB-x", payload={"skip": cause})]
        _obs, plans = _plan_pass(planner_mod, mutation_mod, mutations=muts, binding_view={})
        plan = plans[0]
        assert plan.disposition.value == "noop"
        assert plan.defer_reason.value == "skipped"
        assert any(cause in d for d in plan.diagnostics)


def test_excluded_plans_carry_no_lifecycle_intents(planner_mod, mutation_mod):
    """A pre-effect-excluded plan (here scope) is not intent-eligible: its ``intents`` are
    empty, while a co-resident mutate plan still derives its intents."""
    muts = [
        _mk(mutation_mod, "outbound", "create", "REB-in"),
        _mk(mutation_mod, "outbound", "create", "REB-out"),
    ]
    _obs, plans = _plan_pass(
        planner_mod,
        mutation_mod,
        mutations=muts,
        selection={"kind": "ids", "ids": ["REB-in"]},
    )
    by = {p.identity: p for p in plans}
    assert by["REB-out"].intents == ()
    assert tuple(i.kind.value for i in by["REB-in"].intents) == ("bind",)


def test_run_differs_shadow_plan_applies_scope_exclusion(monkeypatch, mutation_mod):
    """E2E through the real ``run_differs``: an ``ids`` selection narrows the shadow plan so
    out-of-scope targets are deferred with ``scope_deferred`` while the in-scope target is a
    mutate plan — driven at ``Mode.LIVE`` with the pre-effect exclusion still applied."""
    run_differs_mod = _load("run_differs_shadow_scope_e2e", "run_differs.py")
    seed = _mutations(mutation_mod)

    ctx = _drive_run_differs(run_differs_mod, monkeypatch, seed_mutations=seed)
    # Re-drive with a narrowed selection by mutating ctx knobs the shadow site reads.
    ctx.selection_kind = "ids"
    ctx.selection_ids = ["REB-200"]
    run_differs_mod.run_differs(ctx)

    by = {p.identity: p for p in ctx.ticket_plans}
    assert by["REB-200"].disposition.value == "mutate"
    assert by["local-9"].disposition.value == "defer"
    assert by["local-9"].defer_reason.value == "scope_deferred"
    assert by["REB-300"].defer_reason.value == "scope_deferred"


# HELDOUT-END
