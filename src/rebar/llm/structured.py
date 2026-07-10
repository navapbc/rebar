"""The structured-output reliability stack (1268) — deterministic where possible.

Workflow LLM output feeds DETERMINISTIC steps, so schema-conformance is load-bearing.
The research (docs/experiments/workflow-remediation-pocs/structured-output-research.md)
converged on a LAYERED, deterministic-where-possible stack that RETIRES the
second-interpreter LLM (using another non-deterministic model to "fix" output is a
recognized anti-pattern):

  (1) provider-native CONSTRAINED decoding / strict json_schema where the provider
      offers it (:func:`output_mode` -> NativeOutput), else cross-provider
      PromptedOutput. PromptedOutput is also the mode used whenever extended thinking is
      on, because Anthropic currently 400s when extended thinking is combined with a
      native/forced output constraint ("Thinking may not be enabled when tool_choice
      forces tool use") — a live API constraint on the current pydantic_ai output modes,
      not a relic of any earlier forced-tool mechanism;
  (2) DETERMINISTIC tolerant parse of near-miss output (:func:`tolerant_parse`, via
      json-repair — NO LLM): strips markdown fences, repairs trailing commas / unclosed
      braces / smart quotes;
  (3) Pydantic validation + NORMALIZING validators, with numeric/length BOUNDS in the
      validators (NOT the JSON Schema, to stay inside Anthropic's strict-grammar
      subset) — :func:`validate_to`;
  (4) a SINGLE bounded retry to the SAME model with the validation error fed back is
      the accepted fallback (configured on the agent; the deterministic Pydantic
      validator is the arbiter, not a second model).

Anthropic ``stop_reason`` in {refusal, max_tokens} is surfaced as a clear error
(:func:`check_stop_reason`) rather than silently treated as empty output. json-repair
is a lean (no-LLM) dependency; pydantic is imported lazily so ``import rebar.llm`` stays
stdlib-only.
"""

from __future__ import annotations

import json
import re
from typing import Any

from rebar.llm.errors import StructuredOutputError, UnretryableOutputError

# Providers whose Pydantic AI profile offers provider-enforced constrained decoding
# (strict json_schema / native Structured Outputs). Anthropic shipped a strict path in
# early 2026 but Pydantic AI may still route it through ToolOutput per the model
# profile — so we keep Anthropic on the safe PromptedOutput path (which, with the
# tolerant parse below, is reliable) until the profile is confirmed. This is a small
# capability map, NOT per-provider behaviour code.
_NATIVE_OUTPUT_PROVIDERS = frozenset({"openai", "google-gla", "google-vertex", "groq"})

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def output_mode(model_cls, model_str: str, *, thinking: bool = False):
    """Select the Pydantic AI output mode for ``model_cls`` (layer 1).

    NativeOutput for providers that enforce a strict json_schema; PromptedOutput for
    everyone else (the broadest, and — crucially — not a constrained/native output mode,
    so it stays compatible with Claude extended thinking, which pydantic_ai's default
    ToolOutput mode is not). ``thinking`` forces PromptedOutput regardless of provider:
    pairing extended thinking with a native/forced output constraint is the documented
    Anthropic 400, so the prompted mode is the only thinking-compatible choice."""
    from pydantic_ai import NativeOutput, PromptedOutput

    provider = model_str.split(":", 1)[0] if ":" in model_str else ""
    if not thinking and provider in _NATIVE_OUTPUT_PROVIDERS:
        return NativeOutput(model_cls)
    return PromptedOutput(model_cls)


def schema_directive(model_cls) -> str:
    """The JSON-schema directive appended to a PROMPTED structured call (the json-repair
    path) so the model is TOLD the exact output shape.

    The prompted path generates free text and then tolerantly parses it — but the model
    can only emit the right shape if it knows the schema. Without this, a model that knows
    a field only by prose (e.g. "severity ATTRIBUTES") guesses the JSON keys (``attributes``
    instead of ``severity_attributes``, a ``findings`` wrapper instead of ``verifications``),
    and tolerant parsing silently drops the unrecognized keys → an EMPTY validated object.
    (NativeOutput / PromptedOutput-as-output_type inject this automatically; the manual
    json-repair path must do it explicitly — the gap that left plan-review verifications all
    ``no-verification``.)"""
    import json

    schema = json.dumps(model_cls.model_json_schema(), separators=(",", ":"))
    return (
        "Respond with ONLY a single JSON object conforming to this JSON Schema "
        "(use these EXACT keys; no prose, no markdown fence):\n" + schema
    )


