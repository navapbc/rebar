"""Process-level token-usage sink for the live-LLM CI jobs.

``PydanticAIRunner.run`` already extracts per-call token usage (input/output/cache
tokens + request count) and attaches it as ``result["_usage"]`` (see ``runner.py``).
This module turns that in-memory value into a **durable, retrievable** record for the
two weekly, billable jobs — the external tier (``external-integration.yml``) and the
live prompt-eval (``prompt-eval.yml``) — which otherwise surface no spend at all.

Opt-in via the ``REBAR_USAGE_LOG`` env var: when it points at a path, :func:`record`
appends one JSON object per LLM call (JSONL). :func:`summarize` folds that file into a
Markdown table for ``$GITHUB_STEP_SUMMARY``; the raw JSONL is uploaded as a CI artifact.

With that var unset, a run inside a GATE SESSION falls back to ``<repo root>/.rebar/usage.jsonl``
when that directory already exists (bug aec1 — an operator's own gate run is billable, agentic,
and the one that loops, yet it recorded nothing at all). Every OTHER call with the var unset —
every normal library/test run — still makes :func:`record` a **no-op**, so the default runner
path and ``make test`` are byte-unchanged. :func:`_resolve_sink` owns that precedence.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

#: Env var naming the JSONL sink file. Unset ⇒ recording is off (the default).
ENV_VAR = "REBAR_USAGE_LOG"

#: The integer token fields ``_extract_usage()`` reports (runner.py); summed by summarize().
_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "requests")

#: The RUN-SHAPE fields :func:`run_shape` derives from a run's accumulated pydantic-ai messages.
#
# A SECOND allowlist rather than more ``_FIELDS`` because the write rules differ: ``_FIELDS`` are
# token counters every caller has, written unconditionally; these describe the SHAPE of the agent
# loop and exist only when the caller could reduce the messages, so each is written only when
# present (see :func:`record`). Before bug aec1 all seven were computed and then DISCARDED at the
# write — a 125-request loop reduced to ``tool_calls=125, tool_calls_distinct=1`` and the row
# carried neither, so the one signal separating a LOOP from BREADTH never reached the record.
# Explicit, not ``**usage`` passthrough: this module owns its durable schema, because a row is
# read back months later and what may appear in it is a decision made HERE.
_SHAPE_FIELDS = (
    "tool_calls",
    "tool_calls_distinct",
    "max_consecutive_repeat",
    "top_repeated_tool_calls",
    "request_limit",
    "tool_calls_limit",
    "finish_reason",
    "distinct_fetches",
)

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


def run_shape(
    messages: list[object],
    *,
    request_limit: int,
    tool_calls_limit: int,
) -> dict[str, Any]:
    """Reduce a run's accumulated pydantic-ai messages to its SHAPE, on EITHER outcome.

    Originally written for the raise path only (hence the name), but nothing here is
    failure-specific: the same reduction over the same accumulated messages is exactly what a
    SUCCESSFUL run needs to be readable too (bug aec1 — a success row carried no shape at all,
    so a run that succeeded after 40 near-identical tool calls was indistinguishable from one
    that succeeded in three). Reused verbatim by both paths rather than duplicated, so the two
    outcomes can never drift into reporting the same run differently.

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
    distinct_fetches: list[dict] = []
    seen_fetches: set[tuple[str, str]] = set()
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
                name = str(getattr(part, "tool_name", "") or "?")
                target = fetch_target(name, getattr(part, "args", None))
                if target is not None:
                    key = (name, target)
                    if key not in seen_fetches:
                        seen_fetches.add(key)
                        distinct_fetches.append({"tool": name, "target": target})
    return {
        **totals,
        "finish_reason": finish_reason,
        "request_limit": request_limit,
        "tool_calls_limit": tool_calls_limit,
        "distinct_fetches": distinct_fetches,
        **_repetition_summary(signatures),
    }


def shape_only(shape: dict) -> dict[str, Any]:
    """Just the :data:`_SHAPE_FIELDS` entries of a :func:`run_shape` result.

    So a caller merging the shape into its own usage dict need not reach into this module's
    private field tuple: which keys are durable schema belongs HERE, next to the only thing that
    writes them, and a caller filtering by its own inline list would be a second, drifting copy.
    It also DROPS the reducer's token totals, which are summed from the messages whereas a
    successful caller already holds the provider's own authoritative figures — merging the
    approximation over them would silently corrupt cost accounting.
    """

    return {field: shape[field] for field in _SHAPE_FIELDS if field in shape}


