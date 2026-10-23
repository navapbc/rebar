"""Pin deterministic, pure shadow planning from immutable observations. Selection,
dependencies, mode caps, and skipped data decide plans before effects; additive
lifecycle previews preserve canonical mutation bytes and redact raw payloads.
"""

from __future__ import annotations

import importlib
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
RECON_DIR = REPO_ROOT / "src" / "rebar" / "reconciler"


@pytest.fixture(scope="module")
def mutation_mod():
    return importlib.import_module("rebar.reconciler.mutation")


@pytest.fixture(scope="module")
def observation_mod():
    return importlib.import_module("rebar.reconciler.observation")


@pytest.fixture(scope="module")
def ticket_plan_mod():
    return importlib.import_module("rebar.reconciler.ticket_plan")


@pytest.fixture(scope="module")
def planner_mod():
    return importlib.import_module("rebar.reconciler.ticket_planner")


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
    """Emit targets in sorted order and each target's mutations by the canonical total
    sort key, regardless of input order.
    """
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
    """For equal direction/action keys, payload provides a stable canonical tie-break
    independent of input order.
    """
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
    """Observation versions depend on substantive data, not pass_id; changed data
    changes the fingerprint.
    """
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
    """Plan construction reads no repository, provider, subprocess, or clock seam, even
    when those seams are poisoned.
    """
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
    """Run the real differ coordinator with mutation phases stubbed to a known set,
    exposing the attached shadow plan without changing authoritative mutations.
    """
    seed = list(seed_mutations)
    mode_mod = importlib.import_module("rebar.reconciler.mode")
    monkeypatch.setattr(
        run_differs_mod,
        "_run_differs_invariants",
        lambda ctx: (False, set(), []),
    )
    # The suppression drops "local-state" differ arms; keep the seed intact.
    from rebar.reconciler import reconcile_helpers as _rh_mod

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
    run_differs_mod = importlib.import_module("rebar.reconciler.run_differs")
    seed = _mutations(mutation_mod)

    ctx1 = _drive_run_differs(run_differs_mod, monkeypatch, seed_mutations=seed)

    # The legacy mutation list is untouched (still authoritative).
    def key(m):
        return (m.direction.value, m.action.value, m.target)

    assert sorted(map(key, ctx1.mutations)) == sorted(map(key, seed))

    # The by-path loader gives attached plans distinct class identities from
    # test-side imports. Check type by name/module, then assert version linkage
    # and deterministic grouping by target.
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
    """Attach a shadow plan from mixed typed and dict mutations without rewriting
    authoritative ctx.mutations.
    """
    run_differs_mod = importlib.import_module("rebar.reconciler.run_differs")
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
    return importlib.import_module("rebar.reconciler.operation_outcome")


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


def test_selection_only_excludes_out_of_scope_target(planner_mod, mutation_mod):
    """With the canonical ``selection={"kind": "only", "ids": [...]}`` a target outside the id
    set is a ``defer`` plan tagged ``scope_deferred`` — a pre-effect exclusion."""
    muts = [
        _mk(mutation_mod, "outbound", "update", "REB-in"),
        _mk(mutation_mod, "outbound", "update", "REB-out"),
    ]
    _obs, plans = _plan_pass(
        planner_mod,
        mutation_mod,
        mutations=muts,
        selection={"kind": "only", "ids": ["REB-in"]},
    )
    by = {p.identity: p for p in plans}
    assert by["REB-in"].disposition.value == "mutate"
    assert by["REB-out"].disposition.value == "defer"
    assert by["REB-out"].defer_reason.value == "scope_deferred"


