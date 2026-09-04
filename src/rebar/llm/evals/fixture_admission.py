"""Admission runner for mined plan-review fixture candidates."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from rebar.llm.criteria.ids import criterion_prompt_id
from rebar.llm.evals import fixture_emit
from rebar.llm.evals.eval import validate_eval_spec
from rebar.llm.evals.plan_replay import ledger


class TransientSolverError(Exception):
    """A retryable model/transport error raised by a solver; the runner retries then continues."""


@dataclass(frozen=True)
class CaseOutcome:
    """One finder run's observable result: whether it fired, plus billable usage rows."""

    fired: bool
    usage_rows: list[dict]


@dataclass(frozen=True)
class DriftEntry:
    """A non-reproducing (or unrecoverable-material) case withheld from admission."""

    criterion: str
    case_id: str
    direction: str
    predicted: str
    observed: str
    reason: str
    ticket_id: str | None
    review_event_uuid: str


@dataclass(frozen=True)
class AdmissionSummary:
    """In-memory result of an admission run (the runner writes only specs + the drift report)."""

    admitted: list[str] = field(default_factory=list)
    withheld: list[str] = field(default_factory=list)
    drift: list[DriftEntry] = field(default_factory=list)
    spend_usd: float = 0.0
    incomplete: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _RehydratedCase:
    case: dict[str, Any]
    direction: str
    ticket_id: str | None
    review_event_uuid: str


class _IncompleteCriterion(Exception):
    """A criterion could not finish because all transient retries were exhausted."""