def tool_call_signature(name: str, args: object) -> str:
    """``tool_name:args_digest`` for one ``(name, args)`` pair — identity WITHOUT the
    argument content.

    The ONE canonicalization shared by the post-hoc message reduction
    (:func:`_tool_signature`) and the in-flight runaway guard (``agent_call``), so the
    two can never drift into hashing the same call differently. Falls back to
    ``str(args)`` when the arguments cannot be canonicalized, so a surprising arg shape
    degrades the signal rather than raising inside a live tool call or a failure path.
    """

    import hashlib

    try:
        canonical = json.dumps(args, sort_keys=True, default=str) if args is not None else ""
    except (TypeError, ValueError):
        canonical = str(args)
    digest = hashlib.sha256(canonical.encode("utf-8", "replace")).hexdigest()[:8]
    return f"{name}:{digest}"


def _tool_signature(part: object) -> str:
    """:func:`tool_call_signature` for a pydantic-ai ``ToolCallPart``-shaped object."""

    name = str(getattr(part, "tool_name", "") or "?")
    return tool_call_signature(name, getattr(part, "args", None))


def fetch_target(name: str, args: object) -> str | None:
    """The PRIMARY path/query arg of a fetch-like tool call, or ``None`` for any other tool.

    Records ONLY the primary path/query (``read_file``→its path, ``search_files``→its
    query/pattern, ``list_directory``→its path) — NEVER the full args, so no line ranges and no
    content leak into the run shape. ``args`` may be a dict, a JSON string, or an arbitrary
    object; a dict or a parseable JSON-string dict is inspected, anything else (or a parse
    failure) yields ``None``.
    """
    key_by_tool = {
        "read_file": ("path",),
        "search_files": ("query", "pattern", "regex"),
        "list_directory": ("path",),
    }
    keys = key_by_tool.get(name)
    if keys is None:
        return None
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (TypeError, ValueError):
            return None
    if not isinstance(args, dict):
        return None
    for key in keys:
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _normalize_fetch_path(path: str) -> str:
    """Strip a single leading ``./`` for suffix comparison."""
    return path[2:] if path.startswith("./") else path


def fetch_overlap(fetches: list[dict], file_impact: Iterable[str]) -> dict:
    """Overlap of the run's path-bearing fetches against a ticket's declared ``file_impact``.

    Returns ``{"covered", "total", "fraction"}``. ``total`` counts only path-bearing fetches
    (``tool == "read_file"``; search queries are ignored for path coverage). ``covered`` counts
    those whose ``target`` matches a declared path by EXACT path or by one being a SUFFIX of the
    other (a single leading ``./`` normalized away). ``fraction`` = covered/total (0.0 when
    total is 0), rounded to 4 dp.
    """
    declared = [_normalize_fetch_path(p) for p in file_impact if isinstance(p, str) and p]
    reads = [
        _normalize_fetch_path(str(f.get("target")))
        for f in fetches
        if f.get("tool") == "read_file" and f.get("target")
    ]
    total = len(reads)
    covered = 0
    for target in reads:
        if any(target == dp or target.endswith(dp) or dp.endswith(target) for dp in declared):
            covered += 1
    fraction = round(covered / total, 4) if total else 0.0
    return {"covered": covered, "total": total, "fraction": fraction}


#: Minimum sample size (trailing tool calls) before loop accusation is valid.
REPETITION_WINDOW = 24

#: Trip threshold: distinct_ratio_window at or below this reads as a loop.
REPETITION_TRIP_RATIO = 0.50


def window_distinct_ratio(signatures: Sequence[str]) -> float | None:
    """Set cardinality of the trailing :data:`REPETITION_WINDOW` signatures over the
    window size, rounded to 3 decimals — the order-insensitive loop signal that catches
    every cycle length. ``None`` when the sample is shorter than the window, so short
    runs are never accused. The SINGLE source of the trip predicate's ratio: both the
    post-hoc :func:`_repetition_summary` and the in-flight runaway guard
    (``agent_call``) call this function. :data:`REPETITION_WINDOW` is read at CALL time
    (a module-global lookup), so tests can shrink the window via monkeypatch.
    """

    if len(signatures) < REPETITION_WINDOW:
        return None
    return round(len(set(signatures[-REPETITION_WINDOW:])) / REPETITION_WINDOW, 3)


