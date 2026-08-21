"""RP-06 S2 — the shared discovery-execution kernel (``rebar.llm.review_kernel.discovery``).

Contract tests for the typed discovery plan/result/outcome model and the dependency-aware
executor. The kernel gives both review gates the SAME trustworthy execution facts: which
units succeeded, resumed, were skipped, shed, failed, or cancelled — never an empty result
masquerading as success, never a later failure erasing an earlier success.

The tests inject a stateful fake ONLY at the model-call boundary (``run_unit``); the plan,
result, outcome, and envelope codecs are the REAL ones. Assertions are on observable
contracts — returned outcome kinds, exact usage accounting, checkpoint eligibility, and
collateral state — never on private structure.
"""

from __future__ import annotations

import pytest

from rebar.llm.review_kernel import discovery
from rebar.llm.review_kernel.discovery import (
    DISCOVERY_NAMESPACE_VERSION,
    CheckpointEnvelope,
    DiscoveryStagePlan,
    DiscoveryStageResult,
    DiscoveryUnitPlan,
    LocalOperationExhausted,
    MemoryCheckpointStore,
    SystemicDiscoveryError,
    Usage,
    execute_stage,
)

pytestmark = pytest.mark.unit


# ── fixtures / helpers ────────────────────────────────────────────────────────
def _unit(
    unit_id: str,
    *,
    deps: tuple[str, ...] = (),
    est: float = 1.0,
    blocking: bool = False,
    policy_digest: str = "pol",
    context_digest: str | None = None,
) -> DiscoveryUnitPlan:
    return DiscoveryUnitPlan(
        unit_id=unit_id,
        dependencies=deps,
        prompt_id=f"prompt::{unit_id}",
        contract_id="contract::v1",
        model="test-model",
        mode="single",
        context_digest=context_digest if context_digest is not None else f"ctx::{unit_id}",
        policy_digest=policy_digest,
        blocking=blocking,
        budget_estimate=est,
    )


def _stage(units, *, budget=None) -> DiscoveryStagePlan:
    return DiscoveryStagePlan(
        units=tuple(units),
        budget=budget,
        material="material-fingerprint",
        code_ref="ref::abcdef",
        topology_digest="topo::v1",
    )


class _Runner:
    """A stateful fake at the model-call boundary. Records dispatch order; returns
    per-unit content + usage; can be told to raise a per-unit or systemic error, and can
    run a side effect (e.g. flip a cancel flag) when a given unit is dispatched."""

    def __init__(self, *, usage_per_call=None, raise_local=(), raise_systemic=(), on_dispatch=None):
        self.dispatched: list[str] = []
        self._usage = usage_per_call or Usage(input_tokens=10, output_tokens=2, requests=1)
        self._raise_local = set(raise_local)
        self._raise_systemic = set(raise_systemic)
        self._on_dispatch = on_dispatch or {}

    def __call__(self, unit: DiscoveryUnitPlan):
        self.dispatched.append(unit.unit_id)
        if unit.unit_id in self._on_dispatch:
            self._on_dispatch[unit.unit_id]()
        if unit.unit_id in self._raise_systemic:
            raise SystemicDiscoveryError(f"provider init failed for {unit.unit_id}")
        if unit.unit_id in self._raise_local:
            raise LocalOperationExhausted(f"local op exhausted for {unit.unit_id}")
        return (f"content::{unit.unit_id}", self._usage)


def _kinds(result: DiscoveryStageResult) -> dict[str, str]:
    return {o.unit_id: o.kind for o in result.outcomes}