def test_selection_except_inverts_scope_semantics(planner_mod, mutation_mod):
    """The canonical ``except`` selection kind INVERTS scoping (narrow_selection_inputs keeps
    ids NOT in the set): a target IN the id set is scope-excluded, one outside it survives."""
    muts = [
        _mk(mutation_mod, "outbound", "update", "REB-in"),
        _mk(mutation_mod, "outbound", "update", "REB-out"),
    ]
    _obs, plans = _plan_pass(
        planner_mod,
        mutation_mod,
        mutations=muts,
        selection={"kind": "except", "ids": ["REB-in"]},
    )
    by = {p.identity: p for p in plans}
    assert by["REB-in"].disposition.value == "defer"
    assert by["REB-in"].defer_reason.value == "scope_deferred"
    assert by["REB-out"].disposition.value == "mutate"


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
    """An unavailable create prerequisite records its dependency and defers the
    dependent with no out-of-order mutation.
    """
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
    """A NO-WRITE mode (``dry-run``, canonical ``MODE_CAPS==0``) aborts
    every planned mutation pre-effect with ``safety_aborted`` — outbound and inbound alike,
    since such a pass performs no writes at all."""
    muts = [
        _mk(mutation_mod, "outbound", "update", "REB-out"),
        _mk(mutation_mod, "inbound", "clean_label", "REB-inb"),
    ]
    for readonly_mode in ("dry-run",):
        _obs, plans = _plan_pass(planner_mod, mutation_mod, mutations=muts, mode=readonly_mode)
        by = {p.identity: p for p in plans}
        assert by["REB-out"].defer_reason.value == "safety_aborted"
        assert by["REB-inb"].defer_reason.value == "safety_aborted"
        assert by["REB-out"].disposition.value == "defer"


def test_write_enabled_modes_are_not_mode_capped(planner_mod, mutation_mod):
    """Bootstrap and live write modes leave eligible outbound plans mutable; only
    no-write modes abort them before effect.
    """
    muts = [_mk(mutation_mod, "outbound", "update", "REB-out")]
    for write_mode in ("bootstrap-strict", "bootstrap-throttle", "live"):
        _obs, plans = _plan_pass(planner_mod, mutation_mod, mutations=muts, mode=write_mode)
        plan = plans[0]
        assert plan.disposition.value == "mutate", write_mode
        assert plan.defer_reason is None, write_mode


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
        selection={"kind": "only", "ids": ["REB-in"]},
    )
    by = {p.identity: p for p in plans}
    assert by["REB-out"].intents == ()
    assert tuple(i.kind.value for i in by["REB-in"].intents) == ("bind",)


def test_run_differs_shadow_plan_applies_scope_exclusion(monkeypatch, mutation_mod):
    """An only-selection defers out-of-scope targets while retaining in-scope mutations
    in the shadow plan.
    """
    run_differs_mod = importlib.import_module("rebar.reconciler.run_differs")
    seed = _mutations(mutation_mod)

    ctx = _drive_run_differs(run_differs_mod, monkeypatch, seed_mutations=seed)
    # Re-drive with a narrowed selection by mutating ctx knobs the shadow site reads.
    ctx.selection_kind = "only"
    ctx.selection_ids = ["REB-200"]
    run_differs_mod.run_differs(ctx)

    by = {p.identity: p for p in ctx.ticket_plans}
    assert by["REB-200"].disposition.value == "mutate"
    assert by["local-9"].disposition.value == "defer"
    assert by["local-9"].defer_reason.value == "scope_deferred"
    assert by["REB-300"].defer_reason.value == "scope_deferred"


# HELDOUT-END


def test_excluded_create_target_does_not_satisfy_dependent(planner_mod, mutation_mod):
    """An excluded create cannot provide an identity; its dependent plan stays
    dependency-deferred.
    """
    muts = [
        # Two independent creates + one dependent; max_changes caps the *third* eligible
        # target (sorted). Use ids so the dependent's provider "REB-new" is the capped one.
        _mk(mutation_mod, "outbound", "create", "AAA-filler"),
        _mk(mutation_mod, "outbound", "create", "REB-new"),
        _mk(
            mutation_mod,
            "outbound",
            "update",
            "REB-link",
            payload={"requires_create": ["REB-new"]},
        ),
    ]
    # Sorted eligible order: AAA-filler, REB-link, REB-new. max_changes=2 caps REB-new.
    _obs, plans = _plan_pass(
        planner_mod,
        mutation_mod,
        mutations=muts,
        local_snapshot={},
        remote_snapshot={},
        binding_view={},
        limits={"max_changes": 2},
    )
    by = {p.identity: p for p in plans}
    assert by["REB-new"].defer_reason.value == "safety_aborted"
    # REB-new won't be created, so its dependent is dependency-deferred (no out-of-order mut).
    assert by["REB-link"].disposition.value == "defer"
    assert by["REB-link"].defer_reason.value == "dependency_deferred"
    assert by["REB-link"].mutations == ()


