"""HELD-OUT edge oracle for the fixture ADMISSION runner (ticket 67aa).

Withheld from the implementation subagent and validated by the orchestrator. Covers every
disposition the happy path does not: drift on non-reproduction and on unrecoverable material,
the budget ceiling and the post-``finalize`` actual-spend cap halt, the no-shadowing packaged
skip, unbalanced/mid-raise atomicity, crash-consistent resume, and transient-error retry.

Shared scaffolding is imported from the visible happy-path module (``test_fixture_admission``),
the same cross-module pattern ``test_fixture_emit_heldout`` uses.
"""

from __future__ import annotations

import json

import pytest
import yaml
from test_fixture_admission import (
    CHEAP,
    ScriptedSolver,
    admission_paths,
    candidate,
    case_id,
    run_admission,
    sidecar_row,
    stub_genai_prices,
    usage_row,
    write_manifest_file,
)

from rebar.llm.criteria.ids import criterion_prompt_id
from rebar.llm.evals.eval import validate_eval_spec
from rebar.llm.evals.fixture_admission import CaseOutcome, TransientSolverError
from rebar.llm.evals.plan_replay import ledger

pytestmark = pytest.mark.unit


def _balanced_manifest(crit, tmp_path, *, fire_uuid="uuid-fire", pass_uuid="uuid-pass"):
    rows = [
        candidate(crit, "fire", 0, fire_uuid),
        candidate(crit, "no_fire", 0, pass_uuid),
    ]
    return write_manifest_file(rows, tmp_path)


def _material(
    fire_uuid="uuid-fire", pass_uuid="uuid-pass", *, fire_verified=True, pass_verified=True
):
    return {
        fire_uuid: sidecar_row(
            fire_uuid, ticket_id="t-fire", description="FIRE PLAN", verified=fire_verified
        ),
        pass_uuid: sidecar_row(
            pass_uuid, ticket_id="t-pass", description="PASS PLAN", verified=pass_verified
        ),
    }


# ── AC2: non-reproduction drifts; unrecoverable material drifts with no finder call ──────
def test_nonreproducing_fire_case_is_withheld_to_drift(tmp_path, monkeypatch):
    stub_genai_prices(monkeypatch, usd_per_row=0.01)
    crit = "project.alpha"
    manifest = _balanced_manifest(crit, tmp_path)
    solver = ScriptedSolver(
        {
            case_id(crit, "fire", 0): [
                True,
                False,
                False,
            ],  # majority silent ≠ expect finding → drift
            case_id(crit, "no_fire", 0): [False, False, True],  # reproduces
        }
    )
    p = admission_paths(tmp_path)

    summary = run_admission(
        manifest,
        material_index=_material(),
        solver=solver,
        out_dir=p["out_dir"],
        drift_path=p["drift_path"],
        ledger_path=p["ledger_path"],
        cap_usd=250.0,
        reserve_usd=0.0,
        epochs=3,
        packaged_ids=frozenset(),
        tier_for=lambda _c: CHEAP,
    )

    # The fire side drifted → the criterion loses its fire candidates → unbalanced → no spec.
    assert crit in summary.withheld
    assert crit not in summary.admitted
    assert not (p["out_dir"] / f"{criterion_prompt_id(crit)}.eval.yaml").exists()
    drift = [d for d in summary.drift if d.case_id == case_id(crit, "fire", 0)]
    assert len(drift) == 1
    d = drift[0]
    assert d.predicted == "finding" and d.observed == "pass"
    # Provenance is the originating review (ticket_id, review_event_uuid) from the matched row.
    assert d.ticket_id == "t-fire" and d.review_event_uuid == "uuid-fire"
    # A drift report file was written.
    assert p["drift_path"].exists()
    report = p["drift_path"].read_text(encoding="utf-8")
    assert "uuid-fire" in report and crit in report