# ══════════════════════════════════════════════════════════════════════════════
# HAPPY PATH — the minimal specification of correct behaviour on well-formed input.
# (This section is what the implementation subagent sees.)
# ══════════════════════════════════════════════════════════════════════════════
def test_all_success_stage_reports_every_unit_success_with_aggregated_usage() -> None:
    # A → B (B depends on A); both succeed; result carries typed success outcomes and the
    # EXACT summed real usage, with no gate-specific verdict fields on the result.
    a, b = _unit("a"), _unit("b", deps=("a",))
    runner = _Runner(usage_per_call=Usage(input_tokens=100, output_tokens=20, requests=1))
    result = execute_stage(_stage([b, a]), run_unit=runner)

    assert _kinds(result) == {"a": "success", "b": "success"}
    # dependency order respected: a dispatched before its dependent b.
    assert runner.dispatched == ["a", "b"]
    # exact usage accounting: two real calls summed, nothing fabricated.
    assert result.usage == Usage(input_tokens=200, output_tokens=40, requests=2)
    # the six outcome kinds are the enumerable contract vocabulary.
    assert set(discovery.OUTCOME_KINDS) == {
        "success",
        "resumed",
        "skipped",
        "shed",
        "failed",
        "cancelled",
    }
    # no verdict fields leaked onto the execution result (that stays with the gate).
    assert not hasattr(result, "verdict")
    assert not hasattr(result.outcomes[0], "verdict")


def test_success_envelope_round_trips_through_the_real_codec() -> None:
    # A success unit's checkpoint envelope is digest-complete and survives JSON round-trip
    # byte-for-byte in identity, and is a reusable success.
    u = _unit("solo")
    stage = _stage([u])
    result = execute_stage(stage, run_unit=_Runner())

    env = result.outcomes[0].envelope
    assert isinstance(env, CheckpointEnvelope)
    assert env.namespace_version == DISCOVERY_NAMESPACE_VERSION
    assert env.kind == "success"

    round_tripped = CheckpointEnvelope.from_json(env.to_json())
    assert round_tripped == env
    assert round_tripped.is_reusable_success()
    # the digest is exactly the identity digest computed from the plan.
    assert env.digest == CheckpointEnvelope.identity_digest(unit_plan=u, stage_plan=stage)


def test_positive_budget_that_fits_sheds_nothing() -> None:
    # Two independent units, each estimate 1.0, budget 5.0 → everything runs, nothing shed.
    a, b = _unit("a", est=1.0), _unit("b", est=1.0)
    result = execute_stage(_stage([a, b], budget=5.0), run_unit=_Runner())
    assert _kinds(result) == {"a": "success", "b": "success"}


def test_plan_rejects_zero_budget_but_accepts_omission() -> None:
    # Zero is a likely-mistake sentinel, rejected by the contract; omission = uncapped.
    with pytest.raises(ValueError):
        DiscoveryStagePlan(units=(_unit("a"),), budget=0.0)
    with pytest.raises(ValueError):
        DiscoveryStagePlan(units=(_unit("a"),), budget=-1.0)
    # omission is valid and uncapped.
    assert DiscoveryStagePlan(units=(_unit("a"),), budget=None).budget is None


# ══════════════════════════════════════════════════════════════════════════════
# HELD-OUT ORACLE — edge / boundary / systemic / E2E behaviour.
# (Withheld from the implementation subagent. Run by the orchestrator only.)
# ══════════════════════════════════════════════════════════════════════════════
def test_local_failure_preserves_earlier_successes_and_skips_dependents() -> None:
    # AC2: A succeeds; B (independent) fails with an EXHAUSTED local op; C depends on B; D is
    # independent. Earlier success A is retained, the independent D still runs, and C — whose
    # dependency B is missing — becomes skipped naming the missing dependency id. A later
    # failure must NEVER erase an earlier success.
    a = _unit("a")
    b = _unit("b")
    c = _unit("c", deps=("b",))
    d = _unit("d")
    runner = _Runner(raise_local=("b",))
    result = execute_stage(_stage([a, b, c, d]), run_unit=runner)

    kinds = _kinds(result)
    assert kinds["a"] == "success"
    assert kinds["b"] == "failed"
    assert kinds["c"] == "skipped"
    assert kinds["d"] == "success"  # independent of the failure — still executed
    # the skip names the missing dependency id.
    c_out = next(o for o in result.outcomes if o.unit_id == "c")
    assert c_out.reason is not None and "b" in c_out.reason
    # no usage fabricated for the failed/skipped units.
    d_out = next(o for o in result.outcomes if o.unit_id == "d")
    assert d_out.usage.requests == 1
    # aggregate usage counts only the two real successful calls.
    assert result.usage.requests == 2