def test_probe_and_conflict_actions_yield_no_intent(planner_mod, mutation_mod):
    """``probe`` and ``conflict`` actions are not lifecycle bindings: a mutate plan built
    solely from them derives NO lifecycle intents (while its mutations are retained)."""
    muts = [
        _mk(mutation_mod, "outbound", "probe", "REB-p"),
        _mk(mutation_mod, "inbound", "conflict", "REB-k"),
    ]
    _obs, plans = _plan_pass(planner_mod, mutation_mod, mutations=muts, binding_view={})
    by = {p.identity: p for p in plans}
    assert by["REB-p"].disposition.value == "mutate"
    assert by["REB-p"].intents == ()
    assert by["REB-k"].disposition.value == "mutate"
    assert by["REB-k"].intents == ()
    assert len(by["REB-p"].mutations) == 1


def test_unrecognized_skip_value_still_classifies_skipped(planner_mod, mutation_mod):
    """A truthy but unrecognized/mistyped ``payload['skip']`` is not silently ignored: it
    still classifies the plan as ``skipped`` with the raw cause recorded in diagnostics."""
    muts = [_mk(mutation_mod, "outbound", "update", "REB-x", payload={"skip": "tombstoen"})]
    _obs, plans = _plan_pass(planner_mod, mutation_mod, mutations=muts, binding_view={})
    plan = plans[0]
    assert plan.disposition.value == "noop"
    assert plan.defer_reason.value == "skipped"
    assert any("tombstoen" in d for d in plan.diagnostics)


def test_invalid_lifecycle_override_falls_back_to_action_mapping(planner_mod, mutation_mod):
    """An unknown lifecycle override falls back to the action mapping instead of
    becoming an intent kind.
    """
    muts = [
        _mk(mutation_mod, "outbound", "create", "REB-c", payload={"lifecycle": "not_a_kind"}),
    ]
    _obs, plans = _plan_pass(planner_mod, mutation_mod, mutations=muts, binding_view={})
    intents = plans[0].intents
    assert tuple(i.kind.value for i in intents) == ("bind",)


