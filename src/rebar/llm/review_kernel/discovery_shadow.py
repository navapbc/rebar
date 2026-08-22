"""RP-06 S7 — the cross-gate discovery SHADOW comparator.

From ONE observed review run this module reconstructs the *legacy* (pre-kernel) discovery
derivation and diffs it, field by field, against the observed *RP-06* kernel derivation. It
reports every divergence EXCEPT an enumerated approved-delta allowlist — the deliberate
behavior changes the pre-kernel→kernel cutover introduced.

It is a pure, deterministic comparator: it performs NO I/O and issues NO provider/model
call, and it never decides a gate's final verdict. It only certifies that the two
derivations agree everywhere the cutover did not intend to change.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

# The deliberate, approved behavior changes of the cutover. A divergence whose category is
# one of these is an ``AcceptedDelta`` (when in the caller's ``allow`` set) rather than a
# ``Mismatch``. Any other divergence is always a ``Mismatch``.
APPROVED_DELTAS: frozenset[str] = frozenset(
    {
        "builtin_retune",
        "project_det",
        "project_llm_applicability",
        "code_review_budget",
        "partial_failure_continuation",
        "success_only_checkpoint",
    }
)

_REUSABLE_KINDS: frozenset[str] = frozenset({"success", "resumed"})

# Cutover-invariant identity/measurement fields: any difference is always a Mismatch.
_INVARIANT_FIELDS: tuple[str, ...] = (
    "order",
    "prompt_id",
    "contract_id",
    "context_digest",
    "model",
    "mode",
    "dependencies",
    "budget_estimate",
    "usage",
)


# ── observable projections ─────────────────────────────────────────────────────
@dataclass(frozen=True)
class UnitProjection:
    """One projected discovery unit: identity/measurement fields plus the policy signals
    that drive legacy reconstruction and delta classification."""

    unit_id: str
    prompt_id: str = ""
    contract_id: str = ""
    context_digest: str = ""
    model: str = ""
    mode: str = ""
    dependencies: tuple[str, ...] = ()
    budget_estimate: float = 0.0
    posture: str = ""
    usage: Mapping[str, int] = field(default_factory=dict)
    outcome_kind: str | None = None
    checkpoint_written: bool = False
    selected: bool = True
    order: int | None = None
    source: str = "builtin"
    exec_tier: str = "LLM"
    overlay_disabled: bool = False
    overlay_retuned: bool = False
    legacy_posture: str = ""
    project_applies: bool = True


@dataclass(frozen=True)
class StageProjection:
    """A projected discovery stage for one gate."""

    gate: str
    disposition: str
    units: tuple[UnitProjection, ...] = ()
    budget: float | None = None
    systemic_abort: bool = False


@dataclass(frozen=True)
class ObservedDiscovery:
    """The observed RP-06 derivation plus the auxiliary facts needed to reconstruct legacy."""

    rp06: StageProjection
    excluded: tuple[UnitProjection, ...] = ()
    legacy_checkpoints: frozenset[str] = frozenset()


# ── report shapes ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Mismatch:
    """An unapproved field-level divergence between legacy and RP-06."""

    unit_id: str | None
    field: str
    legacy: Any
    rp06: Any


@dataclass(frozen=True)
class AcceptedDelta:
    """A divergence that matches an approved-delta category."""

    unit_id: str | None
    field: str
    category: str


@dataclass(frozen=True)
class ComparisonReport:
    """The result of a shadow comparison."""

    mismatches: tuple[Mismatch, ...] = ()
    accepted: tuple[AcceptedDelta, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.mismatches


# ── internal diff carrier ──────────────────────────────────────────────────────
@dataclass(frozen=True)
class _Diff:
    unit_id: str | None
    field: str
    legacy: Any
    rp06: Any
    category: str | None  # None => always a Mismatch; else a candidate AcceptedDelta


def _local_failure(stage: StageProjection) -> bool:
    """A local (non-systemic) unit failure occurred in the stage."""
    if stage.systemic_abort:
        return False
    return any(u.outcome_kind == "failed" for u in stage.units)


def _diff_key(d: _Diff) -> tuple[bool, str, str]:
    """Stable ordering: stage-level fields (unit_id None) first, then by unit_id, field."""
    return (d.unit_id is not None, d.unit_id or "", d.field)


# ── stage-level diffs ──────────────────────────────────────────────────────────
def _budget_category(legacy: StageProjection, rp06: StageProjection) -> str | None:
    if rp06.gate == "code_review" and legacy.budget is None and rp06.budget is not None:
        return "code_review_budget"
    return None


def _stage_diffs(legacy: StageProjection, rp06: StageProjection) -> list[_Diff]:
    diffs: list[_Diff] = []
    if legacy.gate != rp06.gate:
        diffs.append(_Diff(None, "gate", legacy.gate, rp06.gate, None))
    if legacy.budget != rp06.budget:
        diffs.append(
            _Diff(None, "budget", legacy.budget, rp06.budget, _budget_category(legacy, rp06))
        )
    if legacy.disposition != rp06.disposition:
        cat = "partial_failure_continuation" if _local_failure(rp06) else None
        diffs.append(_Diff(None, "disposition", legacy.disposition, rp06.disposition, cat))
    if legacy.systemic_abort != rp06.systemic_abort:
        diffs.append(
            _Diff(None, "systemic_abort", legacy.systemic_abort, rp06.systemic_abort, None)
        )
    return diffs


# ── selection diffs (a unit present on only one side) ──────────────────────────
def _legacy_only_category(unit: UnitProjection) -> str | None:
    if unit.source == "builtin" and unit.overlay_disabled:
        return "builtin_retune"
    if unit.source == "project" and unit.exec_tier == "LLM" and not unit.project_applies:
        return "project_llm_applicability"
    return None


def _rp06_only_category(unit: UnitProjection) -> str | None:
    if unit.source == "project" and unit.exec_tier == "DET":
        return "project_det"
    return None


# ── per-unit field diffs (a unit present on both sides) ────────────────────────
def _posture_diff(legacy_u: UnitProjection, rp06_u: UnitProjection) -> _Diff | None:
    if legacy_u.posture == rp06_u.posture:
        return None
    cat = "builtin_retune" if rp06_u.overlay_retuned else None
    return _Diff(rp06_u.unit_id, "posture", legacy_u.posture, rp06_u.posture, cat)


def _checkpoint_diff(legacy_u: UnitProjection, rp06_u: UnitProjection) -> _Diff | None:
    if legacy_u.checkpoint_written == rp06_u.checkpoint_written:
        return None
    if (
        legacy_u.checkpoint_written
        and not rp06_u.checkpoint_written
        and rp06_u.outcome_kind not in _REUSABLE_KINDS
    ):
        cat: str | None = "success_only_checkpoint"
    else:
        cat = None
    return _Diff(
        rp06_u.unit_id,
        "checkpoint_written",
        legacy_u.checkpoint_written,
        rp06_u.checkpoint_written,
        cat,
    )


def _outcome_diff(
    legacy_u: UnitProjection, rp06_u: UnitProjection, stage: StageProjection
) -> _Diff | None:
    if legacy_u.outcome_kind == rp06_u.outcome_kind:
        return None
    cat = "partial_failure_continuation" if _local_failure(stage) else None
    return _Diff(rp06_u.unit_id, "outcome_kind", legacy_u.outcome_kind, rp06_u.outcome_kind, cat)


def _invariant_diffs(legacy_u: UnitProjection, rp06_u: UnitProjection) -> list[_Diff]:
    diffs: list[_Diff] = []
    for fld in _INVARIANT_FIELDS:
        lv: Any = getattr(legacy_u, fld)
        rv: Any = getattr(rp06_u, fld)
        if fld == "usage":
            lv, rv = dict(lv), dict(rv)
        if lv != rv:
            diffs.append(_Diff(rp06_u.unit_id, fld, lv, rv, None))
    return diffs


def _both_diffs(
    legacy_u: UnitProjection, rp06_u: UnitProjection, stage: StageProjection
) -> list[_Diff]:
    diffs: list[_Diff] = []
    for candidate in (
        _posture_diff(legacy_u, rp06_u),
        _checkpoint_diff(legacy_u, rp06_u),
        _outcome_diff(legacy_u, rp06_u, stage),
    ):
        if candidate is not None:
            diffs.append(candidate)
    diffs.extend(_invariant_diffs(legacy_u, rp06_u))
    return diffs


def _unit_diffs(legacy: StageProjection, rp06: StageProjection) -> list[_Diff]:
    legacy_by = {u.unit_id: u for u in legacy.units}
    rp06_by = {u.unit_id: u for u in rp06.units}
    diffs: list[_Diff] = []
    for uid in sorted(set(legacy_by) | set(rp06_by)):
        in_l, in_r = uid in legacy_by, uid in rp06_by
        if in_l and not in_r:
            unit = legacy_by[uid]
            diffs.append(_Diff(uid, "selected", True, False, _legacy_only_category(unit)))
        elif in_r and not in_l:
            unit = rp06_by[uid]
            diffs.append(_Diff(uid, "selected", False, True, _rp06_only_category(unit)))
        else:
            diffs.extend(_both_diffs(legacy_by[uid], rp06_by[uid], rp06))
    return diffs


def _classify(
    diffs: list[_Diff], allow: frozenset[str]
) -> tuple[list[Mismatch], list[AcceptedDelta]]:
    mismatches: list[Mismatch] = []
    accepted: list[AcceptedDelta] = []
    for d in diffs:
        if d.category is not None and d.category in allow:
            accepted.append(AcceptedDelta(d.unit_id, d.field, d.category))
        else:
            mismatches.append(Mismatch(d.unit_id, d.field, d.legacy, d.rp06))
    return mismatches, accepted


def compare_projections(
    legacy: StageProjection, rp06: StageProjection, *, allow: frozenset[str] = APPROVED_DELTAS
) -> ComparisonReport:
    """Diff two projections field by field, classifying each divergence via ``allow``."""
    diffs = _stage_diffs(legacy, rp06) + _unit_diffs(legacy, rp06)
    diffs.sort(key=_diff_key)
    mismatches, accepted = _classify(diffs, allow)
    return ComparisonReport(mismatches=tuple(mismatches), accepted=tuple(accepted))


# ── legacy reconstruction ──────────────────────────────────────────────────────
def _keep_in_legacy(unit: UnitProjection) -> bool:
    """Whether an observed RP-06 unit has a legacy counterpart (drop RP-06-only tiers)."""
    if unit.source == "project" and unit.exec_tier == "DET":
        return False
    if unit.source == "project" and unit.exec_tier == "LLM" and not unit.project_applies:
        return False
    return True


def _to_legacy_unit(unit: UnitProjection) -> UnitProjection:
    """Rewrite one unit into its legacy shape (un-retuned posture, success+failure checkpoint)."""
    posture = unit.legacy_posture if unit.legacy_posture else unit.posture
    checkpoint = unit.outcome_kind in {"success", "failed"}
    return replace(unit, posture=posture, checkpoint_written=checkpoint)


def _apply_legacy_abort(units: list[UnitProjection], rp06: StageProjection) -> list[UnitProjection]:
    """Legacy aborts at the first failure; every later unit becomes ``skipped``."""
    if rp06.systemic_abort:
        return units
    effs = [u.order if u.order is not None else i for i, u in enumerate(units)]
    failed_effs = [effs[i] for i, u in enumerate(units) if u.outcome_kind == "failed"]
    if not failed_effs:
        return units
    first = min(failed_effs)
    return [
        replace(u, outcome_kind="skipped") if effs[i] > first else u for i, u in enumerate(units)
    ]


def _legacy_disposition(rp06: StageProjection) -> str:
    if rp06.systemic_abort:
        return rp06.disposition
    if _local_failure(rp06):
        return "INDETERMINATE"
    return rp06.disposition


def reconstruct_legacy(
    rp06: StageProjection, *, excluded: tuple[UnitProjection, ...] = ()
) -> StageProjection:
    """Reconstruct the legacy reference projection from the observed RP-06 facts."""
    legacy_units = [_to_legacy_unit(u) for u in rp06.units if _keep_in_legacy(u)]
    legacy_units.extend(_to_legacy_unit(u) for u in excluded)
    legacy_units = _apply_legacy_abort(legacy_units, rp06)
    return StageProjection(
        gate=rp06.gate,
        disposition=_legacy_disposition(rp06),
        units=tuple(legacy_units),
        budget=None,
        systemic_abort=rp06.systemic_abort,
    )


# ── end-to-end comparison + RP-06 invariants ───────────────────────────────────
def _rp06_invariants(observed: ObservedDiscovery) -> list[Mismatch]:
    extra: list[Mismatch] = []
    rp06_by = {u.unit_id: u for u in observed.rp06.units}
    for unit in observed.rp06.units:
        if unit.checkpoint_written and unit.outcome_kind != "success":
            extra.append(Mismatch(unit.unit_id, "checkpoint_written", False, True))
    for uid in observed.legacy_checkpoints:
        resumed = rp06_by.get(uid)
        if resumed is not None and resumed.outcome_kind == "resumed":
            extra.append(Mismatch(uid, "checkpoint", "ignored", "resumed-from-legacy"))
    extra.sort(key=lambda m: (m.unit_id or "", m.field))
    return extra


def _merge_mismatches(
    existing: tuple[Mismatch, ...], extra: list[Mismatch]
) -> tuple[Mismatch, ...]:
    seen: set[Mismatch] = set()
    out: list[Mismatch] = []
    for m in list(existing) + extra:
        if m in seen:
            continue
        seen.add(m)
        out.append(m)
    return tuple(out)


def compare_discovery(
    observed: ObservedDiscovery, *, allow: frozenset[str] = APPROVED_DELTAS
) -> ComparisonReport:
    """Reconstruct legacy, diff against RP-06, and append the RP-06 invariant checks."""
    legacy = reconstruct_legacy(observed.rp06, excluded=observed.excluded)
    report = compare_projections(legacy, observed.rp06, allow=allow)
    extra = _rp06_invariants(observed)
    if not extra:
        return report
    return ComparisonReport(
        mismatches=_merge_mismatches(report.mismatches, extra), accepted=report.accepted
    )