def run_admission(
    manifest_path: str | Path,
    *,
    material_index: Mapping[str, dict],
    solver: Callable[[str, dict], CaseOutcome],
    out_dir: str | Path,
    drift_path: str | Path,
    ledger_path: str | Path,
    cap_usd: float = 250.0,
    reserve_usd: float = 0.0,
    epochs: int = 3,
    packaged_ids: frozenset[str] | None = None,
    tier_for: Callable[[str], str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    repo_root: str | None = None,
    max_retries: int = 3,
) -> AdmissionSummary:
    if epochs < 1:
        raise ValueError("epochs must be >= 1")

    from rebar.llm.evals import eval_solver
    from rebar.llm.plan_review.container_stage import CONTAINER_CRITERIA

    container_criteria = frozenset(CONTAINER_CRITERIA)

    manifest = Path(manifest_path)
    out = Path(out_dir)
    drift = Path(drift_path)
    ledger_file = Path(ledger_path)
    packaged = _packaged_criterion_ids() if packaged_ids is None else set(packaged_ids)
    criteria, candidates = fixture_emit._read_manifest(manifest)
    ledger_run_ids = _ledger_run_ids(ledger_file)

    admitted: list[str] = []
    withheld: list[str] = []
    incomplete: list[str] = []
    drift_entries: list[DriftEntry] = []
    processed: set[str] = set()

    for criterion in sorted(criteria):
        prompt_id = criterion_prompt_id(criterion)
        if criterion in packaged or prompt_id in packaged:
            continue
        if criterion in eval_solver.INLINE_UNADMISSIBLE_CRITERIA:
            # Not scorable over an inline sidecar-replay fixture (an ISF finder needs a real
            # session log, so the solver can only raise). Skip like a packaged criterion, but
            # record it in the drift report so the run stays auditable instead of silently
            # dropping the candidate or crashing on the first ISF case. Mark it processed so a
            # resume run REPLACES this row rather than appending a duplicate.
            processed.add(criterion)
            drift_entries.append(
                DriftEntry(
                    criterion=criterion,
                    case_id=prompt_id,
                    direction="",
                    predicted="",
                    observed="skipped",
                    reason="not-inline-admissible",
                    ticket_id=None,
                    review_event_uuid="",
                )
            )
            continue
        if criterion in container_criteria:
            # A container criterion (G3/G4/decomp-shape) is scored over a (parent, children,
            # roster) decomposition whose rubric reads each child's LIVE title/description
            # (G3's coverage discharge cannot fire on a title-only roster). The sidecar corpus
            # persists children as bare ticket-id STRINGS only (corpus._child_ids), with no
            # per-child material, so a rehydrated container case would run the finder over an
            # IMPOVERISHED roster — an admit/drift verdict UNFAITHFUL to the historical review
            # (whose finder saw full child state via context_assembly.show_ticket). Until the
            # corpus captures per-child title/description, scope container criteria OUT the way
            # ISF/packaged ones are skipped: never dispatch, admit nothing, and record it as
            # container-material-unrecoverable so the run stays auditable. Mark it processed so
            # a resume run REPLACES this row rather than appending a duplicate.
            processed.add(criterion)
            drift_entries.append(
                DriftEntry(
                    criterion=criterion,
                    case_id=prompt_id,
                    direction="",
                    predicted="",
                    observed="skipped",
                    reason="container-material-unrecoverable",
                    ticket_id=None,
                    review_event_uuid="",
                )
            )
            continue
        if f"admission-{prompt_id}" in ledger_run_ids:
            admitted.append(criterion)
            continue

        processed.add(criterion)
        criterion_drift: list[DriftEntry] = []
        rehydrated = _rehydrate_selected(
            criterion,
            prompt_id,
            candidates.get(criterion, []),
            material_index,
            criterion_drift,
        )
        tier = _tier_for_criterion(criterion, tier_for, repo_root)
        sample_n = len(rehydrated)
        ledger.reserve(
            ledger.estimate(tier, sample_n),
            ledger_path=str(ledger_file),
            cap_usd=cap_usd,
            reserve_usd=reserve_usd,
        )

        usage_rows: list[dict[str, Any]] = []
        reproducing: list[_RehydratedCase] = []
        try:
            for entry in rehydrated:
                observed, rows = _run_case_epochs(
                    prompt_id,
                    entry.case,
                    solver,
                    epochs=epochs,
                    sleep=sleep,
                    max_retries=max_retries,
                )
                usage_rows.extend(rows)
                if observed == entry.case["expect"]:
                    reproducing.append(entry)
                else:
                    criterion_drift.append(
                        _drift_entry(entry, reason="non-reproducing", observed=observed)
                    )
        except _IncompleteCriterion:
            incomplete.append(criterion)
            drift_entries.extend(criterion_drift)
            continue

        if not _is_balanced(reproducing):
            criterion_drift.extend(
                _drift_entry(entry, reason="unbalanced", observed=str(entry.case["expect"]))
                for entry in reproducing
            )
            withheld.append(criterion)
            drift_entries.extend(criterion_drift)
            continue

        spec = _build_spec(prompt_id, epochs, [entry.case for entry in reproducing])
        if validate_eval_spec(spec, strict=True):
            criterion_drift.extend(
                _drift_entry(entry, reason="invalid-spec", observed=str(entry.case["expect"]))
                for entry in reproducing
            )
            withheld.append(criterion)
            drift_entries.extend(criterion_drift)
            continue

        _write_spec_atomic(out / f"{prompt_id}.eval.yaml", spec)
        ledger.finalize(
            f"admission-{prompt_id}",
            tier,
            criterion,
            sample_n,
            {"pass1": _pass1_model(usage_rows)},
            usage_rows,
            ledger_path=str(ledger_file),
        )
        ledger_run_ids.add(f"admission-{prompt_id}")
        admitted.append(criterion)
        drift_entries.extend(criterion_drift)
        if _spent_usd(ledger_file) >= cap_usd:
            break

    _write_drift_report(drift, drift_entries, processed)
    return AdmissionSummary(
        admitted=admitted,
        withheld=withheld,
        drift=drift_entries,
        spend_usd=_spent_usd(ledger_file),
        incomplete=incomplete,
    )


def _packaged_criterion_ids() -> set[str]:
    packaged: set[str] = set()
    spec_dir = Path(__file__).resolve().parent.parent / "eval_specs"
    for path in spec_dir.glob("*.eval.yaml"):
        prompt_id = path.name[: -len(".eval.yaml")]
        packaged.add(prompt_id)
        if prompt_id.startswith("plan-review-project-"):
            packaged.add("project." + prompt_id[len("plan-review-project-") :])
        elif prompt_id.startswith("plan-review-"):
            packaged.add(prompt_id[len("plan-review-") :])
    return packaged


def _ledger_run_ids(path: Path) -> set[str]:
    return {str(row.get("run_id")) for row in ledger._read_ledger(str(path)) if row.get("run_id")}


def _spent_usd(path: Path) -> float:
    return sum(float(row.get("usd", 0.0) or 0.0) for row in ledger._read_ledger(str(path)))


def _tier_for_criterion(
    criterion: str, tier_for: Callable[[str], str] | None, repo_root: str | None
) -> str:
    if tier_for is not None:
        return tier_for(criterion)
    from rebar.llm.plan_review import registry

    desc = registry.by_id(repo_root).get(criterion)
    if desc is None:
        return "criteria-eval-cheap"
    return "criteria-eval-agent" if registry.exec_tier(desc) == "AGENT" else "criteria-eval-cheap"


def _rehydrate_selected(
    criterion: str,
    prompt_id: str,
    rows: list[dict[str, Any]],
    material_index: Mapping[str, dict],
    drift_entries: list[DriftEntry],
) -> list[_RehydratedCase]:
    rehydrated: list[_RehydratedCase] = []
    for row in fixture_emit._selected(rows, "fire") + fixture_emit._selected(rows, "no_fire"):
        entry = _rehydrate_candidate(criterion, prompt_id, row, material_index)
        if isinstance(entry, DriftEntry):
            drift_entries.append(entry)
        else:
            rehydrated.append(entry)
    return rehydrated


def _rehydrate_candidate(
    criterion: str,
    prompt_id: str,
    candidate: dict[str, Any],
    material_index: Mapping[str, dict],
) -> _RehydratedCase | DriftEntry:
    direction = str(candidate["direction"])
    rank = int(candidate["rank"])
    review_event_uuid = str(candidate.get("review_event_uuid") or "")
    expect = "finding" if direction == "fire" else "pass"
    case_id = f"{prompt_id}-{direction}-{rank}"
    material = material_index.get(review_event_uuid)
    if not material or not material.get("verified"):
        return DriftEntry(
            criterion=criterion,
            case_id=case_id,
            direction=direction,
            predicted=expect,
            observed="unavailable",
            reason="unrecoverable-material",
            ticket_id=None,
            review_event_uuid=review_event_uuid,
        )
    case: dict[str, Any] = {
        "id": case_id,
        "corpus": fixture_emit._CORPUS,
        "criterion": criterion,
        "expect": expect,
        "input": str(material.get("description") or ""),
    }
    children = material.get("children")
    if children:
        # The sidecar corpus stores a review's child list as bare ticket-id STRINGS
        # (`corpus._child_ids`), but the container eval path (`pass1_container`,
        # `build_sibling_roster`) consumes `{ticket_id, ...}` DICTS — the same shape
        # `corpus._build_context` builds for the production fingerprint. Normalize here so a
        # container criterion (G3/G4/decomp-shape) rehydrates into a runnable case instead of
        # crashing the finder on the first agent-tier case.
        case["children"] = [c if isinstance(c, dict) else {"ticket_id": c} for c in children]
    ticket_id = material.get("ticket_id")
    return _RehydratedCase(
        case=case,
        direction=direction,
        ticket_id=str(ticket_id) if ticket_id is not None else None,
        review_event_uuid=review_event_uuid,
    )


def _run_case_epochs(
    prompt_id: str,
    case: dict[str, Any],
    solver: Callable[[str, dict], CaseOutcome],
    *,
    epochs: int,
    sleep: Callable[[float], None],
    max_retries: int,
) -> tuple[str, list[dict[str, Any]]]:
    fired = 0
    usage_rows: list[dict[str, Any]] = []
    for _epoch in range(epochs):
        outcome = _run_solver_with_retries(
            prompt_id, case, solver, sleep=sleep, max_retries=max_retries
        )
        fired += int(outcome.fired)
        usage_rows.extend(dict(row) for row in outcome.usage_rows)
    return ("finding" if fired >= _majority_threshold(epochs) else "pass"), usage_rows


def _run_solver_with_retries(
    prompt_id: str,
    case: dict[str, Any],
    solver: Callable[[str, dict], CaseOutcome],
    *,
    sleep: Callable[[float], None],
    max_retries: int,
) -> CaseOutcome:
    attempts = max(1, max_retries)
    for attempt in range(attempts):
        try:
            return solver(prompt_id, case)
        except TransientSolverError as exc:
            if attempt == attempts - 1:
                raise _IncompleteCriterion from exc
            sleep(float(2**attempt))
    raise _IncompleteCriterion


def _majority_threshold(epochs: int) -> int:
    return (epochs // 2) + 1


def _is_balanced(entries: list[_RehydratedCase]) -> bool:
    expects = {str(entry.case.get("expect")) for entry in entries}
    return {"finding", "pass"} <= expects


def _drift_entry(entry: _RehydratedCase, *, reason: str, observed: str) -> DriftEntry:
    return DriftEntry(
        criterion=str(entry.case["criterion"]),
        case_id=str(entry.case["id"]),
        direction=entry.direction,
        predicted=str(entry.case["expect"]),
        observed=observed,
        reason=reason,
        ticket_id=entry.ticket_id,
        review_event_uuid=entry.review_event_uuid,
    )


def _build_spec(prompt_id: str, epochs: int, dataset: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "prompt": prompt_id,
        "model": fixture_emit._MODEL,
        "epochs": epochs,
        "gate": f"at_least({_majority_threshold(epochs)})",
        "coverage_threshold": 1.0,
        "scorers": [fixture_emit._SCORER],
        "dataset": dataset,
        "gold_set": [{"input": case["input"], "label": case["expect"]} for case in dataset],
    }


def _pass1_model(usage_rows: list[dict[str, Any]]) -> str:
    for row in usage_rows:
        model = row.get("model")
        if model:
            return str(model)
    return fixture_emit._MODEL


def _write_spec_atomic(path: Path, spec: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as stream:
            yaml.safe_dump(spec, stream, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


_DRIFT_TITLE = "# Fixture admission drift report"
_DRIFT_COLS = (
    "| criterion | case_id | predicted | observed | reason | ticket_id | review_event_uuid |"
)
_DRIFT_SEP = "| --- | --- | --- | --- | --- | --- | --- |"


def _drift_row(entry: DriftEntry) -> str:
    return (
        "| "
        + " | ".join(
            _md_cell(value)
            for value in (
                entry.criterion,
                entry.case_id,
                entry.predicted,
                entry.observed,
                entry.reason,
                entry.ticket_id or "",
                entry.review_event_uuid,
            )
        )
        + " |"
    )


def _drift_row_criterion(line: str) -> str:
    return line[2:].split(" | ", 1)[0]


def _write_drift_report(path: Path, entries: list[DriftEntry], processed: set[str]) -> None:
    """Render the drift report, MERGING with any existing one so a resume run preserves the
    drift of criteria it skipped. Rows belonging to a criterion processed THIS run are replaced
    by this run's entries; rows for untouched (finalized-and-skipped) criteria are kept. The
    report is always (re)written to a consistent state, even when there is no drift."""
    kept: list[str] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.startswith("| ") or line in (_DRIFT_COLS, _DRIFT_SEP):
                continue
            if _drift_row_criterion(line) not in processed:
                kept.append(line)
    rows = kept + [_drift_row(entry) for entry in entries]
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [_DRIFT_TITLE, "", _DRIFT_COLS, _DRIFT_SEP, *rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _md_cell(value: str) -> str:
    return value.replace("|", r"\|").replace("\n", " ")