def test_transitive_create_provider_cascade_blocks_dependent(planner_mod, mutation_mod):
    """A dependency-blocked create cannot serve as a provider; deferral cascades to
    downstream plans.
    """
    muts = [
        _mk(
            mutation_mod,
            "outbound",
            "create",
            "REB-mid",
            payload={"requires_create": ["REB-absent"]},
        ),
        _mk(
            mutation_mod,
            "outbound",
            "update",
            "REB-leaf",
            payload={"requires_create": ["REB-mid"]},
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
    assert by["REB-mid"].defer_reason.value == "dependency_deferred"
    assert by["REB-leaf"].defer_reason.value == "dependency_deferred"
    assert by["REB-leaf"].mutations == ()
    # Sanity: when REB-absent DOES exist, the whole chain becomes mutate.
    _obs2, plans2 = _plan_pass(
        planner_mod,
        mutation_mod,
        mutations=muts,
        local_snapshot={},
        remote_snapshot={"REB-absent": {}},
        binding_view={},
    )
    by2 = {p.identity: p for p in plans2}
    assert by2["REB-mid"].disposition.value == "mutate"
    assert by2["REB-leaf"].disposition.value == "mutate"


# ════════════════════════════════════════════════════════════════════════════════
# RP-03 S2 T3 — shadow plans through additive preview output + parity comparison.
# ════════════════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def manifest_renderer_mod():
    return importlib.import_module("rebar.reconciler.manifest_renderer")


@pytest.fixture(scope="module")
def apply_planning_mod():
    return importlib.import_module("rebar.reconciler.apply_planning")


@pytest.fixture(scope="module")
def mode_mod_t3():
    return importlib.import_module("rebar.reconciler.mode")


def _deferred_plans(planner_mod, mutation_mod, *, extra_payload=None):
    """Build a one-target dependency-deferred plan beside its legacy mutation for
    parity and rendering checks.
    """
    payload = {"requires_create": ["REB-missing"]}
    if extra_payload:
        payload.update(extra_payload)
    legacy = [_mk(mutation_mod, "outbound", "update", "REB-link", payload=payload)]
    _obs, plans = _plan_pass(
        planner_mod,
        mutation_mod,
        mutations=legacy,
        local_snapshot={},
        remote_snapshot={},
        binding_view={},
    )
    return legacy, plans


def _emit(apply_planning_mod, mode_mod, mutations, *, ticket_plans=None, route="preview"):
    """Drive the real apply-planning manifest consumer in no-persist DRY_RUN/preview mode and
    return the rendered manifest dict (its no-write RETURN payload)."""
    action, value = apply_planning_mod._emit_mode_manifest(
        mode_mod.Mode.DRY_RUN,
        mode_mod,
        list(mutations),
        [],
        "pass-preview",
        None,
        None,
        False,
        None,
        route,
        None,
        ticket_plans=ticket_plans,
    )
    assert action == "RETURN"
    return value


# ── T3 HAPPY PATH — minimal executable spec of the additive surface. ─────────────


def test_render_lifecycle_intents_is_versioned_section(
    planner_mod, mutation_mod, manifest_renderer_mod
):
    """Render one versioned lifecycle section with observation identity and each plan's
    disposition, dependencies, and intent kinds.
    """
    _obs, plans = _plan_pass(planner_mod, mutation_mod, binding_view={})
    section = manifest_renderer_mod.render_lifecycle_intents(plans)

    # Versioned: a stable integer schema version + the pass observation version identity.
    assert isinstance(section["schema_version"], int)
    ov = plans[0].observation_version
    assert section["observation_version"] == {
        "pass_id": ov.pass_id,
        "fingerprint": ov.fingerprint,
    }
    # One entry per plan, addressed by identity, exposing disposition + intent kinds.
    by_identity = {e["identity"]: e for e in section["plans"]}
    assert by_identity.keys() == {p.identity for p in plans}
    for plan in plans:
        entry = by_identity[plan.identity]
        assert entry["disposition"] == plan.disposition.value
        assert tuple(i["kind"] for i in entry["intents"]) == tuple(
            i.kind.value for i in plan.intents
        )


def test_render_lifecycle_intents_empty_when_no_plans(manifest_renderer_mod):
    """With no shadow plans the section is still well-formed and versioned (empty plan list,
    null observation version) so legacy consumers can always read the key."""
    section = manifest_renderer_mod.render_lifecycle_intents(())
    assert isinstance(section["schema_version"], int)
    assert section["observation_version"] is None
    assert section["plans"] == []


def test_compare_parity_matches_on_happy_corpus(planner_mod, mutation_mod):
    """For a normal corpus every plan mutates and emits exactly the legacy mutations, so parity
    matches with no deltas."""
    legacy = _mutations(mutation_mod)
    _obs, plans = _plan_pass(planner_mod, mutation_mod, mutations=legacy)
    report = planner_mod.compare_parity(legacy, plans)
    assert report.matched is True
    assert report.deltas == ()
    assert report.unexpected == ()


def test_compare_parity_reports_identity_version_and_fields(planner_mod, mutation_mod):
    """A deferred plan diverges from legacy: parity reports a delta that names the ticket
    identity, the observation version, and the differing plan fields."""
    legacy, plans = _deferred_plans(planner_mod, mutation_mod)
    report = planner_mod.compare_parity(legacy, plans)
    assert report.matched is False
    assert report.deltas  # at least one delta
    identities = {d.identity for d in report.deltas}
    assert "REB-link" in identities
    ov = plans[0].observation_version
    for d in report.deltas:
        assert d.observation_version == ov
        assert isinstance(d.fields, tuple) and d.fields  # named differing fields present


def test_emit_manifest_adds_lifecycle_section_without_touching_mutation_array(
    planner_mod, mutation_mod, apply_planning_mod, mode_mod_t3
):
    """Threading shadow plans into the manifest consumer adds the ``lifecycle_intents`` section
    while leaving the existing mutation array (``plan``) byte-for-byte unchanged."""
    import json

    legacy = _mutations(mutation_mod)
    _obs, plans = _plan_pass(planner_mod, mutation_mod, mutations=legacy)

    without = _emit(apply_planning_mod, mode_mod_t3, legacy)
    with_plans = _emit(apply_planning_mod, mode_mod_t3, legacy, ticket_plans=plans)

    assert "lifecycle_intents" not in without
    assert "lifecycle_intents" in with_plans
    assert json.dumps(with_plans["plan"]) == json.dumps(without["plan"])


# ── T3 HELD OUT — the hard guarantees the impl must satisfy blind. ───────────────
# HELDOUT-START


def test_emit_manifest_section_is_purely_additive(
    planner_mod, mutation_mod, apply_planning_mod, mode_mod_t3
):
    """The ONLY difference the shadow section makes to the manifest is the single new
    top-level ``lifecycle_intents`` key: every other key (and its bytes) is identical."""
    legacy = _mutations(mutation_mod)
    _obs, plans = _plan_pass(planner_mod, mutation_mod, mutations=legacy)

    without = _emit(apply_planning_mod, mode_mod_t3, legacy)
    with_plans = _emit(apply_planning_mod, mode_mod_t3, legacy, ticket_plans=plans)

    stripped = dict(with_plans)
    stripped.pop("lifecycle_intents")
    assert stripped == without


def test_serialize_manifest_hash_unperturbed_by_shadow_section(
    planner_mod, mutation_mod, apply_planning_mod, mode_mod_t3
):
    """Adding the preview section leaves canonical mutation bytes and SHA-256
    unchanged.
    """
    legacy = _mutations(mutation_mod)
    golden_text, golden_hash = mutation_mod.serialize_manifest(legacy)

    # The fixed hash catches canonical-byte drift that a runtime recompare of
    # two equally changed serializations would miss.
    GOLDEN_HASH = "e8d2bfad77db0336b2ee67af1b344ea6c7f5266a92205f0a583b294cf6ab61c9"
    assert golden_hash == GOLDEN_HASH

    _obs, plans = _plan_pass(planner_mod, mutation_mod, mutations=legacy)
    _rendered = _emit(apply_planning_mod, mode_mod_t3, legacy, ticket_plans=plans)

    after_text, after_hash = mutation_mod.serialize_manifest(legacy)
    assert (after_text, after_hash) == (golden_text, golden_hash)
    # sha256 hex digest shape — a stable, content-addressed identity.
    assert after_hash == GOLDEN_HASH
    assert len(golden_hash) == 64 and all(c in "0123456789abcdef" for c in golden_hash)


def test_compare_parity_approved_delta_is_matched(planner_mod, mutation_mod):
    """A divergence explicitly named as an approved delta (by ticket identity) is recorded but
    does NOT fail parity; an unnamed one does."""
    legacy, plans = _deferred_plans(planner_mod, mutation_mod)

    unapproved = planner_mod.compare_parity(legacy, plans)
    assert unapproved.matched is False
    assert any(d.identity == "REB-link" for d in unapproved.unexpected)

    approved = planner_mod.compare_parity(
        legacy, plans, approved_identities=frozenset({"REB-link"})
    )
    assert approved.matched is True
    assert approved.deltas  # the delta is still RECORDED
    assert approved.unexpected == ()


def test_parity_delta_fields_are_bounded_and_carry_no_secret(planner_mod, mutation_mod):
    """AC7: a parity delta carries the identity, observation version and the differing plan
    fields ONLY — bounded, structured strings, never the raw (possibly secret) payload."""
    legacy, plans = _deferred_plans(
        planner_mod, mutation_mod, extra_payload={"secret_token": "TOP-SECRET-XYZ"}
    )
    report = planner_mod.compare_parity(legacy, plans)
    assert report.matched is False
    blob = repr(report.deltas)
    assert "TOP-SECRET-XYZ" not in blob
    for d in report.deltas:
        for f in d.fields:
            assert isinstance(f, str)
            assert len(f) <= 120  # bounded, not an unbounded dump
            assert "TOP-SECRET-XYZ" not in f


def test_lifecycle_section_excludes_raw_payload(planner_mod, mutation_mod, manifest_renderer_mod):
    """The additive preview section exposes intent KIND/target/version — not the raw mutation
    payload — so a secret in a mutation payload never leaks into the shadow section."""
    muts = [
        _mk(
            mutation_mod,
            "outbound",
            "create",
            "REB-c",
            payload={"summary": "x", "secret_token": "TOP-SECRET-XYZ"},
        )
    ]
    _obs, plans = _plan_pass(planner_mod, mutation_mod, mutations=muts, binding_view={})
    section = manifest_renderer_mod.render_lifecycle_intents(plans)
    assert "TOP-SECRET-XYZ" not in repr(section)


def test_run_differs_attaches_matching_parity_report(monkeypatch, mutation_mod):
    """E2E: run_differs computes a shadow-vs-legacy parity report from ctx.mutations (legacy)
    vs ctx.ticket_plans (shadow) and attaches it. For a normal corpus it matches."""
    run_differs_mod = importlib.import_module("rebar.reconciler.run_differs")
    seed = _mutations(mutation_mod)
    ctx = _drive_run_differs(run_differs_mod, monkeypatch, seed_mutations=seed)

    assert ctx.plan_parity is not None
    assert ctx.plan_parity.matched is True
    assert ctx.plan_parity.unexpected == ()
    # Parity read the shadow version identity of this very pass.
    for plan in ctx.ticket_plans:
        assert plan.observation_version == ctx.observation.version


def test_run_differs_parity_flags_deferred_divergence(monkeypatch, mutation_mod):
    """A missing create defers the shadow mutation, so parity reports the unexpected
    ticket and observation version delta.
    """
    run_differs_mod = importlib.import_module("rebar.reconciler.run_differs")
    seed = [
        _mk(
            mutation_mod,
            "outbound",
            "update",
            "REB-link",
            payload={"requires_create": ["REB-missing"]},
        )
    ]
    ctx = _drive_run_differs(run_differs_mod, monkeypatch, seed_mutations=seed)

    # Authoritative legacy mutation list is untouched.
    assert len(list(ctx.mutations)) == 1
    assert ctx.plan_parity.matched is False
    flagged = {d.identity for d in ctx.plan_parity.unexpected}
    assert "REB-link" in flagged
    for d in ctx.plan_parity.unexpected:
        assert d.observation_version == ctx.observation.version


def test_parity_and_lifecycle_render_perform_zero_io(
    planner_mod, mutation_mod, manifest_renderer_mod
):
    """AC6: parity comparison and the additive lifecycle render touch no repository/provider/
    subprocess/clock seam. Poison every such seam for the duration and assert they still
    produce their deterministic results."""
    import subprocess
    import time
    from unittest import mock

    legacy = _mutations(mutation_mod)
    _obs, plans = _plan_pass(planner_mod, mutation_mod, mutations=legacy)

    def _boom(*_a, **_k):
        raise AssertionError("parity/preview touched a forbidden I/O / clock seam")

    with (
        mock.patch.object(time, "time", _boom),
        mock.patch.object(time, "time_ns", _boom),
        mock.patch.object(subprocess, "run", _boom),
        mock.patch.object(subprocess, "Popen", _boom),
        mock.patch.object(Path, "write_text", _boom),
        mock.patch.object(Path, "read_text", _boom),
        mock.patch.object(Path, "open", _boom),
    ):
        report = planner_mod.compare_parity(legacy, plans)
        section = manifest_renderer_mod.render_lifecycle_intents(plans)

    assert report.matched is True
    assert {e["identity"] for e in section["plans"]} == {p.identity for p in plans}


# HELDOUT-END
