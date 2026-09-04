"""Eval budget ledger for the plan-replay eval suite (ticket fizzy-hypnotic-boto).

A plan-replay eval run makes many billable LLM calls; without a hard stop a runaway
sweep (a wide grid, a retry storm) can burn real money before anyone notices. This
module is the single choke point: :func:`estimate` gives a cheap pre-flight cost from a
flat historical per-sample rate, :func:`reserve` refuses to let a run start if its
estimate would push cumulative spend over the cap, and :func:`finalize` prices the
run's actual token usage (via ``genai_prices``, the same adapter
:mod:`rebar.llm.usage_report` uses) and durably records it. Pricing failures are LOUD by
design: an unpriceable run raises rather than silently recording ``usd=0``, because a
silent zero would let spend run past the cap undetected.

The ledger itself is a flat JSONL file, one line per finalized run — append-only, so
"spent so far" is always a sum over lines already on disk, with no separate state to get
out of sync.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

#: Hard budget cap for the eval suite, in USD. No run may push cumulative recorded
#: spend past this ceiling.
LEDGER_CAP_USD = 200.0

#: Held-back headroom below the cap that :func:`reserve` will not let an estimate eat
#: into — a safety margin against the gap between an estimate and a run's actual cost.
LEDGER_RESERVE_USD = 30.0

#: Flat historical per-sample cost estimate, in USD, keyed by eval tier. Not a
#: token-derived prediction -- a pre-flight number cheap enough to compute before any
#: LLM call is made.
PER_SAMPLE_ESTIMATE_USD = {
    "tier1": 0.5,
    "tier2": 3.4,
    "criteria-eval-cheap": 0.03,
    "criteria-eval-agent": 0.25,
}

#: Default location of the committed ledger JSONL file (one row per finalized eval run).
DEFAULT_LEDGER_PATH = "docs/experiments/plan-review-gate/replay/ledger.jsonl"

#: The four billable token fields summed into a finalized ledger entry.
_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


class BudgetExceeded(Exception):
    """Raised by :func:`reserve` when an estimate would exceed the remaining budget."""


class UnpriceableRun(Exception):
    """Raised by :func:`finalize`/:func:`reconcile` when a row's model cannot be priced.

    Pricing must never fail silently into ``usd=0`` -- an unresolvable model raises this
    instead, and nothing is written to the ledger for that run.
    """


def estimate(tier: str, sample_n: int) -> float:
    """Flat pre-flight cost estimate for running ``sample_n`` samples of ``tier``.

    Raises ``ValueError`` for an unknown tier.
    """
    try:
        per_sample = PER_SAMPLE_ESTIMATE_USD[tier]
    except KeyError:
        raise ValueError(f"unknown eval tier: {tier!r}") from None
    return per_sample * sample_n


def _read_ledger(ledger_path: str) -> list[dict]:
    """Parse the JSONL at ``ledger_path``; a missing file is an empty ledger."""
    try:
        with open(ledger_path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]
    except FileNotFoundError:
        return []


def _spent_so_far(ledger_path: str) -> float:
    return sum(float(row.get("usd", 0.0) or 0.0) for row in _read_ledger(ledger_path))


def reserve(
    estimate_usd: float,
    *,
    ledger_path: str = DEFAULT_LEDGER_PATH,
    cap_usd: float | None = None,
    reserve_usd: float | None = None,
) -> None:
    """Refuse a run whose ``estimate_usd`` would exceed the remaining budget.

    Remaining budget is ``cap_usd``, minus the ``reserve_usd`` headroom, minus
    everything already recorded in the ledger. ``cap_usd``/``reserve_usd`` default to the
    module globals (resolved live at call time, so patching the globals still takes
    effect) when omitted; a sibling epic passes its own ceiling. Raises
    :class:`BudgetExceeded` naming the remaining allocation when the estimate does not
    fit; otherwise returns ``None``.
    """
    cap = LEDGER_CAP_USD if cap_usd is None else cap_usd
    reserve_headroom = LEDGER_RESERVE_USD if reserve_usd is None else reserve_usd
    spent = _spent_so_far(ledger_path)
    remaining = cap - reserve_headroom - spent
    if estimate_usd > remaining:
        raise BudgetExceeded(
            f"estimate ${estimate_usd:.2f} exceeds remaining allocation ${remaining:.2f} "
            f"(cap ${cap:.2f}, reserve ${reserve_headroom:.2f}, "
            f"spent ${spent:.2f})"
        )


def _price_rows(rows: list[dict]) -> tuple[float, dict[str, int]]:
    """Price every row via ``genai_prices``; sum cost and token totals.

    Raises :class:`UnpriceableRun` if any row's model cannot be resolved -- pricing
    failures must be loud, never a silent ``usd=0``.
    """
    import genai_prices

    from rebar.llm.usage_report import _pricing_model_ref, usage_kwargs

    totals = dict.fromkeys(_TOKEN_FIELDS, 0)
    total_usd = 0.0
    for row in rows:
        for field in _TOKEN_FIELDS:
            totals[field] += int(row.get(field, 0) or 0)
        model = row.get("model")
        raw_ts = row.get("timestamp")
        timestamp = datetime.fromisoformat(str(raw_ts)) if raw_ts else None
        try:
            price = genai_prices.calc_price(
                genai_prices.Usage(**usage_kwargs(row)),
                model_ref=_pricing_model_ref(str(model)),
                provider_id=row.get("provider") or None,
                genai_request_timestamp=timestamp,
            )
        except LookupError as exc:
            raise UnpriceableRun(f"cannot price model {model!r}: {exc}") from exc
        total_usd += float(price.total_price)
    return total_usd, totals


def finalize(
    run_id: str,
    tier: str,
    candidate: str,
    sample_n: int,
    per_pass_models: dict,
    rows: list[dict],
    *,
    ledger_path: str = DEFAULT_LEDGER_PATH,
) -> dict:
    """Price ``rows`` and append one JSONL entry recording the run's actual cost.

    Raises :class:`UnpriceableRun` (and writes nothing) if any row's model cannot be
    priced by ``genai_prices``.
    """
    total_usd, totals = _price_rows(rows)
    entry = {
        "run_id": run_id,
        "tier": tier,
        "candidate": candidate,
        "sample_n": sample_n,
        "models": per_pass_models,
        "usd": total_usd,
        **totals,
        "finished": datetime.now(UTC).isoformat(),
    }
    directory = os.path.dirname(ledger_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(ledger_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")
    return entry


def charge_estimate(
    run_id: str,
    tier: str,
    candidate: str,
    estimate_usd: float,
    *,
    ledger_path: str = DEFAULT_LEDGER_PATH,
) -> dict:
    """Record the pre-flight ESTIMATE as consumed spend for a run whose actual usage
    cannot be priced into billable rows.

    :func:`finalize` needs priceable ``rows`` (a model ``genai_prices`` resolves) to record
    spend; a caller that ran a live model but cannot surface such a row would otherwise
    record nothing, leaving :func:`reserve`'s ``spent`` at zero so the budget cap never
    trips (it fails OPEN). Appending the deterministic :func:`estimate` here keeps the cap
    fail-CLOSED: ``spent`` accumulates by the estimate and a later :func:`reserve` refuses
    the run that would exceed the cap. The entry is flagged ``estimated`` so a reader can
    tell an estimate charge from a priced :func:`finalize` entry.
    """
    entry = {
        "run_id": run_id,
        "tier": tier,
        "candidate": candidate,
        "usd": float(estimate_usd),
        "estimated": True,
        "finished": datetime.now(UTC).isoformat(),
    }
    directory = os.path.dirname(ledger_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(ledger_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")
    return entry


def reconcile(
    run_id: str,
    tier: str,
    candidate: str,
    sample_n: int,
    per_pass_models: dict,
    rows: list[dict],
    *,
    ledger_path: str = DEFAULT_LEDGER_PATH,
) -> dict:
    """Finalize a run that may already have been recorded (e.g. after a crash mid-run).

    If ``run_id`` is already present in the ledger, returns that existing entry
    unchanged -- never re-appends or double-counts. Otherwise behaves exactly like
    :func:`finalize`.
    """
    for existing in _read_ledger(ledger_path):
        if existing.get("run_id") == run_id:
            return existing
    return finalize(
        run_id,
        tier,
        candidate,
        sample_n,
        per_pass_models,
        rows,
        ledger_path=ledger_path,
    )


def print_summary(*, ledger_path: str = DEFAULT_LEDGER_PATH, cap_usd: float | None = None) -> str:
    """Multi-line report: total spent, remaining allocation, and a per-tier breakdown.

    ``cap_usd`` defaults to the module global (resolved live at call time) when omitted;
    a sibling epic passes its own ceiling so the reported headroom reflects that cap. All
    dollar amounts render with exactly two decimal places.
    """
    cap = LEDGER_CAP_USD if cap_usd is None else cap_usd
    rows = _read_ledger(ledger_path)
    spent = sum(float(row.get("usd", 0.0) or 0.0) for row in rows)
    remaining = cap - spent
    per_tier: dict[str, float] = {}
    for row in rows:
        tier = str(row.get("tier", "?"))
        per_tier[tier] = per_tier.get(tier, 0.0) + float(row.get("usd", 0.0) or 0.0)

    lines = [
        "Eval budget ledger",
        f"  spent:     ${spent:.2f}",
        f"  cap:       ${cap:.2f}",
        f"  remaining: ${remaining:.2f}",
        "  by tier:",
    ]
    if per_tier:
        for tier in sorted(per_tier):
            lines.append(f"    {tier}: ${per_tier[tier]:.2f}")
    else:
        lines.append("    (no runs recorded)")
    return "\n".join(lines)
