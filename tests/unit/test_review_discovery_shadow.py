"""RP-06 S7 — the cross-gate discovery SHADOW comparator
(``rebar.llm.review_kernel.discovery_shadow``).

The shadow comparator certifies the pre-kernel→kernel cutover: from ONE observed
call/result set of a review run it reconstructs the *legacy* reference derivation and
compares it, field by field, against the *RP-06* kernel derivation. Every divergence is
reported EXCEPT the small, enumerated approved-delta allowlist (the deliberate behavior
changes the cutover introduced). The comparator is a pure, deterministic function — it
issues NO provider/model call and never controls a gate's final verdict.

All assertions here are on OBSERVABLE contracts: the returned ``ComparisonReport`` (its
``mismatches`` / ``accepted`` / ``ok``), the ``Mismatch`` field-level shape, and the
reconstructed projection's outcomes/dispositions — never private structure or source text.
"""

from __future__ import annotations

import pytest

from rebar.llm.review_kernel import discovery_shadow as ds

pytestmark = pytest.mark.unit


# ── projection builders (observable inputs only) ───────────────────────────────
def _unit(unit_id: str, **kw: object) -> ds.UnitProjection:
    """One projected discovery unit; every field defaults to a cutover-invariant value so a
    test overrides only the axis it exercises."""
    base: dict[str, object] = {
        "prompt_id": f"prompt:{unit_id}",
        "contract_id": "findings",
        "context_digest": "",
        "model": "standard",
        "mode": "single",
        "dependencies": (),
        "budget_estimate": 1.0,
        "posture": "advisory",
        "usage": {"input_tokens": 10, "output_tokens": 5, "requests": 1},
        "outcome_kind": "success",
        "checkpoint_written": True,
        "source": "builtin",
        "exec_tier": "LLM",
        "overlay_disabled": False,
        "overlay_retuned": False,
        "legacy_posture": "advisory",
        "project_applies": True,
    }
    base.update(kw)
    return ds.UnitProjection(unit_id=unit_id, **base)  # type: ignore[arg-type]


def _stage(units: tuple[ds.UnitProjection, ...], **kw: object) -> ds.StageProjection:
    params: dict[str, object] = {
        "gate": "plan_review",
        "disposition": "PASS",
        "budget": None,
        "systemic_abort": False,
    }
    params.update(kw)
    return ds.StageProjection(units=units, **params)  # type: ignore[arg-type]


def _fields(report: ds.ComparisonReport) -> set[str]:
    return {m.field for m in report.mismatches}


def _categories(report: ds.ComparisonReport) -> set[str]:
    return {a.category for a in report.accepted}


# ══════════════════════════════════════════════════════════════════════════════
# HAPPY PATH — the diff machinery + Mismatch shape + no-overlay reconstruction.
# (These remain visible to the blind implementer.)
# ══════════════════════════════════════════════════════════════════════════════
def test_matching_projections_report_ok_with_no_mismatches() -> None:
    """Two identical projections diff to nothing: ``ok`` is True and both lists are empty."""
    legacy = _stage((_unit("a"), _unit("b")))
    rp06 = _stage((_unit("a"), _unit("b")))
    report = ds.compare_projections(legacy, rp06)
    assert report.ok is True
    assert report.mismatches == ()
    assert report.accepted == ()


def test_an_unapproved_field_change_is_reported_at_field_level() -> None:
    """A model swap on a built-in (no retune/disable signal) is an unapproved divergence:
    exactly one Mismatch naming the unit, the field, and BOTH values."""
    legacy = _stage((_unit("a", model="standard"),))
    rp06 = _stage((_unit("a", model="frontier"),))
    report = ds.compare_projections(legacy, rp06)
    assert report.ok is False
    assert len(report.mismatches) == 1
    m = report.mismatches[0]
    assert m.unit_id == "a"
    assert m.field == "model"
    assert m.legacy == "standard"
    assert m.rp06 == "frontier"