def _first_json_object(text: str) -> Any | None:
    """The first balanced ``{…}`` object in ``text`` that PARSES as JSON (string-aware
    brace matching), or None.

    Advancing to the NEXT ``{`` when a candidate region fails to parse is what makes
    prose-wrapped JSON robust: a preamble containing a non-JSON brace — e.g. a GitHub
    Actions ``${{ … }}`` expression before the real object — no longer aborts the scan
    and loses the trailing object (bug 67ee / messianic-wild-dassie: the abort dropped the
    verdict, json-repair then mangled the prose into a list, and the completion-verifier
    close gate fail-closed with "got list"). Preferring the first PARSING object still
    makes multi-object output DETERMINISTIC (a model that emits a draft then a correction
    does not get the last-wins surprise json-repair gives)."""
    start = text.find("{")
    while start >= 0:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break  # this region isn't valid JSON — advance to the next '{'
        start = text.find("{", start + 1)
    return None  # no balanced object parses (e.g. a truncated stream)


def tolerant_parse(text: str, schema: Any = None) -> Any:
    """Deterministically parse near-miss model output into a Python object (layer 2).

    Order (all NO-LLM): strict ``json.loads`` → fenced block → the FIRST balanced
    ``{…}`` object (deterministic, first-wins on multi-object / prose-wrapped) →
    ``json-repair`` (trailing commas, unclosed braces, single/smart quotes) as the
    last resort. Raises :class:`StructuredOutputError` only when nothing is parseable
    (e.g. a truncated stream with no balanced object — caught upstream by the
    ``max_tokens`` stop-reason guard).

    ``schema`` (story drake): when a Pydantic model is supplied, json-repair is given it
    for schema-GUIDED deterministic repair (it can coerce/fill toward the target shape
    before any LLM reask). Best-effort: a schema-guided repair that raises falls back to
    the schema-less call, so it never regresses today's behavior."""
    if not isinstance(text, str) or not text.strip():
        raise StructuredOutputError("empty model output (nothing to parse)")
    candidates = [text]
    fenced = _FENCE_RE.search(text)
    if fenced:
        candidates.insert(0, fenced.group(1))
    for cand in candidates:
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            continue
    # Prefer the first complete object before resorting to (last-wins) repair.
    for cand in candidates:
        obj = _first_json_object(cand)
        if obj is not None:
            return obj
    # json-repair: deterministic best-effort repair of malformed JSON (no model call).
    try:
        from json_repair import repair_json
    except ImportError as exc:  # pragma: no cover - json-repair ships with [agents]
        raise StructuredOutputError(
            f"output is not valid JSON and json-repair is unavailable: {text[:120]!r}"
        ) from exc
    for cand in candidates:
        repaired = _repair(repair_json, cand, schema)
        if repaired not in ("", None, [], {}):
            return repaired
    raise StructuredOutputError(f"output could not be parsed even after repair: {text[:120]!r}")


def _repair(repair_json, cand: str, schema: Any):
    """Schema-guided json-repair (story drake) with a safe fallback: when a ``schema`` is
    supplied, pass it so json-repair coerces toward the target shape; if that raises (a
    json-repair edge case), fall back to the schema-less repair so behavior never regresses."""
    if schema is not None:
        try:
            return repair_json(cand, return_objects=True, schema=schema)
        except Exception:  # noqa: BLE001 — schema-guided repair is best-effort; fall back
            pass
    return repair_json(cand, return_objects=True)