def _repetition_summary(signatures: list[str], *, top: int = 5) -> dict:
    """Loop-versus-work signal derived from the tool-call signature sequence.

    Returns a dict with four keys:
    - ``tool_calls_distinct``: cardinality of the unique signatures.
    - ``max_consecutive_repeat``: longest back-to-back repetition (1-cycle only; blind spot
      for k-cycles where k >= 2).
    - ``top_repeated_tool_calls``: ranked list of the top-N most-repeated signatures.
    - ``distinct_ratio_window``: set cardinality of the trailing REPETITION_WINDOW calls,
      divided by REPETITION_WINDOW and rounded to 3 decimals. This is order-insensitive
      and catches every cycle length (including k-cycles that ``max_consecutive_repeat``
      cannot see). ``None`` when ``len(signatures) < REPETITION_WINDOW`` (insufficient
      sample to accuse a loop).
    """

    if not signatures:
        return {
            "tool_calls_distinct": 0,
            "max_consecutive_repeat": 0,
            "top_repeated_tool_calls": [],
            "distinct_ratio_window": None,
        }
    counts: dict[str, int] = {}
    for sig in signatures:
        counts[sig] = counts.get(sig, 0) + 1
    longest = current = 1
    for previous, sig in itertools.pairwise(signatures):
        current = current + 1 if sig == previous else 1
        longest = max(longest, current)
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top]
    window_ratio = window_distinct_ratio(signatures)
    return {
        "tool_calls_distinct": len(counts),
        "max_consecutive_repeat": longest,
        "top_repeated_tool_calls": [{"signature": sig, "count": n} for sig, n in ranked if n > 1],
        "distinct_ratio_window": window_ratio,
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
        "distinct_ratio_window={distinct_ratio_window} "
        "top_repeats={top_repeated_tool_calls}"
    ).format(
        requests=usage.get("requests"),
        tool_calls=usage.get("tool_calls"),
        tool_calls_distinct=usage.get("tool_calls_distinct"),
        max_consecutive_repeat=usage.get("max_consecutive_repeat"),
        distinct_ratio_window=usage.get("distinct_ratio_window"),
        top_repeated_tool_calls=usage.get("top_repeated_tool_calls"),
    )


def _repo_root_for_default_sink() -> str | None:
    """The repo root the default gate sink lives under, or None when it cannot be determined.

    Its own named function so :func:`_resolve_sink` reads as one decision rather than an inline
    try/except, and so a test can pin the root without a chdir. ``rebar.config`` is imported
    LAZILY because this module is deliberately stdlib-only at import time (module docstring): it
    is imported from the runner's hot path and from an ``except`` block. Never raises — root
    discovery walks the filesystem and consults git, either of which can fail for reasons
    unrelated to the call being measured, and telemetry that raised there would break the very
    call it exists to observe.
    """

    try:
        from rebar import config as _config

        return str(_config.repo_root())
    except Exception:  # noqa: BLE001 — see docstring: telemetry must never raise into the caller
        return None


def _resolve_sink() -> str | None:
    """The JSONL path :func:`record` should append to, or None when recording is off.

    Two sources, in strict precedence order:

    1. ``REBAR_USAGE_LOG`` — an operator/CI pointing the sink somewhere explicit. Used verbatim,
       and it wins, because an explicit path is an instruction, not a hint.
    2. The DEFAULT GATE SINK ``<repo root>/.rebar/usage.jsonl`` — but ONLY inside a gate session.
       Before bug aec1 the env var was the only source, so a normal operator gate run (a
       ``review-plan``, a ``verify-completion``) recorded NOTHING and its spend and run shape were
       gone the moment the process exited. A gate run is exactly the run worth measuring: it is
       billable, it is agentic, and it is the one that loops.

    The gate-session condition keeps this from becoming "rebar writes files during library use":
    a call OUTSIDE a gate session with the env var unset resolves to None and :func:`record`
    no-ops byte-for-byte as it always has — load-bearing, since ``import rebar`` and
    ``make test`` must not start dropping JSONL into a checkout. The ``.rebar`` directory must
    already EXIST for the same reason: its presence is the signal that this checkout is a rebar
    store, and we create nothing.
    """

    # Resolve the explicit sink override through the owned config seam
    # (``config.resolve_usage_log_sink`` reads ``REBAR_USAGE_LOG`` LIVE per call) — this is a
    # fixed, operator-facing var, so it belongs in the named table in docs/env-vars.md and the
    # env-var registry generator (scripts/gen_env_registry.py) names it at the composition seam.
    from rebar import config as _root_config

    path = _root_config.resolve_usage_log_sink()
    if path:
        return path
    # Lazy for the same stdlib-only reason as `_repo_root_for_default_sink`. Imported through
    # `rebar.llm.config`, NOT from `gate_context` where it is defined: the suite's monkeypatch
    # targets name `rebar.llm.config.<name>`, so a consumer reading the new module directly would
    # keep working while those patches silently stopped applying. `test_gate_context_seam.py`
    # enforces this for every module under src/.
    from rebar.llm.gate_context import in_gate_session

    if not in_gate_session():
        return None
    root = _repo_root_for_default_sink()
    if root is None:
        return None
    default = os.path.join(root, ".rebar", "usage.jsonl")
    return default if os.path.isdir(os.path.join(root, ".rebar")) else None