def test_candidate_with_no_verified_row_is_unrecoverable_no_finder_call(tmp_path, monkeypatch):
    stub_genai_prices(monkeypatch, usd_per_row=0.01)
    crit = "project.alpha"
    manifest = _balanced_manifest(crit, tmp_path)
    # The fire candidate's uuid resolves to NO row at all; the pass candidate's row is unverified.
    material = {
        "uuid-pass": sidecar_row(
            "uuid-pass", ticket_id="t-pass", description="PASS PLAN", verified=False
        )
    }
    solver = ScriptedSolver(
        {
            case_id(crit, "no_fire", 0): [False, False, True],
        }
    )
    p = admission_paths(tmp_path)

    summary = run_admission(
        manifest,
        material_index=material,
        solver=solver,
        out_dir=p["out_dir"],
        drift_path=p["drift_path"],
        ledger_path=p["ledger_path"],
        cap_usd=250.0,
        reserve_usd=0.0,
        epochs=3,
        packaged_ids=frozenset(),
        tier_for=lambda _c: CHEAP,
    )

    # Neither candidate resolved to a verified row → the finder was never called at all.
    assert solver.calls == []
    assert crit in summary.withheld and crit not in summary.admitted
    reasons = {d.reason for d in summary.drift}
    assert reasons == {"unrecoverable-material"}
    fire_drift = [d for d in summary.drift if d.review_event_uuid == "uuid-fire"]
    assert len(fire_drift) == 1 and fire_drift[0].ticket_id is None


# ── AC3: ledger at the ceiling → first reserve raises, zero model calls ──────────────────
def test_ledger_at_ceiling_raises_budget_exceeded_zero_calls(tmp_path, monkeypatch):
    stub_genai_prices(monkeypatch, usd_per_row=0.01)
    crit = "project.alpha"
    manifest = _balanced_manifest(crit, tmp_path)
    p = admission_paths(tmp_path)
    # Pre-load the ledger to the ceiling so no allocation remains.
    p["ledger_path"].write_text(
        json.dumps({"run_id": "prior", "tier": CHEAP, "usd": 250.0}) + "\n", encoding="utf-8"
    )
    solver = ScriptedSolver({})  # any call would KeyError; we assert there are none

    with pytest.raises(ledger.BudgetExceeded):
        run_admission(
            manifest,
            material_index=_material(),
            solver=solver,
            out_dir=p["out_dir"],
            drift_path=p["drift_path"],
            ledger_path=p["ledger_path"],
            cap_usd=250.0,
            reserve_usd=0.0,
            epochs=3,
            packaged_ids=frozenset(),
            tier_for=lambda _c: CHEAP,
        )
    assert solver.calls == []
    assert not (p["out_dir"] / f"{criterion_prompt_id(crit)}.eval.yaml").exists()


# ── AC4: post-finalize actual spend meets the cap → halt before the next criterion ───────
def test_actual_spend_reaching_cap_halts_before_next_criterion(tmp_path, monkeypatch):
    # Criterion A finalizes 6 usage rows (2 cases x 3 epochs); pricing each at 250 makes its
    # actual spend EXACTLY the 1500 cap, so the halt boundary is `>=` (fires) not `>` (misses).
    stub_genai_prices(monkeypatch, usd_per_row=250.0)
    cap = 1500.0
    rows = [
        candidate("project.aaa", "fire", 0, "a-fire"),
        candidate("project.aaa", "no_fire", 0, "a-pass"),
        candidate("project.zzz", "fire", 0, "z-fire"),
        candidate("project.zzz", "no_fire", 0, "z-pass"),
    ]
    manifest = write_manifest_file(rows, tmp_path)
    material = {
        "a-fire": sidecar_row("a-fire", ticket_id="ta", description="A FIRE"),
        "a-pass": sidecar_row("a-pass", ticket_id="ta2", description="A PASS"),
        "z-fire": sidecar_row("z-fire", ticket_id="tz", description="Z FIRE"),
        "z-pass": sidecar_row("z-pass", ticket_id="tz2", description="Z PASS"),
    }
    solver = ScriptedSolver(
        {
            case_id("project.aaa", "fire", 0): [True, True, True],
            case_id("project.aaa", "no_fire", 0): [False, False, False],
            case_id("project.zzz", "fire", 0): [True, True, True],
            case_id("project.zzz", "no_fire", 0): [False, False, False],
        }
    )
    p = admission_paths(tmp_path)

    summary = run_admission(
        manifest,
        material_index=material,
        solver=solver,
        out_dir=p["out_dir"],
        drift_path=p["drift_path"],
        ledger_path=p["ledger_path"],
        cap_usd=cap,
        reserve_usd=0.0,
        epochs=3,
        packaged_ids=frozenset(),
        tier_for=lambda _c: CHEAP,
    )

    # A (processed first, sorted) is admitted; the cap breach halts before Z is ever solved.
    assert "project.aaa" in summary.admitted
    assert "project.zzz" not in summary.admitted
    assert case_id("project.zzz", "fire", 0) not in solver.case_ids()
    assert (p["out_dir"] / f"{criterion_prompt_id('project.aaa')}.eval.yaml").exists()
    assert not (p["out_dir"] / f"{criterion_prompt_id('project.zzz')}.eval.yaml").exists()