def test_no_overlay_run_reconstructs_to_an_identical_legacy_projection() -> None:
    """A plain built-in run with no overlay/policy signals reconstructs a legacy projection
    equal to the observed RP-06 one — the comparator confirms the cutover was a no-op."""
    rp06 = _stage((_unit("a"), _unit("b")))
    report = ds.compare_discovery(ds.ObservedDiscovery(rp06=rp06))
    assert report.ok is True
    assert report.mismatches == ()


# ══════════════════════════════════════════════════════════════════════════════
# HELD-OUT — allowlist categories (AC1), the nine cross-gate fixtures (AC2),
# legacy-checkpoint ignore + success-only checkpointing (AC5), purity (AC1).
# ══════════════════════════════════════════════════════════════════════════════


# ── AC1: the approved-delta allowlist, one category at a time ───────────────────
def test_allowlist_names_exactly_the_six_approved_deltas() -> None:
    assert ds.APPROVED_DELTAS == frozenset(
        {
            "builtin_retune",
            "project_det",
            "project_llm_applicability",
            "code_review_budget",
            "partial_failure_continuation",
            "success_only_checkpoint",
        }
    )


def test_effective_disabled_builtin_is_an_approved_selection_delta() -> None:
    """A built-in the overlay DISABLES runs in legacy but not RP-06 — an accepted delta,
    not a mismatch."""
    disabled = _unit("sec", source="builtin", overlay_disabled=True, outcome_kind=None)
    legacy = _stage((_unit("a"), disabled))
    rp06 = _stage((_unit("a"),))
    report = ds.compare_projections(legacy, rp06)
    assert report.ok is True
    assert ds.AcceptedDelta(unit_id="sec", field="selected", category="builtin_retune") in (
        report.accepted
    )


def test_effective_retuned_builtin_posture_is_an_approved_delta() -> None:
    legacy = _stage((_unit("a", posture="advisory", legacy_posture="advisory"),))
    rp06 = _stage(
        (_unit("a", posture="blocking", overlay_retuned=True, legacy_posture="advisory"),)
    )
    report = ds.compare_projections(legacy, rp06)
    assert report.ok is True
    assert "builtin_retune" in _categories(report)


def test_project_det_inclusion_is_an_approved_selection_delta() -> None:
    """A project DETERMINISTIC criterion runs only under RP-06 (legacy had no project-DET
    tier) — accepted."""
    det = _unit("project.style", source="project", exec_tier="DET")
    legacy = _stage((_unit("a"),))
    rp06 = _stage((_unit("a"), det))
    report = ds.compare_projections(legacy, rp06)
    assert report.ok is True
    assert (
        ds.AcceptedDelta(unit_id="project.style", field="selected", category="project_det")
        in report.accepted
    )


def test_scoped_project_llm_not_applying_is_an_approved_selection_delta() -> None:
    """A scoped project-LLM criterion whose ``applies_to`` does not match runs in legacy
    (ungated) but is filtered out by RP-06 — accepted."""
    scoped = _unit(
        "project.sec", source="project", exec_tier="LLM", project_applies=False, outcome_kind=None
    )
    legacy = _stage((_unit("a"), scoped))
    rp06 = _stage((_unit("a"),))
    report = ds.compare_projections(legacy, rp06)
    assert report.ok is True
    assert "project_llm_applicability" in _categories(report)


def test_positive_code_review_budget_enforcement_is_an_approved_delta() -> None:
    """Code review enforces a positive budget legacy never had — the budget field differing
    from legacy's uncapped None is accepted only on the code_review gate."""
    legacy = _stage((_unit("a"),), gate="code_review", budget=None)
    rp06 = _stage((_unit("a"),), gate="code_review", budget=5.0)
    report = ds.compare_projections(legacy, rp06)
    assert report.ok is True
    assert ds.AcceptedDelta(unit_id=None, field="budget", category="code_review_budget") in (
        report.accepted
    )


