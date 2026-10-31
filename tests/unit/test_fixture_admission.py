"""Visible admission-runner happy path and shared scaffolding (67aa).

For balanced candidates, resolve verified sidecar material, rehydrate the
finder input from its description rather than emitter provenance, and run
Pass-1 across epochs. Majority reproduction admits one strict-valid
.eval.yaml with runnable material. The held-out suite covers drift,
budgets, skip dispositions, atomicity, resume, and retry.

Held-out oracles merged from test_fixture_admission_heldout.py: Withheld tests cover non-
reproduction and unrecoverable-material drift, budget and recorded-spend halt, packaged/ISF skips,
atomicity after unbalanced or raised work, crash resume, and transient retry. Share scaffolding
with the visible admission suite.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest
import yaml

from rebar.llm.criteria.ids import criterion_prompt_id
from rebar.llm.evals.eval import validate_eval_spec
from rebar.llm.evals.fixture_admission import CaseOutcome, TransientSolverError, run_admission
from rebar.llm.evals.fixture_selection import write_manifest
from rebar.llm.evals.plan_replay import ledger

pytestmark = pytest.mark.unit

CHEAP = "criteria-eval-cheap"


# ── shared scaffolding (the runner's injectable seams) ────────────────────────────────
def candidate(
    criterion: str,
    direction: str,
    rank: int,
    review_event_uuid: str,
    *,
    norm_id: str | None = None,
    tier: str = "advisory",
    signals: list[str] | None = None,
) -> dict[str, Any]:
    """A ``candidate`` manifest row in the selector's emitted shape (ticket 549b)."""
    return {
        "kind": "candidate",
        "criterion": criterion,
        "direction": direction,
        "norm_id": norm_id
        if norm_id is not None
        else (f"n-{rank}" if direction == "fire" else None),
        "tier": tier,
        "rank": rank,
        "signals": sorted(signals or ["reproduction_consensus"]),
        "escaped_defect": False,
        "abs_margin": None,
        "review_event_uuid": review_event_uuid,
    }


def sidecar_row(
    review_event_uuid: str,
    *,
    ticket_id: str,
    description: str,
    children: list[dict] | None = None,
    children_material: list[dict] | None = None,
    verified: bool = True,
) -> dict[str, Any]:
    """A verified plan-review sidecar corpus row (``corpus.py:_build_sidecar_row`` shape):
    the at-review material keyed by ``review_event_uuid`` that the runner rehydrates from."""
    return {
        "review_event_uuid": review_event_uuid,
        "ticket_id": ticket_id,
        "description": description,
        "children": children or [],
        "children_material": children_material or [],
        "ticket_type": "task",
        "file_impact": [],
        "verified": verified,
    }


def write_manifest_file(rows: list[dict[str, Any]], tmp_path: Path) -> Path:
    path = tmp_path / "manifest.jsonl"
    write_manifest(rows, path)
    return path


def case_id(criterion: str, direction: str, rank: int) -> str:
    """The dataset case id the runner mints — identical to the emitter's convention."""
    return f"{criterion_prompt_id(criterion)}-{direction}-{rank}"


def usage_row(model: str = "bedrock:m", input_tokens: int = 1000, output_tokens: int = 500) -> dict:
    return {
        "model": model,
        "provider": "bedrock",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "timestamp": "2026-07-30T00:00:00+00:00",
    }


class ScriptedSolver:
    """A model-free solver seam. ``script`` maps a dataset case id to the per-epoch fired
    booleans consumed one-per-call; ``raises`` maps a case id to an exception raised on every
    call. Records every ``(prompt_id, case)`` it receives for input/no-call assertions."""

    def __init__(
        self,
        script: dict[str, list[bool]] | None = None,
        *,
        raises: dict[str, BaseException] | None = None,
        row: dict | None = None,
    ) -> None:
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.raises = dict(raises or {})
        self.row = row or usage_row()
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, prompt_id: str, case: dict) -> CaseOutcome:
        self.calls.append((prompt_id, dict(case)))
        cid = case["id"]
        if cid in self.raises:
            raise self.raises[cid]
        fired = self.script[cid].pop(0)
        return CaseOutcome(fired=fired, usage_rows=[dict(self.row)])

    def case_ids(self) -> list[str]:
        return [case["id"] for _pid, case in self.calls]

    def inputs_for(self, cid: str) -> list[str]:
        return [case.get("input") for _pid, case in self.calls if case["id"] == cid]