# ── AC6: a packaged built-in is skipped; a same-id .rebar/evals prior artifact is not ────
def test_packaged_builtin_is_skipped_prior_artifact_is_not(tmp_path, monkeypatch):
    stub_genai_prices(monkeypatch, usd_per_row=0.01)
    pkg, ok = "project.packaged", "project.fresh"
    rows = [
        candidate(pkg, "fire", 0, "p-fire"),
        candidate(pkg, "no_fire", 0, "p-pass"),
        candidate(ok, "fire", 0, "o-fire"),
        candidate(ok, "no_fire", 0, "o-pass"),
    ]
    manifest = write_manifest_file(rows, tmp_path)
    material = {
        "p-fire": sidecar_row("p-fire", ticket_id="p1", description="P FIRE"),
        "p-pass": sidecar_row("p-pass", ticket_id="p2", description="P PASS"),
        "o-fire": sidecar_row("o-fire", ticket_id="o1", description="O FIRE"),
        "o-pass": sidecar_row("o-pass", ticket_id="o2", description="O PASS"),
    }
    solver = ScriptedSolver(
        {
            case_id(ok, "fire", 0): [True, True, True],
            case_id(ok, "no_fire", 0): [False, False, False],
        }
    )
    p = admission_paths(tmp_path)
    # A same-id prior artifact already sits in the output dir — it must be overwritten, not
    # mistaken for a packaged shadow that should be skipped.
    p["out_dir"].mkdir(parents=True)
    prior = p["out_dir"] / f"{criterion_prompt_id(ok)}.eval.yaml"
    prior.write_text("stale: artifact\n", encoding="utf-8")

    summary = run_admission(
        manifest,
        material_index=material,
        solver=solver,
        out_dir=p["out_dir"],
        drift_path=p["drift_path"],
        ledger_path=p["ledger_path"],
        cap_usd=250.0,
        reserve_usd=0.0,
        epochs=3,
        packaged_ids=frozenset({pkg}),
        tier_for=lambda _c: CHEAP,
    )

    assert ok in summary.admitted
    assert pkg not in summary.admitted
    # The packaged criterion was never solved.
    assert case_id(pkg, "fire", 0) not in solver.case_ids()
    # The prior artifact was overwritten with a real, strict-valid spec.
    written = {q.name for q in p["out_dir"].glob("*.eval.yaml")}
    assert f"{criterion_prompt_id(pkg)}.eval.yaml" not in written
    assert f"{criterion_prompt_id(ok)}.eval.yaml" in written
    spec = yaml.safe_load(prior.read_text(encoding="utf-8"))
    assert validate_eval_spec(spec, strict=True) == []


# ── AC7: unbalanced reproducing set → no spec; a mid-criterion raise → no partial file ───
def test_unbalanced_reproducing_set_writes_no_spec(tmp_path, monkeypatch):
    stub_genai_prices(monkeypatch, usd_per_row=0.01)
    crit = "project.alpha"
    manifest = _balanced_manifest(crit, tmp_path)
    solver = ScriptedSolver(
        {
            case_id(crit, "fire", 0): [True, True, True],  # reproduces
            case_id(crit, "no_fire", 0): [True, True, True],  # fires ≠ expect pass → drifts
        }
    )
    p = admission_paths(tmp_path)

    summary = run_admission(
        manifest,
        material_index=_material(),
        solver=solver,
        out_dir=p["out_dir"],
        drift_path=p["drift_path"],
        ledger_path=p["ledger_path"],
        cap_usd=250.0,
        reserve_usd=0.0,
        epochs=3,
        packaged_ids=frozenset(),
        tier_for=lambda _c: CHEAP,
    )

    assert crit in summary.withheld and crit not in summary.admitted
    assert not (p["out_dir"] / f"{criterion_prompt_id(crit)}.eval.yaml").exists()
    # The reproducing fire case is still recorded as drift alongside the non-reproducing one,
    # since the whole criterion is withheld.
    assert {d.case_id for d in summary.drift} >= {case_id(crit, "no_fire", 0)}