def test_typed_partial_failure_continuation_is_an_approved_delta() -> None:
    """Legacy aborts the whole stage on a local failure; RP-06 continues independent units.
    The disposition + downstream-outcome divergences are accepted, not mismatches."""
    legacy = _stage(
        (
            _unit("a", outcome_kind="success"),
            _unit("b", outcome_kind="failed"),
            _unit("c", outcome_kind="skipped"),
        ),
        disposition="INDETERMINATE",
    )
    rp06 = _stage(
        (
            _unit("a", outcome_kind="success"),
            _unit("b", outcome_kind="failed"),
            _unit("c", outcome_kind="success"),
        ),
        disposition="PASS",
    )
    report = ds.compare_projections(legacy, rp06)
    assert report.ok is True
    assert "partial_failure_continuation" in _categories(report)


def test_success_only_checkpointing_is_an_approved_delta() -> None:
    """Legacy checkpoints every terminal outcome; RP-06 checkpoints only successes. A failed
    unit written by legacy but not RP-06 is accepted."""
    legacy = _stage((_unit("b", outcome_kind="failed", checkpoint_written=True),))
    rp06 = _stage((_unit("b", outcome_kind="failed", checkpoint_written=False),))
    report = ds.compare_projections(legacy, rp06)
    assert report.ok is True
    assert "success_only_checkpoint" in _categories(report)


# ── AC1: the allowlist is exact — look-alikes that are NOT approved still fire ──
def test_dropping_a_non_disabled_builtin_is_not_an_approved_delta() -> None:
    """RP-06 dropping a built-in that the overlay did NOT disable is a real regression —
    reported despite superficially resembling the disable delta."""
    live = _unit("sec", source="builtin", overlay_disabled=False, outcome_kind=None)
    legacy = _stage((_unit("a"), live))
    rp06 = _stage((_unit("a"),))
    report = ds.compare_projections(legacy, rp06)
    assert report.ok is False
    assert "selected" in _fields(report)


def test_positive_budget_on_the_plan_review_gate_is_not_approved() -> None:
    """The budget-enforcement delta is scoped to code_review; the same divergence on the
    plan_review gate is unapproved."""
    legacy = _stage((_unit("a"),), gate="plan_review", budget=None)
    rp06 = _stage((_unit("a"),), gate="plan_review", budget=5.0)
    report = ds.compare_projections(legacy, rp06)
    assert report.ok is False
    assert "budget" in _fields(report)


def test_posture_change_without_a_retune_signal_is_not_approved() -> None:
    legacy = _stage((_unit("a", posture="advisory", legacy_posture="advisory"),))
    rp06 = _stage(
        (_unit("a", posture="blocking", overlay_retuned=False, legacy_posture="advisory"),)
    )
    report = ds.compare_projections(legacy, rp06)
    assert report.ok is False
    assert "posture" in _fields(report)


def test_a_gate_mismatch_is_always_reported() -> None:
    legacy = _stage((_unit("a"),), gate="plan_review")
    rp06 = _stage((_unit("a"),), gate="code_review")
    report = ds.compare_projections(legacy, rp06)
    assert report.ok is False
    assert "gate" in _fields(report)


@pytest.mark.parametrize(
    "field",
    ["prompt_id", "contract_id", "context_digest", "mode", "dependencies"],
)
def test_identity_field_divergence_is_always_reported(field: str) -> None:
    """Prompt/contract identity, context hash, mode and dependencies are cutover-invariant:
    any divergence is a mismatch (never approved)."""
    alt = {"dependencies": ("x",)}.get(field, "MUTATED")
    legacy = _stage((_unit("a"),))
    rp06 = _stage((_unit("a", **{field: alt}),))
    report = ds.compare_projections(legacy, rp06)
    assert report.ok is False
    assert field in _fields(report)


# ── AC2: the nine cross-gate fixtures → approved dispositions + trace outcomes ──
def _observed_no_overlay() -> ds.ObservedDiscovery:
    return ds.ObservedDiscovery(rp06=_stage((_unit("a"), _unit("b"))))


