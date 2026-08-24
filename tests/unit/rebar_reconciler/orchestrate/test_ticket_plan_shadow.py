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