def test_mid_criterion_nontransient_raise_leaves_no_partial_file(tmp_path, monkeypatch):
    stub_genai_prices(monkeypatch, usd_per_row=0.01)
    crit = "project.alpha"
    manifest = _balanced_manifest(crit, tmp_path)
    solver = ScriptedSolver(
        {case_id(crit, "no_fire", 0): [False, False, False]},
        raises={case_id(crit, "fire", 0): ValueError("boom")},
    )
    p = admission_paths(tmp_path)

    with pytest.raises(ValueError):
        run_admission(
            manifest,
            material_index=_material(),
            solver=solver,
            out_dir=p["out_dir"],
            drift_path=p["drift_path"],
            ledger_path=p["ledger_path"],
            cap_usd=250.0,
            reserve_usd=0.0,
            epochs=3,
            packaged_ids=frozenset(),
            tier_for=lambda _c: CHEAP,
        )
    # Atomic write ⇒ no partial spec, and no stray temp .eval.yaml, for the aborted criterion.
    assert list(p["out_dir"].glob("*.eval.yaml")) == [] if p["out_dir"].exists() else True


# ── AC8: crash-consistent resume ─────────────────────────────────────────────────────────
def test_resume_recharges_no_finalized_criterion(tmp_path, monkeypatch):
    stub_genai_prices(monkeypatch, usd_per_row=0.01)
    crit = "project.alpha"
    manifest = _balanced_manifest(crit, tmp_path)
    p = admission_paths(tmp_path)
    common = dict(
        material_index=_material(),
        out_dir=p["out_dir"],
        drift_path=p["drift_path"],
        ledger_path=p["ledger_path"],
        cap_usd=250.0,
        reserve_usd=0.0,
        epochs=3,
        packaged_ids=frozenset(),
        tier_for=lambda _c: CHEAP,
    )
    script = {
        case_id(crit, "fire", 0): [True, True, True],
        case_id(crit, "no_fire", 0): [False, False, False],
    }
    run_admission(manifest, solver=ScriptedSolver(dict(script)), **common)
    rows_after_first = ledger._read_ledger(str(p["ledger_path"]))
    assert len(rows_after_first) == 1

    # Re-run: the criterion is already finalized (its run_id is in the ledger) → no solver call,
    # no new ledger row.
    resume_solver = ScriptedSolver(dict(script))
    run_admission(manifest, solver=resume_solver, **common)
    assert resume_solver.calls == []
    assert ledger._read_ledger(str(p["ledger_path"])) == rows_after_first


def test_resume_rederives_spec_orphan_with_no_ledger_row(tmp_path, monkeypatch):
    stub_genai_prices(monkeypatch, usd_per_row=0.01)
    crit = "project.alpha"
    manifest = _balanced_manifest(crit, tmp_path)
    p = admission_paths(tmp_path)
    # Simulate a crash BETWEEN the atomic spec write and the ledger finalize: a spec file exists
    # but the ledger has no row for it.
    p["out_dir"].mkdir(parents=True)
    spec_path = p["out_dir"] / f"{criterion_prompt_id(crit)}.eval.yaml"
    spec_path.write_text("orphan: true\n", encoding="utf-8")
    assert ledger._read_ledger(str(p["ledger_path"])) == []

    solver = ScriptedSolver(
        {
            case_id(crit, "fire", 0): [True, True, True],
            case_id(crit, "no_fire", 0): [False, False, False],
        }
    )
    summary = run_admission(
        manifest,
        material_index=_material(),
        solver=solver,
        out_dir=p["out_dir"],
        drift_path=p["drift_path"],
        ledger_path=p["ledger_path"],
        cap_usd=250.0,
        reserve_usd=0.0,
        epochs=3,
        packaged_ids=frozenset(),
        tier_for=lambda _c: CHEAP,
    )
    # The orphan is re-derived: the criterion is solved, the spec overwritten with a valid one,
    # and exactly one ledger row now records it.
    assert crit in summary.admitted
    assert solver.calls, "orphan spec (no ledger row) must be re-derived, not skipped"
    entries = ledger._read_ledger(str(p["ledger_path"]))
    assert len(entries) == 1 and entries[0]["run_id"] == f"admission-{criterion_prompt_id(crit)}"
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    assert validate_eval_spec(spec, strict=True) == []