def _observed_disable_retune() -> ds.ObservedDiscovery:
    disabled = _unit("sec", source="builtin", overlay_disabled=True, outcome_kind=None)
    rp06 = _stage(
        (_unit("a", posture="blocking", overlay_retuned=True, legacy_posture="advisory"),)
    )
    return ds.ObservedDiscovery(rp06=rp06, excluded=(disabled,))


def _observed_project_llm() -> ds.ObservedDiscovery:
    glob = _unit("project.global", source="project", exec_tier="LLM", project_applies=True)
    scoped = _unit(
        "project.scoped",
        source="project",
        exec_tier="LLM",
        project_applies=False,
        outcome_kind=None,
    )
    rp06 = _stage((_unit("a"), glob), gate="code_review")
    return ds.ObservedDiscovery(rp06=rp06, excluded=(scoped,))


def _observed_project_det() -> ds.ObservedDiscovery:
    det = _unit("project.det", source="project", exec_tier="DET")
    return ds.ObservedDiscovery(rp06=_stage((_unit("a"), det), gate="code_review"))


def _observed_budget_shed() -> ds.ObservedDiscovery:
    shed = _unit("c", outcome_kind="shed", checkpoint_written=False)
    rp06 = _stage((_unit("a"), _unit("b"), shed), gate="code_review", budget=2.0)
    return ds.ObservedDiscovery(rp06=rp06)


def _observed_local_failure() -> ds.ObservedDiscovery:
    rp06 = _stage(
        (
            _unit("a", outcome_kind="success"),
            _unit("b", outcome_kind="failed", checkpoint_written=False),
            _unit("c", outcome_kind="success"),
        ),
        disposition="PASS",
    )
    return ds.ObservedDiscovery(rp06=rp06)


def _observed_systemic_failure() -> ds.ObservedDiscovery:
    rp06 = _stage(
        (
            _unit("a", outcome_kind="failed", checkpoint_written=False),
            _unit("b", outcome_kind="skipped", checkpoint_written=False),
        ),
        disposition="INDETERMINATE",
        systemic_abort=True,
    )
    return ds.ObservedDiscovery(rp06=rp06)


def _observed_cancellation() -> ds.ObservedDiscovery:
    rp06 = _stage(
        (
            _unit("a", outcome_kind="success"),
            _unit("b", outcome_kind="cancelled", checkpoint_written=False),
        ),
        disposition="INDETERMINATE",
    )
    return ds.ObservedDiscovery(rp06=rp06)


def _observed_legacy_checkpoint() -> ds.ObservedDiscovery:
    # A stored LEGACY-namespace envelope exists for unit "a", but RP-06 recomputed it
    # (outcome success, not "resumed") — the envelope was ignored.
    rp06 = _stage((_unit("a", outcome_kind="success"), _unit("b")))
    return ds.ObservedDiscovery(rp06=rp06, legacy_checkpoints=frozenset({"a"}))


_FIXTURES = {
    "no_overlay": _observed_no_overlay,
    "disable_retune": _observed_disable_retune,
    "project_llm": _observed_project_llm,
    "project_det": _observed_project_det,
    "budget_shed": _observed_budget_shed,
    "local_failure": _observed_local_failure,
    "systemic_failure": _observed_systemic_failure,
    "cancellation": _observed_cancellation,
    "legacy_checkpoint": _observed_legacy_checkpoint,
}


@pytest.mark.parametrize("name", sorted(_FIXTURES))
def test_cross_gate_fixture_yields_only_approved_deltas(name: str) -> None:
    """Every cross-gate scenario reconstructs to the approved final disposition + trace
    outcomes: ``compare_discovery`` finds no unapproved mismatch."""
    report = ds.compare_discovery(_FIXTURES[name]())
    assert report.ok is True, f"{name} produced unapproved mismatches: {report.mismatches}"