def test_systemic_failure_aborts_remaining_with_exact_accounting_no_fabricated_usage() -> None:
    # AC3: A succeeds, then B raises a SYSTEMIC (provider-init/config) error. Remaining work
    # stops; accounting is exact (A success, B failed) and later units are never dispatched
    # nor credited with fabricated usage/findings.
    a, b, c = _unit("a"), _unit("b"), _unit("c")
    runner = _Runner(raise_systemic=("b",))
    result = execute_stage(_stage([a, b, c]), run_unit=runner)

    kinds = _kinds(result)
    assert kinds["a"] == "success"
    assert kinds["b"] == "failed"
    # C was never dispatched (systemic abort halts scheduling).
    assert "c" not in runner.dispatched
    # exact accounting: no fabricated success/usage for the unrun unit.
    assert result.usage.requests == 1
    assert result.systemic_abort is True


def test_cancellation_commits_in_flight_dispatch_and_cancels_not_yet_dispatched() -> None:
    # AC3 cut-point: the executor observes cancellation BEFORE dispatch. A unit already
    # dispatched when cancellation is observed (non-interruptible sunk cost) commits as its
    # true outcome with real usage; a not-yet-dispatched unit is recorded ``cancelled``.
    flag = {"cancel": False}

    def _cancelled() -> bool:
        return flag["cancel"]

    # dispatching A flips the cancel flag mid-flight; A still completes and commits.
    runner = _Runner(on_dispatch={"a": lambda: flag.__setitem__("cancel", True)})
    a, b = _unit("a"), _unit("b")
    result = execute_stage(_stage([a, b]), run_unit=runner, cancelled=_cancelled)

    kinds = _kinds(result)
    assert kinds["a"] == "success"  # dispatched before cancel observed → committed
    assert kinds["b"] == "cancelled"  # not-yet-dispatched → cancelled
    assert "b" not in runner.dispatched
    # only the committed unit's real usage is counted; nothing fabricated for the cancelled one.
    assert result.usage.requests == 1


def test_budget_shedding_is_deterministic_across_runs() -> None:
    # AC4: three independent units, each estimate 2.0, budget 3.0 → only one fits, two shed.
    # The shed set is identical across repeated runs of the SAME plan and budget.
    units = [_unit("u3", est=2.0), _unit("u1", est=2.0), _unit("u2", est=2.0)]
    shed_sets = []
    for _ in range(3):
        result = execute_stage(_stage(units, budget=3.0), run_unit=_Runner())
        shed_sets.append(tuple(sorted(o.unit_id for o in result.outcomes if o.kind == "shed")))
    assert len(set(shed_sets)) == 1  # identical across runs
    assert len(shed_sets[0]) == 2  # exactly two shed (only one fits)


def test_budget_shed_cascades_unmet_dependents_to_skipped() -> None:
    # AC4/AC2: shedding a unit sheds its unmet dependents as ``skipped`` (never a success),
    # and the loop stops once the kept set fits (the minimal shed set).
    root = _unit("root", est=5.0)
    dependent = _unit("dep", deps=("root",), est=1.0, blocking=True)
    keep = _unit("keep", est=1.0)
    result = execute_stage(_stage([root, dependent, keep], budget=2.0), run_unit=_Runner())
    kinds = _kinds(result)
    assert kinds["root"] == "shed"
    assert kinds["dep"] == "skipped"  # dependency was shed; dep is blocking, not shed itself
    assert kinds["keep"] == "success"  # fits the budget, still runs
    dep_out = next(o for o in result.outcomes if o.unit_id == "dep")
    assert dep_out.reason is not None and "root" in dep_out.reason


def test_resumed_reuses_a_content_identical_success_without_a_new_call() -> None:
    # AC5/AC7: a prior success in the store resumes without re-dispatching run_unit; the
    # resumed outcome is a reusable success and no fresh model call is spent.
    u = _unit("cached")
    stage = _stage([u])
    store = MemoryCheckpointStore()
    execute_stage(stage, run_unit=_Runner(), store=store)  # populate the store

    runner2 = _Runner()
    result = execute_stage(stage, run_unit=runner2, store=store)
    assert _kinds(result) == {"cached": "resumed"}
    assert runner2.dispatched == []  # no new model call
    assert result.usage.requests == 0  # resume spends nothing