# ── AC9: transient error → retry 3×, then leave the criterion incomplete and continue ────
def test_transient_error_retried_then_criterion_incomplete_run_continues(tmp_path, monkeypatch):
    stub_genai_prices(monkeypatch, usd_per_row=0.01)
    bad, good = "project.aaa", "project.zzz"
    rows = [
        candidate(bad, "fire", 0, "b-fire"),
        candidate(bad, "no_fire", 0, "b-pass"),
        candidate(good, "fire", 0, "g-fire"),
        candidate(good, "no_fire", 0, "g-pass"),
    ]
    manifest = write_manifest_file(rows, tmp_path)
    material = {
        "b-fire": sidecar_row("b-fire", ticket_id="b1", description="B FIRE"),
        "b-pass": sidecar_row("b-pass", ticket_id="b2", description="B PASS"),
        "g-fire": sidecar_row("g-fire", ticket_id="g1", description="G FIRE"),
        "g-pass": sidecar_row("g-pass", ticket_id="g2", description="G PASS"),
    }

    # A solver that raises a transient error on every call for the bad criterion's fire case,
    # and behaves normally for the good criterion. Counts attempts on the failing case.
    class RetryCountingSolver(ScriptedSolver):
        def __init__(self):
            super().__init__(
                {
                    case_id(good, "fire", 0): [True, True, True],
                    case_id(good, "no_fire", 0): [False, False, False],
                    case_id(bad, "no_fire", 0): [False, False, False],
                }
            )
            self.bad_attempts = 0

        def __call__(self, prompt_id, case):
            if case["id"] == case_id(bad, "fire", 0):
                self.bad_attempts += 1
                raise TransientSolverError("transport blip")
            return super().__call__(prompt_id, case)

    solver = RetryCountingSolver()
    p = admission_paths(tmp_path)
    sleeps: list[float] = []

    summary = run_admission(
        manifest,
        material_index=material,
        solver=solver,
        out_dir=p["out_dir"],
        drift_path=p["drift_path"],
        ledger_path=p["ledger_path"],
        cap_usd=250.0,
        reserve_usd=0.0,
        epochs=3,
        packaged_ids=frozenset(),
        tier_for=lambda _c: CHEAP,
        sleep=sleeps.append,
    )

    # The transient case was attempted 3× (retry budget) then the criterion left incomplete:
    # no spec, no ledger row — while the run CONTINUED to the good criterion and admitted it.
    assert solver.bad_attempts == 3
    assert sleeps == [1.0, 2.0]  # backoff between the 3 attempts (no sleep after the last)
    assert bad in summary.incomplete
    assert bad not in summary.admitted
    assert not (p["out_dir"] / f"{criterion_prompt_id(bad)}.eval.yaml").exists()
    assert all(
        e["run_id"] != f"admission-{criterion_prompt_id(bad)}"
        for e in ledger._read_ledger(str(p["ledger_path"]))
    )
    assert good in summary.admitted
    assert (p["out_dir"] / f"{criterion_prompt_id(good)}.eval.yaml").exists()


# ── drift-report persistence across a resume run ─────────────────────────────────────────
def _multi_material(**descriptions: str) -> dict[str, dict]:
    return {
        uuid: sidecar_row(uuid, ticket_id=f"t-{uuid}", description=desc)
        for uuid, desc in descriptions.items()
    }


