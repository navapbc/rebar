"""Happy-path oracle for the plan-review fixture ADMISSION runner (ticket 67aa).

Pins the CORE observable contract of ``rebar.llm.evals.fixture_admission.run_admission``:
given a labeled candidate manifest that is BALANCED for a criterion, where every candidate's
``review_event_uuid`` resolves to a verified sidecar row carrying the at-review material, the
runner REHYDRATES each case's finder input from that row's ``description`` (NOT the emitter's
model-free provenance descriptor), runs the criterion's Pass-1 finder over the rehydrated
material across ``epochs`` runs, and — when a case's majority verdict REPRODUCES its predicted
direction — admits the criterion by writing exactly one ``.eval.yaml`` whose dataset carries
the runnable rehydrated material and which passes the REAL ``validate_eval_spec(strict=True)``.

The edge cases (drift on non-reproduction / unrecoverable material, budget ceiling + actual
cap halt, no-shadowing skip, unbalanced/mid-raise atomicity, crash-consistent resume, transient
retry) live in the HELD-OUT suite and are validated by the orchestrator — they are NOT here.

The helpers below (``candidate``/``sidecar_row``/``write_manifest_file``/``ScriptedSolver``/
``stub_genai_prices``/``case_id``) are the shared test scaffolding and describe the runner's
injectable seams; the held-out suite imports them.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest
import yaml

from rebar.llm.criteria.ids import criterion_prompt_id
from rebar.llm.evals.eval import validate_eval_spec
from rebar.llm.evals.fixture_admission import CaseOutcome, run_admission
from rebar.llm.evals.fixture_selection import write_manifest

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
    verified: bool = True,
) -> dict[str, Any]:
    """A verified plan-review sidecar corpus row (``corpus.py:_build_sidecar_row`` shape):
    the at-review material keyed by ``review_event_uuid`` that the runner rehydrates from."""
    return {
        "review_event_uuid": review_event_uuid,
        "ticket_id": ticket_id,
        "description": description,
        "children": children or [],
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
def test_container_criterion_is_withheld_unrecoverable(tmp_path, monkeypatch):
    """A container criterion (G3/G4/decomp-shape) is scored over a (parent, children, roster)
    decomposition, and its rubric reads the LIVE per-child ``title``/``description`` (G3's
    coverage discharge cannot fire on a title-only roster — see ``container_stage``). But the
    sidecar corpus persists a review's children as bare ticket-id STRINGS only
    (``corpus._build_sidecar_row`` -> ``_child_ids``), with no per-child title/description. So a
    container case rehydrated from the corpus would run the finder over an IMPOVERISHED roster
    (empty child content) — an admit/drift verdict UNFAITHFUL to the historical review, whose
    finder saw full child state (``context_assembly`` fetches each child via ``show_ticket``).
    Until the corpus captures per-child material, the runner must SCOPE container criteria OUT
    the way it skips ISF/packaged ones: never dispatch, admit nothing, write no spec and no
    ledger row, record it in the drift report as ``container-material-unrecoverable`` — and keep
    processing the remaining criteria."""
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
    material = {
        "uuid-c-fire": sidecar_row(
            "uuid-c-fire", ticket_id="t-cf", description="parent plan A", children=["child-a"]
        ),
        "uuid-c-pass": sidecar_row(
            "uuid-c-pass", ticket_id="t-cp", description="parent plan B", children=["child-c"]
        ),
        "uuid-fire": sidecar_row("uuid-fire", ticket_id="t-fire", description="fire plan"),
        "uuid-pass": sidecar_row("uuid-pass", ticket_id="t-pass", description="pass plan"),
    }
    # The solver RAISES if ever handed a container case — proving it is never dispatched; the
    # good criterion after it in sort order is scripted to reproduce and admit.
    container_boom = AssertionError("container criterion must never be dispatched to the solver")
    solver = ScriptedSolver(
        {
            case_id(good, "fire", 0): [True, True, False],
            case_id(good, "no_fire", 0): [False, False, True],
        },
        raises={
            case_id(crit, "fire", 0): container_boom,
            case_id(crit, "no_fire", 0): container_boom,
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

    # The container criterion was never dispatched to the solver — no unfaithful reproduction.
    assert all(not cid.startswith(criterion_prompt_id(crit)) for cid in solver.case_ids())
    # It admitted nothing; the good criterion after it in sort order still admitted.
    assert crit not in summary.admitted
    assert summary.admitted == [good]
    # No container spec, no container ledger row.
    assert not (p["out_dir"] / f"{criterion_prompt_id(crit)}.eval.yaml").exists()
    ledger_text = p["ledger_path"].read_text(encoding="utf-8") if p["ledger_path"].exists() else ""
    assert f"admission-{criterion_prompt_id(crit)}" not in ledger_text
    # It is recorded in the drift report as container-material-unrecoverable.
    container_drift = [d for d in summary.drift if d.criterion == crit]
    assert container_drift
    assert all(d.reason == "container-material-unrecoverable" for d in container_drift)
    assert "container-material-unrecoverable" in p["drift_path"].read_text(encoding="utf-8")


# ── inline-unadmissible criterion (regression: ISF finder crashes the run) ──────────────
def test_isf_criterion_is_skipped_not_dispatched(tmp_path, monkeypatch):
    """An ISF finder needs a real session log, so ``eval_solver`` RAISES for it over an inline
    fixture. If the runner dispatches an ISF candidate to the solver, that ``ValueError``
    propagates and crashes the WHOLE admission run — losing every not-yet-processed criterion
    (observed live during the 67aa AC10 admission run, after several agent-tier criteria had
    already been admitted). The runner must instead SKIP any criterion in
    ``eval_solver.INLINE_UNADMISSIBLE_CRITERIA`` the way it skips packaged ones: never dispatch
    it, admit nothing for it, write no spec and no ledger row, record it in the drift report as
    ``not-inline-admissible`` — and keep processing the remaining criteria."""
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