def validate_to(model_cls, data: Any):
    """Validate ``data`` against the Pydantic ``model_cls`` (layer 3), surfacing a
    validation failure as a :class:`StructuredOutputError` whose message carries the
    field errors — exactly what a bounded retry (layer 4) feeds back to the model.
    BOUNDS live in the model's validators (kept out of the JSON Schema to stay in
    Anthropic's strict-grammar subset); they fire here."""
    from pydantic import ValidationError

    # Unwrap a top-level JSON array carrying exactly one object: some models emit their
    # lone structured result wrapped in a top-level JSON array instead of the bare object
    # — either as a bare one-element list, or (reasoning models) as an array that
    # concatenates echoed intermediate arrays it reasoned over with the real result as
    # the sole dict element, e.g. [["open","in_progress"], {"verdict":"PASS", …}]. Both
    # otherwise deterministically fail validation ("got list") and, for the
    # completion-verifier, block a close fail-closed (bug artsy-chain-hold /
    # dash-lure-slag / slit-rubble-braid). We unwrap when the top-level list holds
    # exactly one dict element (regardless of accompanying non-dict noise); a list with
    # zero dicts, or two-or-more dicts (genuinely ambiguous — which object?), stays
    # ambiguous and is still rejected below. Only the top-level elements are considered —
    # we never recurse into or flatten nested lists.
    if isinstance(data, list):
        dict_elems = [item for item in data if isinstance(item, dict)]
        if len(dict_elems) == 1:
            data = dict_elems[0]

    if not isinstance(data, dict):
        raise StructuredOutputError(
            f"structured output must be a JSON object, got {type(data).__name__}"
        )
    try:
        return model_cls(**data)
    except ValidationError as exc:
        raise StructuredOutputError(f"structured output failed validation: {exc}") from exc


def parse_structured(text: str, model_cls):
    """The deterministic layers (2)+(3) as one call: tolerant-parse then validate.
    Returns a validated ``model_cls`` instance, or raises :class:`StructuredOutputError`
    (the signal a caller turns into a single bounded retry — layer 4)."""
    return validate_to(model_cls, tolerant_parse(text, schema=model_cls))


# Stop/finish reasons that are NOT a usable structured answer and must never be read
# as empty/clean output. Keyed by BOTH the raw Anthropic ``stop_reason`` and Pydantic
# AI's provider-agnostic normalized ``finish_reason`` (Literal[stop, length,
# content_filter, tool_call, error]) so the check works without per-provider code.
_BAD_STOP_REASONS = {
    "refusal": "the model refused to answer (stop_reason=refusal)",
    "max_tokens": "the model hit the token cap before finishing (stop_reason=max_tokens) — "
    "raise max_tokens or split the step",
    "length": "the model hit the token cap before finishing (finish_reason=length) — its "
    "output is TRUNCATED; raise max_tokens or split the step",
    "content_filter": "the response was blocked (finish_reason=content_filter)",
    "error": "the model run ended in an error (finish_reason=error)",
}


# Stop reasons whose failure re-running the SAME call reproduces deterministically — a
# TRUNCATED turn (hit the output-token cap) or a refused / filtered turn is a complete,
# unusable response, NOT a near-miss the model can fix when handed the validation error.
# These raise UnretryableOutputError so the bounded retry FAST-FAILS instead of re-paying
# the full call. ``error`` (a transient provider/run error) stays retryable.
_UNRETRYABLE_STOP_REASONS = frozenset({"refusal", "max_tokens", "length", "content_filter"})


def check_stop_reason(stop_reason: str | None) -> None:
    """Raise on a stop/finish reason that signals NO usable output (Anthropic
    {refusal, max_tokens} or the normalized {length, content_filter, error}). A normal
    ``stop``/``tool_call``/``end_turn``/``None`` passes. Keeps a TRUNCATED or refused
    turn from being read as a clean (empty) structured result — and from being
    silently "repaired" into a plausible-but-wrong object by json-repair.

    A truncation (``max_tokens``/``length``), ``refusal``, or ``content_filter`` raises
    :class:`UnretryableOutputError` (re-running reproduces it — fast-fail, don't retry);
    a transient ``error`` raises the retryable :class:`StructuredOutputError`."""
    if stop_reason in _UNRETRYABLE_STOP_REASONS:
        raise UnretryableOutputError(_BAD_STOP_REASONS[stop_reason])
    if stop_reason in _BAD_STOP_REASONS:
        raise StructuredOutputError(_BAD_STOP_REASONS[stop_reason])


# Default bounded retry budget for the structured-output path (layer 4): ONE retry to
# the SAME model with the validation error fed back (Pydantic AI's default budget is 1;
# the research recommends raising it to ~2). The deterministic validator is the arbiter.
OUTPUT_RETRIES = 2