def record(
    usage: dict,
    *,
    op: str,
    model: str | None = None,
    provider: str | None = None,
    step: str | None = None,
    model_class: str | None = None,
    outcome: str = OUTCOME_OK,
    duration_s: float | None = None,
    ticket: str | None = None,
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

    ``duration_s`` and ``ticket`` are optional and written only when supplied. They answer the
    two questions the token counters cannot: how long the spend took — a run can be cheap and
    still be a twenty-minute stall — and WHICH work it belongs to, which is what makes spend
    comparable across tickets rather than only across ops.

    No-op when :func:`_resolve_sink` finds no sink (the default outside a gate session) or
    ``usage`` is empty. Best-effort: a telemetry sink must never break the LLM call path, so any
    write error is logged and swallowed rather than raised into the runner.
    """
    path = _resolve_sink()
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
    # Same omission pattern as model/step/model_class above, but here it is load-bearing rather
    # than tidy: defaulting to 0 would make a caller that reduced no messages claim
    # `tool_calls: 0`, the strong false statement "this run used no tools", which later
    # aggregates would then sum as fact. An absent key says the only true thing — not measured.
    # Values pass through unconverted (`top_repeated_tool_calls` is a list, `finish_reason` a
    # string), so a blanket int() would be wrong.
    for field in _SHAPE_FIELDS:
        if field in usage:
            row[field] = usage[field]
    if duration_s is not None:
        row["duration_s"] = float(duration_s)
    if ticket:
        row["ticket"] = ticket
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
    *,
    duration_s: float | None = None,
    ticket: str | None = None,
) -> None:
    """Append the one usage row for an LLM call that RAISED (bug 8455).

    Positional rather than keyword-only purely so the runner's call fits its line budget — the
    caller sits under a hard module-size gate; the arguments are ``(accumulated pydantic-ai
    messages, op/prompt label, model that ran, request limit, effective max iterations)``.
    ``duration_s``/``ticket`` (bug aec1) are appended as KEYWORD-only with defaults rather than
    extending that positional run, so every existing call site keeps working unchanged and a
    future reader cannot silently transpose them into the ints above.

    ``PydanticAIRunner.run`` reaches :func:`record` only on its success path — its except spine
    (``interpret_failure``) always re-raises — so before 8455 a call that burned input tokens and
    then failed (budget exceeded, provider 400/outage, unrepairable output, rejected sampling
    parameter) left NO row at all, and a run whose late steps failed read back as a short but
    perfectly clean log. Called from the runner's OWN except block, this restores the missing row.

    The row is deliberately the SAME shape a successful one has — same identity (``op``, ``step``,
    ``model_class``, ``model``, ``provider``) and the same token-counter field set — so
    :func:`summarize` folds it and the failed call's spend lands in the totals, which is the whole
    point. It differs only by an explicit ``outcome`` of :data:`OUTCOME_FAILED`. Counters come from
    :func:`run_shape`, which reads what was actually burned off the accumulated pydantic-ai
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
        kwargs: dict[str, Any] = {
            "op": op,
            "model": model,
            "provider": infer_provider(model) if model else None,
            "step": step_id,
            "model_class": declared_model_class(model_token) or model,
            "outcome": OUTCOME_FAILED,
        }
        if duration_s is not None:
            kwargs["duration_s"] = duration_s
        if ticket is not None:
            kwargs["ticket"] = ticket
        record(
            run_shape(messages, request_limit=request_limit, tool_calls_limit=max(8, eff_max_iter)),
            **kwargs,
        )
    except Exception as exc:  # noqa: BLE001 — telemetry must never mask the provider's error
        logger.warning("usage-log: failed-call record failed for op=%s: %s", op, exc)


# ── Reporting surface moved to usage_report.py (module-size split) ────────────────────
#
# The write path lives here; parse/price/summarize live in :mod:`rebar.llm.usage_report`.
# This lazy forwarder keeps every existing reference — the workflows'
# ``python -m rebar.llm.usage_log summarize``, docs, and tests addressing
# ``usage_log.summarize`` et al. — valid without a module-level import cycle
# (usage_report imports ``_FIELDS`` from this module at import time).
_REPORT_EXPORTS = frozenset(
    {
        "summarize",
        "main",
        "usage_kwargs",
        "_read",
        "_pricing_module",
        "_pricing_model_ref",
        "_price_row",
        "_cost_cell",
        "_run_shape_section",
        "_TOKEN_FIELDS",
        "_PRICING_UNAVAILABLE",
    }
)


def __getattr__(name: str) -> object:
    if name in _REPORT_EXPORTS:
        from rebar.llm import usage_report

        return getattr(usage_report, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if __name__ == "__main__":  # pragma: no cover - exercised via usage_report main() in tests
    from rebar.llm.usage_report import main

    raise SystemExit(main())
