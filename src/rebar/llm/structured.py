"""The structured-output reliability stack (1268) — deterministic where possible.

Workflow LLM output feeds DETERMINISTIC steps, so schema-conformance is load-bearing.
The research (docs/experiments/workflow-remediation-pocs/structured-output-research.md)
converged on a LAYERED, deterministic-where-possible stack that RETIRES the
second-interpreter LLM (using another non-deterministic model to "fix" output is a
recognized anti-pattern):

  (1) provider-native CONSTRAINED decoding / strict json_schema where the provider
      offers it (:func:`output_mode` -> NativeOutput), else cross-provider
      PromptedOutput — NEVER forced-tool ToolOutput when extended thinking is on
      (Anthropic 400: "Thinking may not be enabled when tool_choice forces tool use");
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

from rebar.llm.errors import StructuredOutputError

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
    everyone else (the broadest, and — crucially — NOT a forced tool call, so it is
    compatible with Claude extended thinking, which the default ToolOutput is not).
    ``thinking`` forces PromptedOutput regardless of provider (forced-tool/native
    constraint + thinking is the documented 400)."""
    from pydantic_ai import NativeOutput, PromptedOutput

    provider = model_str.split(":", 1)[0] if ":" in model_str else ""
    if not thinking and provider in _NATIVE_OUTPUT_PROVIDERS:
        return NativeOutput(model_cls)
    return PromptedOutput(model_cls)


def _first_json_object(text: str) -> Any | None:
    """The FIRST balanced ``{…}`` object in ``text`` (string-aware brace matching),
    parsed — or None. Preferring the first complete object makes multi-object output
    DETERMINISTIC (a model that emits a draft then a correction does not get the
    last-wins surprise json-repair gives) and cleanly handles prose-wrapped JSON."""
    start = text.find("{")
    if start < 0:
        return None
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
                    return None
    return None  # no balanced object (e.g. a truncated stream)


def tolerant_parse(text: str) -> Any:
    """Deterministically parse near-miss model output into a Python object (layer 2).

    Order (all NO-LLM): strict ``json.loads`` → fenced block → the FIRST balanced
    ``{…}`` object (deterministic, first-wins on multi-object / prose-wrapped) →
    ``json-repair`` (trailing commas, unclosed braces, single/smart quotes) as the
    last resort. Raises :class:`StructuredOutputError` only when nothing is parseable
    (e.g. a truncated stream with no balanced object — caught upstream by the
    ``max_tokens`` stop-reason guard)."""
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
        repaired = repair_json(cand, return_objects=True)
        if repaired not in ("", None, [], {}):
            return repaired
    raise StructuredOutputError(f"output could not be parsed even after repair: {text[:120]!r}")


def validate_to(model_cls, data: Any):
    """Validate ``data`` against the Pydantic ``model_cls`` (layer 3), surfacing a
    validation failure as a :class:`StructuredOutputError` whose message carries the
    field errors — exactly what a bounded retry (layer 4) feeds back to the model.
    BOUNDS live in the model's validators (kept out of the JSON Schema to stay in
    Anthropic's strict-grammar subset); they fire here."""
    from pydantic import ValidationError

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
    return validate_to(model_cls, tolerant_parse(text))


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


def check_stop_reason(stop_reason: str | None) -> None:
    """Raise on a stop/finish reason that signals NO usable output (Anthropic
    {refusal, max_tokens} or the normalized {length, content_filter, error}). A normal
    ``stop``/``tool_call``/``end_turn``/``None`` passes. Keeps a TRUNCATED or refused
    turn from being read as a clean (empty) structured result — and from being
    silently "repaired" into a plausible-but-wrong object by json-repair."""
    if stop_reason in _BAD_STOP_REASONS:
        raise StructuredOutputError(_BAD_STOP_REASONS[stop_reason])


# Default bounded retry budget for the structured-output path (layer 4): ONE retry to
# the SAME model with the validation error fed back (Pydantic AI's default budget is 1;
# the research recommends raising it to ~2). The deterministic validator is the arbiter.
OUTPUT_RETRIES = 2