def test_resume_preserves_prior_drift_report_of_finalized_criterion(tmp_path, monkeypatch):
    """A resume run must NOT clobber the drift a prior run recorded for a criterion that is now
    finalized-and-skipped. Run 1 admits ``project.aaa`` WITH a drift entry (one of its fire
    candidates did not reproduce) and withholds ``project.bbb`` (also drifting). Run 2 skips the
    finalized ``project.aaa`` but re-processes ``project.bbb`` — the persisted drift report must
    still carry ``project.aaa``'s drift, not just run 2's freshly-computed ``project.bbb`` rows."""
    stub_genai_prices(monkeypatch, usd_per_row=0.01)
    a, b = "project.aaa", "project.bbb"
    rows = [
        candidate(a, "fire", 0, "a-fire0"),
        candidate(a, "fire", 1, "a-fire1"),
        candidate(a, "no_fire", 0, "a-pass0"),
        candidate(b, "fire", 0, "b-fire0"),
        candidate(b, "no_fire", 0, "b-pass0"),
    ]
    manifest = write_manifest_file(rows, tmp_path)
    material = _multi_material(
        **{
            "a-fire0": "A FIRE ZERO",
            "a-fire1": "A FIRE ONE",
            "a-pass0": "A PASS ZERO",
            "b-fire0": "B FIRE ZERO",
            "b-pass0": "B PASS ZERO",
        }
    )
    script = {
        case_id(a, "fire", 0): [True, True, False],  # majority finding → reproduces
        case_id(a, "fire", 1): [True, False, False],  # majority pass ≠ finding → drift
        case_id(a, "no_fire", 0): [False, False, True],  # reproduces
        case_id(b, "fire", 0): [False, False, True],  # majority pass ≠ finding → drift
        case_id(b, "no_fire", 0): [False, False, True],  # reproduces
    }
    p = admission_paths(tmp_path)
    common = dict(
        material_index=material,
        out_dir=p["out_dir"],
        drift_path=p["drift_path"],
        ledger_path=p["ledger_path"],
        cap_usd=250.0,
        reserve_usd=0.0,
        epochs=3,
        packaged_ids=frozenset(),
        tier_for=lambda _c: CHEAP,
    )

    first = run_admission(manifest, solver=ScriptedSolver(dict(script)), **common)
    assert a in first.admitted and b in first.withheld
    report_1 = p["drift_path"].read_text(encoding="utf-8")
    # Run 1's report carries BOTH criteria's drift.
    assert "a-fire1" in report_1 and "b-fire0" in report_1

    resume_solver = ScriptedSolver(dict(script))
    second = run_admission(manifest, solver=resume_solver, **common)
    # project.aaa is finalized → skipped this run (its fire cases are never re-solved).
    assert a in second.admitted  # reported as already-admitted
    assert case_id(a, "fire", 0) not in resume_solver.case_ids()
    report_2 = p["drift_path"].read_text(encoding="utf-8")
    # The blocking bug: run 2 rewrites the whole report with only its own drift, dropping the
    # finalized criterion's prior drift. It must be preserved.
    assert "a-fire1" in report_2, "resume must preserve the finalized criterion's prior drift"
    assert "b-fire0" in report_2


def test_zero_drift_run_clears_stale_drift_report(tmp_path, monkeypatch):
    """A run that produces no drift must leave a CONSISTENT report on disk — a pre-existing
    stale report is not left untouched. (A clean admission of a balanced criterion.)"""
    stub_genai_prices(monkeypatch, usd_per_row=0.01)
    crit = "project.alpha"
    manifest = _balanced_manifest(crit, tmp_path)
    p = admission_paths(tmp_path)
    p["drift_path"].parent.mkdir(parents=True, exist_ok=True)
    p["drift_path"].write_text("STALE-BOGUS-DRIFT-MARKER row\n", encoding="utf-8")
    solver = ScriptedSolver(
        {
            case_id(crit, "fire", 0): [True, True, True],
            case_id(crit, "no_fire", 0): [False, False, False],
        }
    )

    summary = run_admission(
        manifest,
        material_index=_material(),
        solver=solver,
        out_dir=p["out_dir"],
        drift_path=p["drift_path"],
        ledger_path=p["ledger_path"],
        cap_usd=250.0,
        reserve_usd=0.0,
        epochs=3,
        packaged_ids=frozenset(),
        tier_for=lambda _c: CHEAP,
    )

    assert crit in summary.admitted and not summary.drift
    report = p["drift_path"].read_text(encoding="utf-8")
    assert "STALE-BOGUS-DRIFT-MARKER" not in report


