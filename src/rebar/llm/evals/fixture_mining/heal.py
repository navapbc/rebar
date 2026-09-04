"""Public contract for the scheduled fixture-mining heal loop (ticket 1cef).

This module owns the per-criterion loop, the quarantine breaker, the due-stamp, and the
ledger reserve/finalize orchestration. The model work (emit + score for one criterion) is
delegated to an injectable ``attempter`` seam so the loop is deterministic under test.

DESIGN CONTRACT (the orchestrator authored this; the implementation must match it exactly):

``heal_fixtures(repo_root, *, attempter, gap_source=None, now=None, interval_days=None,
                cap_usd=25.0, threshold=3, state_path=None, ledger_path=None,
                gate_key="plan_review") -> HealReport``

Behaviour:

* Due-stamp. Load ``state_path`` JSON ``{"last_run": float|None, "counters": {id: int}}``.
  If ``last_run`` is set and ``now - last_run < interval_days * 86400`` seconds, return
  ``HealReport(ran=False)`` and write nothing. Otherwise run, and on completion persist
  ``last_run = now`` and the updated counters. ``now`` defaults to ``time.time()``;
  ``interval_days`` defaults to the configured interval (30).

* Gap + skip. ``gap = list(gap_source())`` (default: the criteria the plan-review gate routes
  with no eval spec, i.e. ``fixture_selection._default_criteria``). Build the unreliable map
  from OPEN tickets titled exactly ``UNRELIABLE_TITLE_PREFIX + criterion_id`` (lowest ticket
  id wins on a duplicate), via ``rebar.list_tickets(status="open", repo_root=...)``. A
  criterion in that map is skipped (reported in ``skipped_unreliable``); the rest form the
  attempt list (sorted). Counters for criteria no longer in the attempt list are dropped
  (so a covered or quarantined — or reopened — criterion starts fresh).

* Per-criterion loop, in attempt order:
    - ``tier, sample_n = attempter.plan(criterion_id)``.
    - ``ledger.reserve(ledger.estimate(tier, sample_n), ledger_path=..., cap_usd=cap_usd,
      reserve_usd=0.0)``. ``cap_usd`` is the whole heal budget, so the pre-check reserves no
      extra headroom (``reserve_usd=0.0``) — the effective ceiling is exactly ``cap_usd`` for
      every cap, matching the admission run's own reservation against the same ledger.
      On ``BudgetExceeded``: set ``stopped_for_budget=True`` and STOP (criteria already
      completed stay admitted).
    - ``result = attempter.run(criterion_id)``.
    - Record spend for the attempt (``_record_spend``): if ``result.usage_rows`` carry a
      priceable model, ``ledger.finalize(...)`` records actual cost; otherwise (no rows, or an
      unpriceable model) ``ledger.charge_estimate(...)`` records the pre-flight
      ``ledger.estimate(tier, sample_n)``. An entry is written for EVERY attempt, so the next
      ``ledger.reserve`` sees accumulated spend and the cap fails CLOSED — never open on a run
      that recorded nothing.
    - Fold the outcome:
        * ``ADMITTED`` — reset the criterion's failure counter to 0; add to ``admitted``.
        * ``FAILED_REPRODUCE`` or ``SKIPPED_UNBALANCED`` — increment the counter; when it
          reaches ``threshold`` (default 3), file the unreliable ticket (idempotent) and add
          to ``quarantined``.
        * ``UNMINABLE`` — file the unreliable ticket on FIRST encounter (no threshold) and
          add to ``quarantined``.
        * ``INCOMPLETE`` — a transient outage (the admission run exhausted its retries, e.g.
          an LLM/infra outage). Leave the counter UNCHANGED and neither admit nor quarantine,
          so a passing outage does not accrue toward a spurious quarantine; the criterion is
          retried on the next sweep.

* Filing a ticket is idempotent: only ``rebar.create_ticket`` when no OPEN ticket of that
  exact title already exists.

Assert OBSERVABLE behaviour against this contract (the returned ``HealReport``, tickets in
the store, and the ledger total) — never private structure.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import rebar
from rebar.llm.criteria.ids import criterion_prompt_id
from rebar.llm.evals.fixture_selection import _default_criteria
from rebar.llm.evals.plan_replay import ledger

#: Title of a quarantine ticket is EXACTLY this prefix followed by the criterion id.
UNRELIABLE_TITLE_PREFIX = "unreliable-criterion: "

#: Drift reasons that mark a criterion as fundamentally un-minable (quarantine on first
#: sight) rather than a transient reproduction failure. Any drift entry carrying one of
#: these outranks the reproduction-failure entries for the same criterion.
UNMINABLE_DRIFT_REASONS = frozenset({"container-material-unrecoverable", "not-inline-admissible"})


class AttemptOutcome:
    """The five dispositions the per-criterion attempt can report."""

    ADMITTED = "admitted"
    FAILED_REPRODUCE = "failed-reproduce"
    SKIPPED_UNBALANCED = "skipped-unbalanced"
    UNMINABLE = "unminable"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class AttemptResult:
    """One criterion's attempt outcome plus the billable rows to finalize.

    ``usage_rows`` are priced by ``ledger.finalize`` (empty when nothing billable ran).
    ``reason`` carries the runner drift reason for an ``UNMINABLE`` outcome
    (``container-material-unrecoverable`` or ``not-inline-admissible``).
    """

    outcome: str
    tier: str = "criteria-eval-cheap"
    sample_n: int = 0
    usage_rows: tuple[dict, ...] = ()
    reason: str = ""


class Attempter(Protocol):
    """The injectable per-criterion work seam."""

    def plan(self, criterion_id: str) -> tuple[str, int]:
        """Return ``(tier, sample_n)`` for the criterion — cheap, no model call."""
        ...

    def run(self, criterion_id: str) -> AttemptResult:
        """Emit + score the criterion and report the outcome."""
        ...


@dataclass(frozen=True)
class HealReport:
    """Observable outcome of one heal run."""

    ran: bool
    attempted: tuple[str, ...] = ()
    admitted: tuple[str, ...] = ()
    quarantined: tuple[str, ...] = ()
    skipped_unreliable: tuple[str, ...] = ()
    stopped_for_budget: bool = False


def heal_fixtures(
    repo_root: str | Path,
    *,
    attempter: Attempter | None = None,
    gap_source: Callable[[], Sequence[str]] | None = None,
    now: float | None = None,
    interval_days: int | None = None,
    cap_usd: float = 25.0,
    threshold: int = 3,
    state_path: str | Path | None = None,
    ledger_path: str | Path | None = None,
    gate_key: str = "plan_review",
    dry_run: bool = False,
) -> HealReport:
    """Run the scheduled fixture-mining heal loop. See the module docstring for the contract.

    When ``dry_run`` is True, compute and return the attempt list (``attempted``) and
    ``skipped_unreliable`` WITHOUT calling the attempter, reserving/finalizing spend, or
    advancing the due-stamp — a read-only preview the CLI prints. ``dry_run`` ignores the
    due-stamp (it always previews). ``attempter`` may be ``None`` only when ``dry_run`` is
    True (the preview computes the attempt list without running any attempt); a live run
    requires an ``attempter``.
    """
    root = Path(repo_root)
    run_now = time.time() if now is None else now
    interval = _configured_interval_days(root) if interval_days is None else interval_days
    state_file = (
        Path(state_path) if state_path is not None else root / ".rebar" / "fixture_heal_state.json"
    )
    ledger_file = (
        Path(ledger_path)
        if ledger_path is not None
        else root / ".rebar" / "fixture_heal_ledger.jsonl"
    )

    state = _load_state(state_file)
    if not dry_run and _not_due(state, run_now, interval):
        return HealReport(ran=False)
    if attempter is None and not dry_run:
        raise ValueError("live fixture heal requires an attempter")

    gap_ids = _gap_ids(root, gate_key, gap_source)
    unreliable = _unreliable_map(root)
    skipped = tuple(sorted(criterion for criterion in gap_ids if criterion in unreliable))
    attempted = tuple(sorted(criterion for criterion in gap_ids if criterion not in unreliable))
    if dry_run:
        return HealReport(ran=True, attempted=attempted, skipped_unreliable=skipped)

    counters = {criterion: int(state["counters"].get(criterion, 0)) for criterion in attempted}
    admitted: list[str] = []
    quarantined: list[str] = []
    stopped_for_budget = False
    assert attempter is not None

    for criterion_id in attempted:
        tier, sample_n = attempter.plan(criterion_id)
        try:
            ledger.reserve(
                ledger.estimate(tier, sample_n),
                ledger_path=str(ledger_file),
                cap_usd=cap_usd,
                reserve_usd=0.0,
            )
        except ledger.BudgetExceeded:
            stopped_for_budget = True
            break

        result = attempter.run(criterion_id)
        _record_spend(criterion_id, tier, sample_n, result.usage_rows, ledger_file)
        _fold_outcome(
            criterion_id,
            result.outcome,
            counters,
            admitted,
            quarantined,
            root,
            threshold,
        )

    _save_state(state_file, {"last_run": run_now, "counters": counters})
    return HealReport(
        ran=True,
        attempted=attempted,
        admitted=tuple(admitted),
        quarantined=tuple(quarantined),
        skipped_unreliable=skipped,
        stopped_for_budget=stopped_for_budget,
    )


def _record_spend(
    criterion_id: str,
    tier: str,
    sample_n: int,
    usage_rows: tuple[dict, ...],
    ledger_file: Path,
) -> None:
    """Record this attempt's spend against the budget ledger.

    Prices ``usage_rows`` via :func:`ledger.finalize` when they carry a priceable model.
    When there are no rows, or pricing fails (the live per-case runner does not yet surface
    a priceable usage row — tracked as follow-up work), fall back to charging the pre-flight
    :func:`ledger.estimate` via :func:`ledger.charge_estimate`. Either way an entry is
    written for every attempt, so :func:`ledger.reserve` sees accumulated ``spent`` and the
    budget cap fails CLOSED — never open on a run that recorded nothing.
    """
    run_id = f"heal-{criterion_prompt_id(criterion_id)}"
    if usage_rows:
        try:
            ledger.finalize(
                run_id,
                tier,
                criterion_id,
                sample_n,
                {},
                list(usage_rows),
                ledger_path=str(ledger_file),
            )
            return
        except ledger.UnpriceableRun:
            pass
    ledger.charge_estimate(
        run_id,
        tier,
        criterion_id,
        ledger.estimate(tier, sample_n),
        ledger_path=str(ledger_file),
    )


def production_attempter(
    repo_root: str | Path, *, ledger_path: str | Path, cap_usd: float
) -> Attempter:
    """Build the live selection→emit→admission attempter used by the CLI."""
    return _ProductionAttempter(Path(repo_root), Path(ledger_path), cap_usd)


def _configured_interval_days(repo_root: Path) -> int:
    from rebar import config

    return config.fixture_heal_interval_days(repo_root)


def _load_state(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"last_run": None, "counters": {}}
    counters = raw.get("counters")
    return {
        "last_run": raw.get("last_run"),
        "counters": counters if isinstance(counters, dict) else {},
    }


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")


def _not_due(state: dict[str, Any], now: float, interval_days: int) -> bool:
    last_run = state.get("last_run")
    return last_run is not None and now - float(last_run) < interval_days * 86400


def _gap_ids(
    repo_root: Path, gate_key: str, gap_source: Callable[[], Sequence[str]] | None
) -> list[str]:
    if gap_source is not None:
        return list(gap_source())
    return list(_default_criteria(str(repo_root), gate_key))


def _unreliable_map(repo_root: Path) -> dict[str, str]:
    unreliable: dict[str, str] = {}
    for ticket in rebar.list_tickets(status="open", repo_root=str(repo_root)):
        title = str(ticket["title"])
        if not title.startswith(UNRELIABLE_TITLE_PREFIX):
            continue
        criterion_id = title[len(UNRELIABLE_TITLE_PREFIX) :]
        ticket_data: Any = ticket
        ticket_id = str(ticket_data["ticket_id"])
        current = unreliable.get(criterion_id)
        if current is None or ticket_id < current:
            unreliable[criterion_id] = ticket_id
    return unreliable


def _fold_outcome(
    criterion_id: str,
    outcome: str,
    counters: dict[str, int],
    admitted: list[str],
    quarantined: list[str],
    repo_root: Path,
    threshold: int,
) -> None:
    if outcome == AttemptOutcome.ADMITTED:
        counters[criterion_id] = 0
        admitted.append(criterion_id)
        return
    if outcome == AttemptOutcome.UNMINABLE:
        _file_unreliable_ticket(repo_root, criterion_id)
        quarantined.append(criterion_id)
        return
    if outcome in (AttemptOutcome.FAILED_REPRODUCE, AttemptOutcome.SKIPPED_UNBALANCED):
        counters[criterion_id] = counters.get(criterion_id, 0) + 1
        if counters[criterion_id] >= threshold:
            _file_unreliable_ticket(repo_root, criterion_id)
            quarantined.append(criterion_id)


def _file_unreliable_ticket(repo_root: Path, criterion_id: str) -> None:
    title = UNRELIABLE_TITLE_PREFIX + criterion_id
    for ticket in rebar.list_tickets(status="open", repo_root=str(repo_root)):
        if ticket["title"] == title:
            return
    rebar.create_ticket("task", title, repo_root=str(repo_root))


class _ProductionAttempter:
    def __init__(self, repo_root: Path, ledger_path: Path, cap_usd: float) -> None:
        self._repo_root = repo_root
        self._ledger_path = ledger_path
        self._cap_usd = cap_usd
        self._rows: dict[str, list[dict[str, Any]]] = {}

    def plan(self, criterion_id: str) -> tuple[str, int]:
        rows = self._criterion_rows(criterion_id)
        return (_tier_for_criterion(criterion_id, self._repo_root), _sample_n(rows))

    def run(self, criterion_id: str) -> AttemptResult:
        rows = self._criterion_rows(criterion_id)
        if _emitter_skips_unbalanced(criterion_id, rows, self._repo_root):
            return AttemptResult(outcome=AttemptOutcome.SKIPPED_UNBALANCED)
        summary = _run_admission(
            criterion_id, rows, self._repo_root, self._ledger_path, self._cap_usd
        )
        if criterion_id in summary.admitted:
            return AttemptResult(outcome=AttemptOutcome.ADMITTED)
        if criterion_id in summary.incomplete:
            return AttemptResult(outcome=AttemptOutcome.INCOMPLETE, reason="incomplete")
        drift_reason = _drift_reason(criterion_id, summary.drift)
        if drift_reason in UNMINABLE_DRIFT_REASONS:
            return AttemptResult(outcome=AttemptOutcome.UNMINABLE, reason=drift_reason)
        return AttemptResult(outcome=AttemptOutcome.FAILED_REPRODUCE, reason=drift_reason)

    def _criterion_rows(self, criterion_id: str) -> list[dict[str, Any]]:
        if criterion_id not in self._rows:
            self._rows[criterion_id] = _select_rows(self._repo_root, criterion_id)
        return self._rows[criterion_id]


def _sample_n(rows: list[dict[str, Any]]) -> int:
    from rebar.llm.evals import fixture_emit

    candidates = [row for row in rows if row.get("kind") == "candidate"]
    return len(fixture_emit._selected(candidates, "fire")) + len(
        fixture_emit._selected(candidates, "no_fire")
    )


def _tier_for_criterion(criterion_id: str, repo_root: Path) -> str:
    from rebar.llm.evals import fixture_admission

    return fixture_admission._tier_for_criterion(criterion_id, None, str(repo_root))


def _select_rows(repo_root: Path, criterion_id: str) -> list[dict[str, Any]]:
    from rebar import config
    from rebar.llm.evals import fixture_selection
    from rebar.llm.evals.plan_replay import corpus

    cache_dir = repo_root / ".rebar" / "fixture_heal_cache"
    tracker = str(config.tracker_dir(repo_root))
    manifest = corpus.build_corpus({"default": tracker}, cache_dir=cache_dir)
    reviews = fixture_selection._load_cache_rows(cache_dir, str(manifest["content_hash"]))
    findings = fixture_selection._review_findings(tracker)
    for review in reviews:
        review["findings"] = findings.get((review["ticket_id"], review["review_event_uuid"]), [])
    fixture_selection._enrich_escaped_batched(reviews, tracker)
    return fixture_selection.select_candidates(
        reviews,
        criteria_ids=[criterion_id],
        repo_root=str(repo_root),
    )


def _emitter_skips_unbalanced(
    criterion_id: str, rows: list[dict[str, Any]], repo_root: Path
) -> bool:
    from rebar.llm.evals import fixture_emit, fixture_selection

    prompt_id = criterion_prompt_id(criterion_id)
    manifest_path = repo_root / ".rebar" / "fixture_heal_manifests" / f"{prompt_id}.jsonl"
    out_dir = repo_root / ".rebar" / "fixture_heal_emit_preview" / prompt_id
    fixture_selection.write_manifest(rows, manifest_path)
    report = fixture_emit.emit_specs(manifest_path, out_dir)
    return criterion_id in report.skipped_unbalanced


def _run_admission(
    criterion_id: str,
    rows: list[dict[str, Any]],
    repo_root: Path,
    ledger_path: Path,
    cap_usd: float,
):
    from rebar.llm.config import LLMConfig
    from rebar.llm.evals import eval_solver, fixture_admission, fixture_selection
    from rebar.llm.runner import get_runner

    prompt_id = criterion_prompt_id(criterion_id)
    manifest_path = repo_root / ".rebar" / "fixture_heal_manifests" / f"{prompt_id}.jsonl"
    spec_dir = repo_root / ".rebar" / "evals"
    drift_path = repo_root / ".rebar" / "fixture_heal_drift.md"
    fixture_selection.write_manifest(rows, manifest_path)
    runner = get_runner(LLMConfig.from_env(repo_root=str(repo_root)))

    def solve(pid: str, case: dict) -> fixture_admission.CaseOutcome:
        output = eval_solver.run_case(pid, case, runner=runner, repo_root=str(repo_root))
        # The inline-criterion case runner does not surface a priceable per-call usage row
        # today (the ran-model string it would carry is not resolvable by the pricer — the
        # bedrock-region pricing gap, tracked as follow-up). Reporting no rows is honest, not
        # a claim of zero spend: the heal loop charges the pre-flight ESTIMATE per attempt
        # (see `_record_spend`), so the budget cap still fails CLOSED.
        return fixture_admission.CaseOutcome(fired=bool(output.get("findings")), usage_rows=[])

    material_index = _material_index(repo_root)
    return fixture_admission.run_admission(
        manifest_path,
        material_index=material_index,
        solver=solve,
        out_dir=spec_dir,
        drift_path=drift_path,
        ledger_path=ledger_path,
        cap_usd=cap_usd,
        reserve_usd=0.0,
        repo_root=str(repo_root),
    )


def _material_index(repo_root: Path) -> dict[str, dict[str, Any]]:
    from rebar import config
    from rebar.llm.evals import fixture_selection
    from rebar.llm.evals.plan_replay import corpus

    cache_dir = repo_root / ".rebar" / "fixture_heal_cache"
    manifest = corpus.build_corpus(
        {"default": str(config.tracker_dir(repo_root))}, cache_dir=cache_dir
    )
    rows = fixture_selection._load_cache_rows(cache_dir, str(manifest["content_hash"]))
    return {str(row["review_event_uuid"]): row for row in rows}


def _drift_reason(criterion_id: str, drift: Sequence[Any]) -> str:
    """Pick the authoritative drift reason for a criterion.

    ``run_admission`` can emit several drift entries for one criterion (e.g. multiple
    ``non-reproducing`` cases plus an ``unbalanced``/unrecoverable-material row). An
    un-minable reason is authoritative — it must outrank a reproduction-failure reason
    regardless of entry order — so a criterion with any un-minable drift entry is quarantined
    on first sight instead of being retried three times. Falls back to the first matching
    entry's reason when none is un-minable.
    """
    reasons = [
        str(getattr(entry, "reason", ""))
        for entry in drift
        if getattr(entry, "criterion", None) == criterion_id
    ]
    for reason in reasons:
        if reason in UNMINABLE_DRIFT_REASONS:
            return reason
    return reasons[0] if reasons else ""