def test_only_success_and_resume_round_trip_missing_states_stay_missing() -> None:
    # AC5: failed / cancelled / skipped / shed units are NOT committed as reusable successes;
    # injected corrupt and legacy (wrong/absent namespace version) envelopes remain missing.
    u = _unit("x")
    stage = _stage([u])

    # a failed unit leaves no reusable checkpoint.
    store = MemoryCheckpointStore()
    execute_stage(stage, run_unit=_Runner(raise_local=("x",)), store=store)
    assert store.load_raw("x") is None

    # a corrupt raw envelope is treated as missing (re-runs).
    store2 = MemoryCheckpointStore()
    store2.put_raw("x", "{not valid json")
    runner = _Runner()
    result = execute_stage(stage, run_unit=runner, store=store2)
    assert _kinds(result) == {"x": "success"}
    assert runner.dispatched == ["x"]  # corrupt → recomputed

    # a legacy envelope (mismatched namespace version) is not read as a new success.
    good_result = execute_stage(stage, run_unit=_Runner(), store=MemoryCheckpointStore())
    good = good_result.outcomes[0].envelope
    legacy_json = good.to_json().replace(f'"{DISCOVERY_NAMESPACE_VERSION}"', '"0"')
    store3 = MemoryCheckpointStore()
    store3.put_raw("x", legacy_json)
    runner3 = _Runner()
    result3 = execute_stage(stage, run_unit=runner3, store=store3)
    assert _kinds(result3) == {"x": "success"}
    assert runner3.dispatched == ["x"]  # legacy → recomputed


def test_envelope_digest_excludes_nothing_material_a_context_change_breaks_reuse() -> None:
    # AC5: the envelope identity is digest-COMPLETE — a change to the input context digest
    # yields a different identity, so a stale checkpoint is not reused.
    u1 = _unit("y", context_digest="ctx-v1")
    u2 = _unit("y", context_digest="ctx-v2")
    stage1, stage2 = _stage([u1]), _stage([u2])
    store = MemoryCheckpointStore()
    execute_stage(stage1, run_unit=_Runner(), store=store)  # cache under ctx-v1

    runner = _Runner()
    result = execute_stage(stage2, run_unit=runner, store=store)  # ctx-v2 ⇒ different digest
    assert _kinds(result) == {"y": "success"}
    assert runner.dispatched == ["y"]  # not reused — context changed


def test_fault_injection_restore_then_clean_rerun_leaves_unrelated_state_unchanged() -> None:
    # AC7: A succeeds and is checkpointed; B fails on run 1. On run 2 (fault cleared) A RESUMES
    # from its checkpoint (unchanged) and B now succeeds — a clean full rerun, with A's earlier
    # success state untouched.
    a, b = _unit("a"), _unit("b")
    stage = _stage([a, b])
    store = MemoryCheckpointStore()

    r1 = execute_stage(stage, run_unit=_Runner(raise_local=("b",)), store=store)
    assert _kinds(r1) == {"a": "success", "b": "failed"}
    a_out_run1 = next(o for o in r1.outcomes if o.unit_id == "a")
    a_env_run1 = a_out_run1.envelope

    runner2 = _Runner()
    r2 = execute_stage(stage, run_unit=runner2, store=store)
    assert _kinds(r2) == {"a": "resumed", "b": "success"}
    assert runner2.dispatched == ["b"]  # A resumed, only B re-ran
    # A's checkpoint is byte-identical across the rerun (unrelated success state unchanged).
    a_out_run2 = next(o for o in r2.outcomes if o.unit_id == "a")
    assert a_out_run2.envelope.digest == a_env_run1.digest
    # the raw stored bytes still round-trip to the identical envelope (state untouched).
    assert CheckpointEnvelope.from_json(store.load_raw(a_env_run1.digest)) == a_env_run1