def test_missing_registry_tier_defaults_not_keyerror(tmp_path, monkeypatch):
    """With the DEFAULT tier resolver (``tier_for=None``), a manifest criterion that is absent
    from the plan-review registry must not abort the run with a KeyError — it defaults to the
    cheap tier and is processed."""
    stub_genai_prices(monkeypatch, usd_per_row=0.01)
    crit = "project.unregistered-zzz-criterion"
    manifest = _balanced_manifest(crit, tmp_path)
    solver = ScriptedSolver(
        {
            case_id(crit, "fire", 0): [True, True, True],
            case_id(crit, "no_fire", 0): [False, False, False],
        }
    )
    p = admission_paths(tmp_path)

    summary = run_admission(
        manifest,
        material_index=_material(),
        solver=solver,
        out_dir=p["out_dir"],
        drift_path=p["drift_path"],
        ledger_path=p["ledger_path"],
        cap_usd=250.0,
        reserve_usd=0.0,
        epochs=3,
        packaged_ids=frozenset(),
        tier_for=None,
        repo_root=None,
    )

    assert crit in summary.admitted


def test_duplicate_rank_invalid_spec_is_withheld_to_drift(tmp_path, monkeypatch):
    """A balanced reproducing set whose emitted spec fails strict validation (here: two fire
    candidates at the SAME rank mint a duplicate case id) is withheld — no spec is written and
    the reproducing cases are recorded as ``invalid-spec`` drift."""
    stub_genai_prices(monkeypatch, usd_per_row=0.01)
    crit = "project.alpha"
    rows = [
        candidate(crit, "fire", 0, "f-a"),
        candidate(crit, "fire", 0, "f-b"),  # duplicate rank → duplicate case id
        candidate(crit, "no_fire", 0, "p-a"),
    ]
    manifest = write_manifest_file(rows, tmp_path)
    material = _multi_material(**{"f-a": "FIRE A", "f-b": "FIRE B", "p-a": "PASS A"})
    solver = ScriptedSolver(
        {
            case_id(crit, "fire", 0): [True, True, True, True, True, True],  # both fire cases
            case_id(crit, "no_fire", 0): [False, False, False],
        }
    )
    p = admission_paths(tmp_path)

    summary = run_admission(
        manifest,
        material_index=material,
        solver=solver,
        out_dir=p["out_dir"],
        drift_path=p["drift_path"],
        ledger_path=p["ledger_path"],
        cap_usd=250.0,
        reserve_usd=0.0,
        epochs=3,
        packaged_ids=frozenset(),
        tier_for=lambda _c: CHEAP,
    )

    assert crit in summary.withheld and crit not in summary.admitted
    assert not (p["out_dir"] / f"{criterion_prompt_id(crit)}.eval.yaml").exists()
    assert {d.reason for d in summary.drift} == {"invalid-spec"}


def test_epochs_below_one_raises_value_error(tmp_path):
    """The ``epochs < 1`` guard rejects a degenerate majority window before any work."""
    crit = "project.alpha"
    manifest = _balanced_manifest(crit, tmp_path)
    p = admission_paths(tmp_path)
    with pytest.raises(ValueError):
        run_admission(
            manifest,
            material_index=_material(),
            solver=ScriptedSolver({}),
            out_dir=p["out_dir"],
            drift_path=p["drift_path"],
            ledger_path=p["ledger_path"],
            cap_usd=250.0,
            reserve_usd=0.0,
            epochs=0,
            packaged_ids=frozenset(),
            tier_for=lambda _c: CHEAP,
        )


# ── contract sanity: CaseOutcome shape ───────────────────────────────────────────────────
def test_case_outcome_is_fired_plus_usage_rows():
    o = CaseOutcome(fired=True, usage_rows=[usage_row()])
    assert o.fired is True and o.usage_rows[0]["model"] == "bedrock:m"