def test_local_failure_fixture_records_the_continued_success_downstream() -> None:
    """The partial-failure fixture's independent unit really did continue to success under
    RP-06 while the reconstructed legacy would have aborted it."""
    report = ds.compare_discovery(_observed_local_failure())
    assert report.ok is True
    assert "partial_failure_continuation" in _categories(report)


def test_systemic_and_cancellation_fixtures_match_legacy_with_no_delta() -> None:
    """A systemic abort and a cancellation are pre-kernel behaviors legacy shares, so the
    reconstruction matches exactly — ok with NO accepted deltas of those kinds needed."""
    for name in ("systemic_failure", "cancellation"):
        report = ds.compare_discovery(_FIXTURES[name]())
        assert report.ok is True, name


# ── AC5: legacy-checkpoint ignore + success-only checkpoint enforcement ─────────
def test_a_legacy_checkpoint_envelope_never_changes_the_comparison() -> None:
    """Presence of a legacy-namespace checkpoint is inert: the report is identical to the
    run without it (the envelope is IGNORED, never resumed)."""
    with_ckpt = ds.compare_discovery(_observed_legacy_checkpoint())
    without = ds.compare_discovery(ds.ObservedDiscovery(rp06=_stage((_unit("a"), _unit("b")))))
    assert with_ckpt.ok is True and without.ok is True
    assert with_ckpt.mismatches == without.mismatches


def test_resuming_from_a_legacy_checkpoint_is_reported_as_a_mismatch() -> None:
    """If RP-06 had actually RESUMED a unit off a legacy envelope, that violates the
    ignore-legacy rule and is a mismatch."""
    rp06 = _stage((_unit("a", outcome_kind="resumed", checkpoint_written=False),))
    observed = ds.ObservedDiscovery(rp06=rp06, legacy_checkpoints=frozenset({"a"}))
    report = ds.compare_discovery(observed)
    assert report.ok is False
    assert "checkpoint" in _fields(report)


def test_checkpointing_a_non_success_outcome_violates_success_only() -> None:
    """RP-06 writing a checkpoint for a failed unit breaks the success-only rule — reported
    even though the legacy side also wrote one (which alone would be an approved delta)."""
    rp06 = _stage((_unit("b", outcome_kind="failed", checkpoint_written=True),))
    report = ds.compare_discovery(ds.ObservedDiscovery(rp06=rp06))
    assert report.ok is False
    assert "checkpoint_written" in _fields(report)


# ── AC1: the comparator is pure — no provider call, deterministic ──────────────
def test_compare_discovery_is_deterministic() -> None:
    observed = _observed_disable_retune()
    first = ds.compare_discovery(observed)
    second = ds.compare_discovery(observed)
    assert first.mismatches == second.mismatches
    assert first.accepted == second.accepted


def test_comparator_takes_no_runner_or_store_argument() -> None:
    """The public entry points accept only the observed set (+ the allowlist) — there is no
    runner/store/model seam through which a provider call could be issued."""
    import inspect

    for fn in (ds.compare_discovery, ds.compare_projections, ds.reconstruct_legacy):
        params = set(inspect.signature(fn).parameters)
        assert not (params & {"run_unit", "runner", "store", "model", "client", "provider"}), fn


def test_reconstruct_legacy_is_pure_and_leaves_rp06_untouched() -> None:
    """Reconstruction derives the legacy projection WITHOUT mutating the observed RP-06
    projection (frozen dataclasses; a second reconstruct yields an equal result)."""
    rp06 = _stage(
        (_unit("a"), _unit("project.det", source="project", exec_tier="DET")), gate="code_review"
    )
    before = rp06
    legacy = ds.reconstruct_legacy(rp06)
    assert rp06 == before
    assert ds.reconstruct_legacy(rp06) == legacy
    # Legacy dropped the project-DET unit (it had no project-DET tier).
    assert "project.det" not in {u.unit_id for u in legacy.units}