def stub_genai_prices(monkeypatch: pytest.MonkeyPatch, usd_per_row: float) -> None:
    """Install a fake ``genai_prices`` returning a fixed price per row, so ``ledger.finalize``
    prices deterministically offline (mirrors ``test_plan_replay_ledger`` pattern)."""
    stub = types.ModuleType("genai_prices")

    class Usage:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    class _Price:
        def __init__(self, total_price: float) -> None:
            self.total_price = total_price

    def calc_price(
        usage: Any, model_ref: Any, provider_id: Any = None, genai_request_timestamp: Any = None
    ) -> _Price:
        return _Price(usd_per_row)

    stub.Usage = Usage
    stub.calc_price = calc_price
    monkeypatch.setitem(sys.modules, "genai_prices", stub)


def admission_paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "out_dir": tmp_path / "evals",
        "drift_path": tmp_path / "drift-report.md",
        "ledger_path": tmp_path / "ledger.jsonl",
    }


# ── happy path ────────────────────────────────────────────────────────────────────────
def test_reproducing_criterion_admits_spec_over_rehydrated_material(tmp_path, monkeypatch):
    """A balanced criterion whose candidates each resolve to a verified sidecar row: the
    runner rehydrates the finder input from each row's ``description`` (not the descriptor),
    and with both cases reproducing their predicted direction across a 2-of-3 majority, admits
    exactly one strict-valid spec whose dataset carries the rehydrated material."""
    stub_genai_prices(monkeypatch, usd_per_row=0.01)
    crit = "project.alpha"
    fire_plan = "PLAN whose material must fire the criterion — rehydrated from history."
    pass_plan = "PLAN whose material must stay silent — a clean, well-formed decomposition."
    rows = [
        candidate(crit, "fire", 0, "uuid-fire"),
        candidate(crit, "no_fire", 0, "uuid-pass"),
    ]
    manifest = write_manifest_file(rows, tmp_path)
    material = {
        "uuid-fire": sidecar_row("uuid-fire", ticket_id="t-fire", description=fire_plan),
        "uuid-pass": sidecar_row("uuid-pass", ticket_id="t-pass", description=pass_plan),
    }
    solver = ScriptedSolver(
        {
            case_id(crit, "fire", 0): [
                True,
                True,
                False,
            ],  # majority fire → reproduces (expect finding)
            case_id(crit, "no_fire", 0): [
                False,
                False,
                True,
            ],  # majority silent → reproduces (expect pass)
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

    assert summary.admitted == [crit]
    assert summary.withheld == []
    assert not summary.drift

    spec_path = p["out_dir"] / f"{criterion_prompt_id(crit)}.eval.yaml"
    assert spec_path.exists()
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    assert validate_eval_spec(spec, strict=True) == []
    assert (
        spec["epochs"] == 3 and spec["gate"] == "at_least(2)" and spec["coverage_threshold"] == 1.0
    )

    # The finder ran on the REHYDRATED description, never the emitter's provenance descriptor.
    assert solver.inputs_for(case_id(crit, "fire", 0)) == [fire_plan, fire_plan, fire_plan]
    inputs = {case["input"] for case in spec["dataset"]}
    assert inputs == {fire_plan, pass_plan}
    labels = {case["expect"] for case in spec["dataset"]}
    assert labels == {"finding", "pass"}
    # gold_set is 1:1 with the reproduced dataset over the runnable material.
    assert {g["input"] for g in spec["gold_set"]} == {fire_plan, pass_plan}


# ── container-criterion child shape (regression: bare-string corpus children) ───────────
def test_container_criterion_admits_with_rehydrated_child_material(tmp_path, monkeypatch):
    """A container criterion (G3/G4/decomp-shape) is scored over a parent plus faithful
    per-child title/description. Once the corpus row carries that material, admission must
    dispatch the container finder and persist the child payload in the admitted eval spec."""
    stub_genai_prices(monkeypatch, usd_per_row=0.01)
    crit = "G3"
    good = "project.alpha"  # sorts AFTER "G3", so scoping-out must not starve it
    rows = [
        candidate(crit, "fire", 0, "uuid-c-fire"),
        candidate(crit, "no_fire", 0, "uuid-c-pass"),
        candidate(good, "fire", 0, "uuid-fire"),
        candidate(good, "no_fire", 0, "uuid-pass"),
    ]
    manifest = write_manifest_file(rows, tmp_path)
    child_fire = {
        "ticket_id": "child-a",
        "canonical_id": "child-a",
        "title": "Child A",
        "description": "## Acceptance Criteria\n- [ ] covers A",
    }
    child_pass = {
        "ticket_id": "child-c",
        "canonical_id": "child-c",
        "title": "Child C",
        "description": "## Acceptance Criteria\n- [ ] covers C",
    }
    material = {
        "uuid-c-fire": sidecar_row(
            "uuid-c-fire",
            ticket_id="t-cf",
            description="parent plan A",
            children=["child-a"],
            children_material=[child_fire],
        ),
        "uuid-c-pass": sidecar_row(
            "uuid-c-pass",
            ticket_id="t-cp",
            description="parent plan B",
            children=["child-c"],
            children_material=[child_pass],
        ),
        "uuid-fire": sidecar_row("uuid-fire", ticket_id="t-fire", description="fire plan"),
        "uuid-pass": sidecar_row("uuid-pass", ticket_id="t-pass", description="pass plan"),
    }
    solver = ScriptedSolver(
        {
            case_id(crit, "fire", 0): [True, True, False],
            case_id(crit, "no_fire", 0): [False, False, True],
            case_id(good, "fire", 0): [True, True, False],
            case_id(good, "no_fire", 0): [False, False, True],
        },
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

    assert summary.admitted == [crit, good]
    assert case_id(crit, "fire", 0) in solver.case_ids()
    assert case_id(crit, "no_fire", 0) in solver.case_ids()
    spec = yaml.safe_load(
        (p["out_dir"] / f"{criterion_prompt_id(crit)}.eval.yaml").read_text(encoding="utf-8")
    )
    children_by_input = {case["input"]: case["children"] for case in spec["dataset"]}
    assert children_by_input["parent plan A"] == [child_fire]
    assert children_by_input["parent plan B"] == [child_pass]
    ledger_text = p["ledger_path"].read_text(encoding="utf-8") if p["ledger_path"].exists() else ""
    assert f"admission-{criterion_prompt_id(crit)}" in ledger_text
    assert not [d for d in summary.drift if d.criterion == crit]


def test_container_criterion_with_legacy_child_strings_is_unrecoverable(tmp_path, monkeypatch):
    """Legacy rows with child IDs but no saved titles or descriptions cannot
    faithfully run G3/G4. Withhold them as unrecoverable."""
    stub_genai_prices(monkeypatch, usd_per_row=0.01)
    crit = "G3"
    rows = [
        candidate(crit, "fire", 0, "uuid-c-fire"),
        candidate(crit, "no_fire", 0, "uuid-c-pass"),
    ]
    manifest = write_manifest_file(rows, tmp_path)
    material = {
        "uuid-c-fire": sidecar_row(
            "uuid-c-fire", ticket_id="t-cf", description="parent plan A", children=["child-a"]
        ),
        "uuid-c-pass": sidecar_row(
            "uuid-c-pass", ticket_id="t-cp", description="parent plan B", children=["child-c"]
        ),
    }
    solver = ScriptedSolver()
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

    assert solver.calls == []
    assert crit in summary.withheld and crit not in summary.admitted
    assert not (p["out_dir"] / f"{criterion_prompt_id(crit)}.eval.yaml").exists()
    assert {d.reason for d in summary.drift} == {"unrecoverable-material"}


# ── inline-unadmissible criterion (regression: ISF finder crashes the run) ──────────────
def test_isf_criterion_is_skipped_not_dispatched(tmp_path, monkeypatch):
    """Skip inline-unadmissible ISF criteria before eval_solver, which needs a
    session log. Write no spec or ledger row, report
    not-inline-admissible drift, and keep processing other criteria."""
    from rebar.llm.evals import eval_solver

    stub_genai_prices(monkeypatch, usd_per_row=0.01)
    isf = "ISF"
    good = "project.alpha"  # sorts AFTER "ISF", so a crash on ISF would starve it
    assert isf in eval_solver.INLINE_UNADMISSIBLE_CRITERIA
    fire_plan = "PLAN whose material must fire the criterion — rehydrated from history."
    pass_plan = "PLAN whose material must stay silent — a clean, well-formed decomposition."
    rows = [
        candidate(isf, "fire", 0, "uuid-isf-fire"),
        candidate(isf, "no_fire", 0, "uuid-isf-pass"),
        candidate(good, "fire", 0, "uuid-fire"),
        candidate(good, "no_fire", 0, "uuid-pass"),
    ]
    manifest = write_manifest_file(rows, tmp_path)
    material = {
        "uuid-isf-fire": sidecar_row("uuid-isf-fire", ticket_id="t-i1", description="isf plan A"),
        "uuid-isf-pass": sidecar_row("uuid-isf-pass", ticket_id="t-i2", description="isf plan B"),
        "uuid-fire": sidecar_row("uuid-fire", ticket_id="t-fire", description=fire_plan),
        "uuid-pass": sidecar_row("uuid-pass", ticket_id="t-pass", description=pass_plan),
    }
    # The solver RAISES the exact production error if ever handed an ISF case — proving the run
    # would crash without the skip; the good criterion is scripted to reproduce and admit.
    isf_boom = ValueError(
        "criterion 'ISF' is an ISF finder (needs a session log), "
        "not runnable over an inline eval fixture"
    )
    solver = ScriptedSolver(
        {
            case_id(good, "fire", 0): [True, True, False],
            case_id(good, "no_fire", 0): [False, False, True],
        },
        raises={
            case_id(isf, "fire", 0): isf_boom,
            case_id(isf, "no_fire", 0): isf_boom,
        },
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

    # ISF was never dispatched to the solver — the run did not crash.
    assert all(not cid.startswith(criterion_prompt_id(isf)) for cid in solver.case_ids())
    # ISF admitted nothing; the good criterion after it in sort order still admitted.
    assert isf not in summary.admitted
    assert summary.admitted == [good]
    # No ISF spec, no ISF ledger row.
    assert not (p["out_dir"] / f"{criterion_prompt_id(isf)}.eval.yaml").exists()
    ledger_text = p["ledger_path"].read_text(encoding="utf-8") if p["ledger_path"].exists() else ""
    assert f"admission-{criterion_prompt_id(isf)}" not in ledger_text
    # ISF is recorded in the drift report as not-inline-admissible.
    isf_drift = [d for d in summary.drift if d.criterion == isf]
    assert isf_drift and all(d.reason == "not-inline-admissible" for d in isf_drift)
    assert "not-inline-admissible" in p["drift_path"].read_text(encoding="utf-8")


def test_isf_skip_row_is_not_duplicated_across_a_resume_run(tmp_path, monkeypatch):
    """The ISF skip marks the criterion ``processed`` so the drift-report MERGE on a resume run
    REPLACES its prior ``not-inline-admissible`` row instead of appending a second one. Run the
    admission over the same paths twice (a resume): the ISF row must appear EXACTLY once in the
    persisted drift report, not accumulate one per run."""
    from rebar.llm.evals import eval_solver

    stub_genai_prices(monkeypatch, usd_per_row=0.01)
    isf = "ISF"
    good = "project.alpha"
    assert isf in eval_solver.INLINE_UNADMISSIBLE_CRITERIA
    fire_plan = "PLAN whose material must fire the criterion — rehydrated from history."
    pass_plan = "PLAN whose material must stay silent — a clean, well-formed decomposition."
    rows = [
        candidate(isf, "fire", 0, "uuid-isf-fire"),
        candidate(isf, "no_fire", 0, "uuid-isf-pass"),
        candidate(good, "fire", 0, "uuid-fire"),
        candidate(good, "no_fire", 0, "uuid-pass"),
    ]
    manifest = write_manifest_file(rows, tmp_path)
    material = {
        "uuid-isf-fire": sidecar_row("uuid-isf-fire", ticket_id="t-i1", description="isf plan A"),
        "uuid-isf-pass": sidecar_row("uuid-isf-pass", ticket_id="t-i2", description="isf plan B"),
        "uuid-fire": sidecar_row("uuid-fire", ticket_id="t-fire", description=fire_plan),
        "uuid-pass": sidecar_row("uuid-pass", ticket_id="t-pass", description=pass_plan),
    }
    p = admission_paths(tmp_path)

    def run(solver: ScriptedSolver) -> None:
        run_admission(
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

    script = {
        case_id(good, "fire", 0): [True, True, False],
        case_id(good, "no_fire", 0): [False, False, True],
    }
    run(ScriptedSolver(dict((k, list(v)) for k, v in script.items())))
    # Resume: `good` is now finalized (ledger row) and skipped; ISF is re-skipped.
    run(ScriptedSolver())

    report = p["drift_path"].read_text(encoding="utf-8")
    isf_rows = [
        line
        for line in report.splitlines()
        if line.startswith("| ") and line[2:].split(" | ", 1)[0] == isf
    ]
    assert len(isf_rows) == 1, f"ISF drift row duplicated across resume: {isf_rows}"


# ── held-out oracles (merged from test_fixture_admission_heldout.py) ──


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
