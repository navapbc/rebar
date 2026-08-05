"""Retrieval/reporting side of the LLM usage log: parse, price, and summarize.

Split from :mod:`rebar.llm.usage_log` along its natural call-graph seam (the module-size
cap): ``usage_log`` OWNS the durable JSONL schema and the write path (``record`` /
``record_failure`` and the sink-resolution rules); this module owns everything that READS
a written log back — :func:`_read`, per-row pricing via the optional ``genai-prices``
extra, the Markdown :func:`summarize` table for ``$GITHUB_STEP_SUMMARY``, the run-shape
(loop-versus-breadth) section, and the ``summarize`` CLI. ``python -m rebar.llm.usage_log
summarize <path>`` remains the documented entry point (the workflows and docs use it);
``usage_log`` forwards it here.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from rebar.llm.usage_log import _FIELDS

logger = logging.getLogger(__name__)

#: The four billable token fields genai-prices consumes (``requests`` is not billable).
_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")

#: The cost footer when the optional ``pricing`` extra is not installed.
_PRICING_UNAVAILABLE = "unavailable (install rebar[pricing])"


def _read(path: str) -> list[dict]:
    """Parse the JSONL at ``path``; tolerate a missing file and skip malformed lines."""
    rows: list[dict] = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning("usage-log: skipping malformed line in %s", path)
    except FileNotFoundError:
        return []
    return rows


def usage_kwargs(row: dict) -> dict[str, int]:
    """Adapter: a stored JSONL row -> genai-prices ``Usage`` kwargs.

    Maps the four billable token fields one-to-one; an absent or null field defaults to
    0. Pure dict-to-kwargs — no dependence on live pydantic-ai usage objects, so rows
    written by any past run remain priceable.
    """
    return {field: int(row.get(field, 0) or 0) for field in _TOKEN_FIELDS}


def _pricing_module():
    """Return the ``genai_prices`` module, or None when the extra is not installed."""
    try:
        import genai_prices
    except ImportError:
        return None
    return genai_prices


def _pricing_model_ref(model: str) -> str:
    """The id to hand genai-prices as ``model_ref`` for a STORED model string.

    Rows record the model PROVIDER-QUALIFIED (``anthropic:claude-sonnet-4-6``) because epic
    061c made model strings carry their provider and ``agent_call`` records ``ran_model``
    verbatim. genai-prices resolves a BARE id and treats the qualifier as part of the name, so
    it raised ``LookupError`` on every qualified id — which :func:`_price_row` then correctly
    turned into "unpriced", making the drop SILENT. Worse, it was asymmetric: only the
    ``bedrock:`` form happened to resolve, so a provider cost comparison showed figures for
    Bedrock and blanks for Anthropic and OpenAI, reading as "Bedrock is the expensive arm" when
    it was the only arm being measured (bug 2ca9). Dropping the qualifier here is the whole
    fix; ``provider_id`` stays the provider signal it already is.

    The qualifier is identified by REGISTRY MEMBERSHIP, via
    :func:`~rebar.llm.config.split_provider_qualifier` (membership in
    ``KNOWN_PROVIDER_NAMES``) — deliberately NOT by prefix sniffing and NOT by parsing the
    colon here. That is correctness, not style: a real AWS id such as
    ``anthropic.claude-haiku-4-5-20251001-v1:0`` prices today, and splitting on the first
    colon would hand genai-prices ``"0"``. Membership leaves an unrecognized prefix intact,
    which is the only answer that keeps such an id priceable — and it keeps this module from
    growing a second, drifting parser of the qualifier grammar.
    """
    from rebar.llm.config import split_provider_qualifier

    _, bare = split_provider_qualifier(model)
    return bare


def _price_row(pricing, row: dict) -> float | None:
    """Est. USD cost for one row, or None (= unpriced). Pricing must never break
    summarize: LookupError (genai-prices' unknown-model signal) and a missing model are
    silently unpriced; anything else logs a WARNING and is unpriced."""
    model = row.get("model")
    if not model:
        return None
    try:
        raw_ts = row.get("timestamp")
        timestamp = datetime.fromisoformat(str(raw_ts)) if raw_ts else None
        price = pricing.calc_price(
            pricing.Usage(**usage_kwargs(row)),
            model_ref=_pricing_model_ref(str(model)),
            provider_id=row.get("provider") or None,
            genai_request_timestamp=timestamp,
        )
        return float(price.total_price)
    except LookupError:
        return None
    except Exception as exc:  # noqa: BLE001 — pricing is best-effort telemetry, never a hard error
        logger.warning("usage-log: pricing failed for model=%s: %s", model, exc)
        return None


def _cost_cell(cost: float, priced: int) -> str:
    return f"${cost:.4f}" if priced else "—"


def _run_shape_section(rows: list[dict]) -> list[str]:
    """The loop-versus-breadth table, or ``[]`` when no row carries run-shape fields.

    Persisting the counts is only half of bug aec1: the point is that an operator who hits a
    budget exhaustion can tell whether they paid for a LOOP or for genuine BREADTH. A retrieval
    command rendering only token counters leaves the signal written and never displayed — the
    same "computed then discarded" defect, one layer out — so the command that READS the log
    must show the discriminator, not merely carry it.

    ``ratio`` is ``distinct / total``, the one number that separates them: near 1.0 every call
    differed (breadth), near 0 the agent span on a handful (a loop). Measured live, 238/258 =
    0.92 is healthy; 76/257 = 0.30, 167/270 = 0.62 and 135/264 = 0.51 are pathological.

    A SEPARATE section appended after the token table, never extra columns on it: that table
    feeds ``$GITHUB_STEP_SUMMARY`` on the billable weekly jobs and several tests assert on it,
    so additive keeps every existing reader byte-identical. Shapeless rows are skipped and an
    all-shapeless log yields ``[]``, so every pre-aec1 log renders exactly as it did. Counts are
    SUMMED per op — exact for the usual single-call case, an upper bound across calls, so a loop
    can only look *better* than it is: the safe direction for a number used to accuse one.
    """

    shaped = [row for row in rows if "tool_calls" in row]
    if not shaped:
        return []
    # (column, row field, combine). Limits are MAXed rather than summed: they are the ceiling
    # each call ran under, so the largest is the one that mattered, whereas summing them would
    # invent a budget no call ever had.
    cols = (
        ("tool_calls", "tool_calls", sum),
        ("distinct", "tool_calls_distinct", sum),
        ("repeat", "max_consecutive_repeat", max),
        ("req", "request_limit", max),
        ("cap", "tool_calls_limit", max),
    )
    per_op: dict[str, dict[str, int]] = {}
    for row in shaped:
        agg = per_op.setdefault(str(row.get("op", "?")), dict.fromkeys([c[0] for c in cols], 0))
        agg["calls"] = agg.get("calls", 0) + 1
        for key, field, combine in cols:
            agg[key] = combine((agg[key], int(row.get(field, 0) or 0)))
    out = [
        "",
        "#### Run shape (loop vs breadth)",
        "",
        "| op | calls | tool_calls | distinct | ratio | max_repeat | request_limit "
        "| tool_calls_limit |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for op in sorted(per_op):
        agg = per_op[op]
        total = int(agg["tool_calls"])
        distinct = int(agg["distinct"])
        # No tool calls at all is not a ratio of 0 (which would read as a perfect loop) — it is
        # the absence of a measurement, so it prints as an em dash.
        ratio = f"{distinct / total:.3f}" if total else "—"
        out.append(
            f"| {op} | {int(agg['calls'])} | {total} | {distinct} | {ratio} "
            f"| {int(agg['repeat'])} | {int(agg['req'])} | {int(agg['cap'])} |"
        )
    return out


def summarize(path: str) -> str:
    """Return a Markdown summary (per-op breakdown + totals) of the JSONL at ``path``.

    When the optional ``pricing`` extra (genai-prices) is installed, each row is priced
    from its own model/provider/timestamp and the table gains an "est. cost" column plus
    a per-model rollup; unpriceable rows are excluded from cost (never guessed). Without
    the extra, token totals still print and the cost line reads "unavailable".

    A missing or empty file yields exactly ``No LLM calls recorded.`` so a run that made
    zero LLM calls still prints an honest, valid line.
    """
    rows = _read(path)
    if not rows:
        return "No LLM calls recorded."
    # Aggregate cache telemetry (bug 1dbe): the write-every-call-never-read run shape is only
    # visible ACROSS a run's rows, which this is the natural seam for — it already reads them
    # all. Pure observability (a WARNING), never alters the returned summary.
    from rebar.llm.structured_run import warn_if_cache_write_never_read

    warn_if_cache_write_never_read(rows, model=str(rows[0].get("model", "?")))
    pricing = _pricing_module()
    per_op: dict[str, dict[str, int]] = {}
    op_cost: dict[str, float] = {}
    op_priced: dict[str, int] = {}
    per_model: dict[str, dict[str, float]] = {}
    totals = {field: 0 for field in _FIELDS}
    calls = 0
    unpriced = 0
    # The model_refs genai-prices could not resolve. Naming them is what makes a genuinely
    # unknown model DISTINGUISHABLE from a row dropped because its id was mis-formatted: an
    # unknown one reads bare (`my-local-model`), a mis-formatted one still shows the
    # malformation (`not-a-provider:m`). "excludes N unpriced calls" alone could not tell the
    # two apart, which is how bug 2ca9 stayed invisible for a whole epic.
    unpriced_refs: list[str] = []
    total_cost = 0.0
    for row in rows:
        calls += 1
        op = str(row.get("op", "?"))
        agg = per_op.setdefault(op, {field: 0 for field in _FIELDS} | {"calls": 0})
        agg["calls"] += 1
        for field in _FIELDS:
            value = int(row.get(field, 0) or 0)
            agg[field] += value
            totals[field] += value
        if pricing is not None:
            cost = _price_row(pricing, row)
            if cost is None:
                unpriced += 1
                stored_model = row.get("model")
                # A pre-pricing row carries no model at all: unpriced, but no id to blame.
                if stored_model:
                    ref = _pricing_model_ref(str(stored_model))
                    if ref not in unpriced_refs:
                        unpriced_refs.append(ref)
            else:
                op_cost[op] = op_cost.get(op, 0.0) + cost
                op_priced[op] = op_priced.get(op, 0) + 1
                total_cost += cost
                model = str(row.get("model"))
                rollup = per_model.setdefault(model, {"calls": 0, "cost": 0.0})
                rollup["calls"] += 1
                rollup["cost"] += cost
    cost_header = " est. cost |" if pricing is not None else ""
    lines = [
        "### LLM token usage",
        "",
        f"| op | calls | input | output | cache_read | cache_write | requests |{cost_header}",
        f"| --- | ---: | ---: | ---: | ---: | ---: | ---: |{' ---: |' if cost_header else ''}",
    ]
    for op in sorted(per_op):
        agg = per_op[op]
        cost_col = (
            f" {_cost_cell(op_cost.get(op, 0.0), op_priced.get(op, 0))} |"
            if pricing is not None
            else ""
        )
        lines.append(
            f"| {op} | {agg['calls']} | {agg['input_tokens']} | {agg['output_tokens']} | "
            f"{agg['cache_read_tokens']} | {agg['cache_write_tokens']} | {agg['requests']} |"
            f"{cost_col}"
        )
    total_cost_col = (
        f" **{_cost_cell(total_cost, calls - unpriced)}** |" if pricing is not None else ""
    )
    lines.append(
        f"| **total** | **{calls}** | **{totals['input_tokens']}** | **{totals['output_tokens']}** "
        f"| **{totals['cache_read_tokens']}** | **{totals['cache_write_tokens']}** "
        f"| **{totals['requests']}** |{total_cost_col}"
    )
    if pricing is None:
        lines += ["", f"est. cost: {_PRICING_UNAVAILABLE}"]
    else:
        if unpriced:
            plural = "s" if unpriced != 1 else ""
            note = f"est. cost excludes {unpriced} unpriced call{plural}."
            if unpriced_refs:
                note += f" No pricing data for: {', '.join(sorted(unpriced_refs))}."
            lines += ["", note]
        if per_model:
            lines += [
                "",
                "#### Est. cost by model",
                "",
                "| model | calls | est. cost |",
                "| --- | ---: | ---: |",
            ]
            for model in sorted(per_model):
                rollup = per_model[model]
                lines.append(f"| {model} | {int(rollup['calls'])} | ${rollup['cost']:.4f} |")
    return "\n".join(lines + _run_shape_section(rows))


def main(argv: list[str] | None = None) -> int:
    """CLI: ``python -m rebar.llm.usage_log summarize <path>`` prints the Markdown summary."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="python -m rebar.llm.usage_log")
    sub = parser.add_subparsers(dest="cmd", required=True)
    summarize_parser = sub.add_parser("summarize", help="print a Markdown token-usage summary")
    summarize_parser.add_argument("path", help="path to the JSONL usage log")
    args = parser.parse_args(argv)
    if args.cmd == "summarize":
        sys.stdout.write(summarize(args.path) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via main() in tests
    raise SystemExit(main())
