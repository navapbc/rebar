"""Process-level token-usage sink for the live-LLM CI jobs.

``PydanticAIRunner.run`` already extracts per-call token usage (input/output/cache
tokens + request count) and attaches it as ``result["_usage"]`` (see ``runner.py``).
This module turns that in-memory value into a **durable, retrievable** record for the
two weekly, billable jobs — the external tier (``external-integration.yml``) and the
live prompt-eval (``prompt-eval.yml``) — which otherwise surface no spend at all.

Opt-in via the ``REBAR_USAGE_LOG`` env var: when it points at a path, :func:`record`
appends one JSON object per LLM call (JSONL). :func:`summarize` folds that file into a
Markdown table for ``$GITHUB_STEP_SUMMARY``; the raw JSONL is uploaded as a CI artifact.
When the env var is unset (every normal library/test run) :func:`record` is a **no-op**,
so the default runner path and ``make test`` are byte-unchanged.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

#: Env var naming the JSONL sink file. Unset ⇒ recording is off (the default).
ENV_VAR = "REBAR_USAGE_LOG"

#: The integer token fields ``_extract_usage()`` reports (runner.py); summed by summarize().
_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "requests")

#: The four billable token fields genai-prices consumes (``requests`` is not billable).
_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")

#: The cost footer when the optional ``pricing`` extra is not installed.
_PRICING_UNAVAILABLE = "unavailable (install rebar[pricing])"

# Row-level call outcome, written under the ``"outcome"`` key on EVERY row (bug 8455).
#
# Before 8455 a call that RAISED — budget exceeded, provider 400/outage, unretryable output —
# left no row at all, because `PydanticAIRunner.run()` only reached its `record()` call on the
# success path: the except spine (`interpret_failure`) always re-raises. So a run whose late
# steps failed read back as a short but perfectly clean log, silently under-reporting the input
# tokens those failed calls had already burned.
#
# The discriminator is an EXPLICIT field on BOTH kinds of row, never "failure = some field is
# missing": an absent field is already the truthful encoding of "not applicable" here (see
# `record`'s omission pattern for model/step/model_class), so overloading absence to also mean
# "this call failed" would make the two indistinguishable for a reader.
OUTCOME_OK = "ok"
OUTCOME_FAILED = "failed"
_FAILURE_MESSAGE_SINK: ContextVar[list[object] | None] = ContextVar(
    "rebar_failure_message_sink", default=None
)


@contextmanager
def collect_failure_messages(sink: list[object]) -> Iterator[None]:
    """Make ``sink`` available to every nested pydantic-ai attempt."""

    token = _FAILURE_MESSAGE_SINK.set(sink)
    try:
        yield
    finally:
        _FAILURE_MESSAGE_SINK.reset(token)


# The workflow step whose execution is in progress, as (step id, RAW declared `model:` token).
# Threaded as a ContextVar because `RunRequest` — the object that reaches the runner — is
# constructed at 25 sites under `src/rebar`, most with NO step context at all (a spec scan and an
# enrich pass are not workflow steps). A required field would churn all 25; an optional one would
# leave ~24 sites passing None forever. A ContextVar costs zero churn and scopes truthfully: a
# call made OUTSIDE a step simply has no step id. Precedent: `_active_gate_config`
# (`llm/config.py`), itself mirroring the read-root ContextVars in `llm/gate_context.py`.
#
# WHY THE RAW TOKEN RIDES ALONG rather than being recovered later: by the time the runner records
# usage the class is GONE — `resolve_model` has collapsed it and `resolve_model_string` returns a
# bare model id — and reverse lookup is UNSOUND because two classes may resolve to the same model.
_ACTIVE_STEP: ContextVar[tuple[str, str | None] | None] = ContextVar(
    "rebar_active_workflow_step", default=None
)


@contextmanager
def step_identity(step_id: str, model_token: str | None) -> Iterator[None]:
    """Bind the executing workflow step's identity for the dynamic extent of the block.

    ``model_token`` is the RAW declared token exactly as the document wrote it (a prompt step's
    ``model:``, a batch step's ``model_ladder[0]``) — never a resolved model id. Dropped on exit,
    so it never leaks into a call made outside a step.
    """

    token = _ACTIVE_STEP.set((step_id, model_token))
    try:
        yield
    finally:
        _ACTIVE_STEP.reset(token)


def active_step() -> tuple[str, str | None] | None:
    """The executing step as ``(step_id, raw model token)``, or None outside any step."""

    return _ACTIVE_STEP.get()


def declared_model_class(model_token: str | None) -> str | None:
    """``model_token`` when it names a model CLASS, else None.

    A literal model id is not a class, and recording it as one would recreate the very ambiguity
    this attribution exists to remove. The import is lazy because ``model_classes`` imports
    ``llm.config``, which would otherwise drag a dependency chain into this stdlib-only sink.
    """

    from rebar.llm.model_classes import CLASS_NAMES

    return model_token if model_token in CLASS_NAMES else None


@contextmanager
def capture_attempt_messages() -> Iterator[None]:
    """Append one pydantic-ai attempt to the active failure counter source."""

    from pydantic_ai import capture_run_messages

    with capture_run_messages() as messages:
        try:
            yield
        finally:
            sink = _FAILURE_MESSAGE_SINK.get()
            if sink is not None:
                sink.extend(messages)


def failure_usage(
    messages: list[object],
    *,
    request_limit: int,
    tool_calls_limit: int,
) -> dict[str, int | str | None]:
    """Return safe counters from pydantic-ai messages when a run raises.

    Prompts, response text, tool arguments, and tool results are deliberately
    excluded so the payload is safe for a durable gate-error record.

    **Repetition signals.** A step-budget exhaustion looks identical in the counters
    whether the agent did a lot of legitimate work or span in a loop, and the step
    count provably cannot tell them apart (``request_limit`` is half
    ``tool_calls_limit``, so a one-tool-call-per-turn loop trips the request ceiling
    first — exactly like careful sequential work). So each tool call is reduced to a
    ``(tool_name, sha256(args)[:8])`` SIGNATURE and summarized: a run with many calls
    but few distinct signatures is looping; one with many distinct signatures is
    exploring. The arguments are HASHED, never recorded, so the privacy contract above
    is unchanged — a digest plus the tool name (a fixed vocabulary) carries the signal
    without the content.
    """

    totals = {
        "requests": 0,
        "tool_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    finish_reason: str | None = None
    signatures: list[str] = []
    for message in messages:
        if type(message).__name__ == "ModelResponse":
            totals["requests"] += 1
            finish_reason = getattr(message, "finish_reason", None) or finish_reason
            usage = getattr(message, "usage", None)
            for field in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
            ):
                totals[field] += int(getattr(usage, field, 0) or 0)
        for part in getattr(message, "parts", ()) or ():
            if type(part).__name__ == "ToolCallPart":
                totals["tool_calls"] += 1
                signatures.append(_tool_signature(part))
    return {
        **totals,
        "finish_reason": finish_reason,
        "request_limit": request_limit,
        "tool_calls_limit": tool_calls_limit,
        **_repetition_summary(signatures),
    }


def _tool_signature(part: object) -> str:
    """``tool_name:args_digest`` — identity WITHOUT the argument content.

    Falls back to the tool name alone when the arguments cannot be canonicalized, so a
    surprising arg shape degrades the signal rather than raising inside a failure path.
    """

    import hashlib

    name = str(getattr(part, "tool_name", "") or "?")
    raw = getattr(part, "args", None)
    try:
        canonical = json.dumps(raw, sort_keys=True, default=str) if raw is not None else ""
    except (TypeError, ValueError):
        canonical = str(raw)
    digest = hashlib.sha256(canonical.encode("utf-8", "replace")).hexdigest()[:8]
    return f"{name}:{digest}"


def _repetition_summary(signatures: list[str], *, top: int = 5) -> dict:
    """Loop-versus-work signal derived from the tool-call signature sequence."""

    if not signatures:
        return {
            "tool_calls_distinct": 0,
            "max_consecutive_repeat": 0,
            "top_repeated_tool_calls": [],
        }
    counts: dict[str, int] = {}
    for sig in signatures:
        counts[sig] = counts.get(sig, 0) + 1
    longest = current = 1
    for previous, sig in zip(signatures, signatures[1:], strict=False):
        current = current + 1 if sig == previous else 1
        longest = max(longest, current)
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top]
    return {
        "tool_calls_distinct": len(counts),
        "max_consecutive_repeat": longest,
        "top_repeated_tool_calls": [{"signature": sig, "count": n} for sig, n in ranked if n > 1],
    }


def format_repetition(usage: dict) -> str:
    """Render the repetition signals as one log field.

    Lives here, next to the code that PRODUCES the summary, so the runner never
    has to know the shape of the diagnostic dict — it logs one interpolated
    value instead of unpacking five keys it does not own.
    """

    return (
        "requests={requests} tool_calls={tool_calls} "
        "distinct={tool_calls_distinct} "
        "max_consecutive_repeat={max_consecutive_repeat} "
        "top_repeats={top_repeated_tool_calls}"
    ).format(
        requests=usage.get("requests"),
        tool_calls=usage.get("tool_calls"),
        tool_calls_distinct=usage.get("tool_calls_distinct"),
        max_consecutive_repeat=usage.get("max_consecutive_repeat"),
        top_repeated_tool_calls=usage.get("top_repeated_tool_calls"),
    )


def record(
    usage: dict,
    *,
    op: str,
    model: str | None = None,
    provider: str | None = None,
    step: str | None = None,
    model_class: str | None = None,
    outcome: str = OUTCOME_OK,
) -> None:
    """Append one usage record for a single LLM call to ``$REBAR_USAGE_LOG`` (JSONL).

    ``model``/``provider`` (when known) and a UTC ISO-8601 timestamp — stamped here, not
    by the caller — make the row priceable later by :func:`summarize`'s optional
    genai-prices integration (historical prices are looked up by timestamp).

    ``step``/``model_class`` (when known) make the row ATTRIBUTABLE to a workflow step. ``op``
    alone cannot: it is the PROMPT name, and one prompt may serve several steps —
    ``code-review-base`` is used both by the ``base`` step and as the ``round_a``/``round_b``
    batch finder — so without ``step`` no record can be tied to the declaration that chose its
    model. ``model_class`` is the raw declared token when it names a class (see
    :func:`declared_model_class`), which is what makes "declared X, ran Y" visible in one row.

    ``outcome`` is :data:`OUTCOME_OK` (the default, so every pre-8455 caller keeps its exact
    behaviour) or :data:`OUTCOME_FAILED` for the row the runner writes when a call RAISED. It is
    written unconditionally — unlike the optional identity fields above — because the whole point
    is that a reader can tell the two apart without inferring anything from an absence.

    No-op when the env var is unset (the default) or ``usage`` is empty. Best-effort: a
    telemetry sink must never break the LLM call path, so any write error is logged and
    swallowed rather than raised into the runner.
    """
    # Read via the string literal (== ENV_VAR) so the env-var registry generator
    # (scripts/gen_env_registry.py) names REBAR_USAGE_LOG rather than listing it as an
    # opaque non-literal read — this is a fixed, operator-facing var, so it belongs in
    # the named table in docs/env-vars.md.
    path = os.environ.get("REBAR_USAGE_LOG")
    if not path or not usage:
        return
    row: dict[str, object] = {"op": op, "outcome": outcome}
    if model:
        row["model"] = model
    if provider:
        row["provider"] = provider
    # Same omission pattern as model/provider above: an absent value is simply not written,
    # rather than written as null. A row without `step` means "this call was not made inside a
    # workflow step" — the truthful answer for a spec scan or an enrich pass.
    if step:
        row["step"] = step
    if model_class:
        row["model_class"] = model_class
    row["timestamp"] = datetime.now(timezone.utc).isoformat()
    for field in _FIELDS:
        row[field] = int(usage.get(field, 0) or 0)
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")
    except OSError as exc:  # pragma: no cover - telemetry must not fail a run
        logger.warning("usage-log record failed for op=%s: %s", op, exc)


def record_failure(
    messages: list[object],
    op: str,
    model: str | None,
    request_limit: int,
    eff_max_iter: int,
) -> None:
    """Append the one usage row for an LLM call that RAISED (bug 8455).

    Positional rather than keyword-only purely so the runner's call fits its line budget — the
    caller sits under a hard module-size gate; the arguments are ``(accumulated pydantic-ai
    messages, op/prompt label, model that ran, request limit, effective max iterations)``.

    ``PydanticAIRunner.run`` reaches :func:`record` only on its success path — its except spine
    (``interpret_failure``) always re-raises — so before 8455 a call that burned input tokens and
    then failed (budget exceeded, provider 400/outage, unrepairable output, rejected sampling
    parameter) left NO row at all, and a run whose late steps failed read back as a short but
    perfectly clean log. Called from the runner's OWN except block, this restores the missing row.

    The row is deliberately the SAME shape a successful one has — same identity (``op``, ``step``,
    ``model_class``, ``model``, ``provider``) and the same token-counter field set — so
    :func:`summarize` folds it and the failed call's spend lands in the totals, which is the whole
    point. It differs only by an explicit ``outcome`` of :data:`OUTCOME_FAILED`. Counters come from
    :func:`failure_usage`, which reads what was actually burned off the accumulated pydantic-ai
    messages; ``tool_calls_limit`` mirrors ``interpret_failure``'s own ``max(8, eff_max_iter)`` so
    the two diagnostics of one failure agree. ``step``/``model_class`` resolve here because
    :func:`step_identity` wraps the WHOLE step execution, the raise included.

    Wholly best-effort — and load-bearingly so: the caller is an ``except`` block whose one job is
    to re-raise the provider's exception unchanged, so an error escaping from telemetry would
    REPLACE that exception and destroy the real diagnosis. Everything here is therefore wrapped
    (not just the write, which :func:`record` already guards), including the lazy provider/class
    lookups.
    """

    try:
        from rebar.llm.config import infer_provider

        step = active_step()
        step_id, model_token = step if step is not None else (None, None)
        record(
            failure_usage(
                messages, request_limit=request_limit, tool_calls_limit=max(8, eff_max_iter)
            ),
            op=op,
            model=model,
            provider=infer_provider(model) if model else None,
            step=step_id,
            model_class=declared_model_class(model_token),
            outcome=OUTCOME_FAILED,
        )
    except Exception as exc:  # noqa: BLE001 — telemetry must never mask the provider's error
        logger.warning("usage-log: failed-call record failed for op=%s: %s", op, exc)


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
    return "\n".join(lines)


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
