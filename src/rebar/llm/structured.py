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
import logging
import re
from typing import Any

from rebar.llm.errors import StructuredOutputError, UnretryableOutputError

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)

# The two line-anchored sentinel markers that delimit the single JSON answer in a
# prompted reply. Defined ONCE here and referenced from both SENTINEL_DIRECTIVE (the
# prompt text) and parse_structured (the parser), so the literals are single-sourced.
_SENTINEL_OPEN = "<<<REBAR_OUTPUT>>>"
_SENTINEL_END = "<<<END>>>"

# The durable contract for the sentinel marker channel: models are told to wrap their
# single JSON answer between the two line-anchored markers so extraction is unambiguous.
# Each marker must be alone on its own physical line; recognition is line-anchored so a
# JSON string value containing the literal END marker cannot terminate a block.
SENTINEL_DIRECTIVE = (
    "Then wrap that single JSON object between two marker lines so it can be extracted "
    f"unambiguously: put a line containing ONLY {_SENTINEL_OPEN}, then the single JSON "
    f"object (no prose, no markdown fence), then a line containing ONLY {_SENTINEL_END}. "
    "Each marker must be alone on its own line."
)


def output_mode(model_cls, caps, *, thinking: bool = False):
    """Select the Pydantic AI output mode for ``model_cls`` (layer 1).

    NativeOutput when ``caps.native_structured_output`` (a :class:`rebar.llm.capabilities.
    ModelCapabilities`, read from the model's PROFILE — never a provider-name string, story
    S2); PromptedOutput for everyone else (the broadest, and — crucially — not a
    constrained/native output mode). ``thinking`` withdraws NativeOutput ONLY when the model is
    not MEASURED to accept native output under extended thinking
    (``caps.native_output_with_thinking``): the exact gate is
    ``native_structured_output and (not thinking or native_output_with_thinking)``.

    The old blanket "thinking forces prompted" rested on a stale rationale — the documented
    Anthropic 400 was ``tool_choice`` x thinking ("Thinking may not be enabled when tool_choice
    forces tool use"), NOT ``outputConfig`` json_schema x thinking, which succeeds on the wire
    today (measured E1: sonnet adaptive, haiku budget 2048). So native-under-thinking is gated
    per-model by a MEASURED capability fact, defaulting fail-closed to False so no cell flips
    until the rows story records measured PASS cells. This branch is reached only from the
    no-tools single-shot path (:func:`rebar.llm.structured_run`): tools x thinking x native
    suppresses tool calling (E1: 2/2 skipped), so the agentic runner never consults it — the
    no-tools scoping is structural, not a parameter. Deliberately does NOT read
    ``caps.supports_thinking`` — the constraint is a property of the CALL, so the caller's
    ``thinking`` argument is authoritative; ``supports_thinking`` exists for signed provenance."""
    from pydantic_ai import NativeOutput, PromptedOutput

    if caps.native_structured_output and (not thinking or caps.native_output_with_thinking):
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
    schema = json.dumps(model_cls.model_json_schema(), separators=(",", ":"))
    return (
        (
            "Respond with ONLY a single JSON object conforming to this JSON Schema "
            "(use these EXACT keys; no prose, no markdown fence):\n" + schema
        )
        + "\n\n"
        + SENTINEL_DIRECTIVE
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


def _all_json_objects(text: str) -> list[Any]:
    """Every top-level, non-overlapping balanced ``{…}`` object in ``text`` that PARSES as
    JSON, in discovery order (same string-aware brace state machine as
    :func:`_first_json_object`, but yields ALL such objects instead of only the first).

    A region that opens a brace but does not close-and-parse is skipped, and the scan
    advances to the next ``{`` after the (attempted) region so nested/enclosed objects are
    not double-counted."""
    objects: list[Any] = []
    start = text.find("{")
    n = len(text)
    while start >= 0:
        depth, in_str, esc = 0, False, False
        closed_at = -1
        for i in range(start, n):
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
                    closed_at = i
                    break
        if closed_at >= 0:
            try:
                objects.append(json.loads(text[start : closed_at + 1]))
                start = text.find("{", closed_at + 1)
                continue
            except json.JSONDecodeError:
                pass
        start = text.find("{", start + 1)
    return objects


def _candidate_dicts(text: str, model_cls) -> list[dict]:
    """Enumerate candidate JSON objects from ``text`` (fenced blocks, every balanced
    top-level object, and the whole text when it is itself valid JSON), screen each by
    top-level key-overlap with ``model_cls.model_fields``, and return the survivors in
    discovery order. Uses plain ``json.loads`` only — NO json-repair at this stage."""
    fields = set(getattr(model_cls, "model_fields", {}))
    candidates: list[Any] = []

    for match in _FENCE_RE.finditer(text):
        try:
            candidates.append(json.loads(match.group(1)))
        except json.JSONDecodeError:
            continue

    candidates.extend(_all_json_objects(text)[-32:])

    try:
        whole = json.loads(text)
    except json.JSONDecodeError:
        whole = None
    else:
        candidates.append(whole)

    survivors: list[dict] = []
    for cand in candidates:
        probed = cand
        if isinstance(probed, list):
            dict_elems = [item for item in probed if isinstance(item, dict)]
            probed = dict_elems[0] if len(dict_elems) == 1 else None
        if isinstance(probed, dict) and fields & set(probed):
            survivors.append(probed)
    return survivors


def _select_and_validate(text: str, model_cls):
    """The deterministic layers (2)+(3) as one call: SCHEMA-FILTERED candidate selection,
    then validate. Returns a validated ``model_cls`` instance, or raises
    :class:`StructuredOutputError` (the signal a caller turns into a single bounded retry —
    layer 4).

    ``tolerant_parse`` is schema-BLIND — it returns the FIRST parseable JSON object, so a
    reply that quotes an unrelated record (e.g. a ``{"relation": …}`` dependency-link record
    from a tool result) BEFORE the real verdict loses the verdict and fail-closes a
    legitimate close (bug df3a). Here we instead enumerate every cleanly-parseable candidate
    object, keep only those that SHARE a top-level key with ``model_cls`` (the all-defaults
    contract validates ``{}``, so the key-overlap screen is mandatory), and among the ones
    that VALIDATE the LAST wins. Anything needing json-repair (trailing commas, unclosed
    braces, smart quotes, non-JSON ``${{ }}`` braces) matches no clean candidate and falls
    through byte-for-byte to today's ``tolerant_parse`` pipeline, so near-miss/repair
    behavior is unchanged."""
    survivors = _candidate_dicts(text, model_cls)
    if survivors:
        validated = []
        for cand in survivors:
            try:
                validated.append(validate_to(model_cls, cand))
            except StructuredOutputError:
                continue
        if validated:
            if len(validated) >= 2 and validated[-1] is not validated[0]:
                logger.warning(
                    "parse_structured: multiple candidates validated for %s; "
                    "selecting the LAST (first=%r, last=%r)",
                    getattr(model_cls, "__name__", model_cls),
                    validated[0],
                    validated[-1],
                )
            return validated[-1]
        # Candidates passed the screen but none validated — raise the LAST one's error.
        return validate_to(model_cls, survivors[-1])
    return validate_to(model_cls, tolerant_parse(text, schema=model_cls))


def _sentinel_blocks(text: str) -> list[str]:
    """Extract the CONTENT of every complete sentinel block in ``text``, in document order.

    LINE-ANCHORED, NON-NESTED pairing: a line is an OPEN marker iff its stripped form equals
    the OPEN literal, an END marker iff its stripped form equals the END literal. Scanning
    left-to-right, each OPEN pairs with the NEXT END line after it (extra OPENs in between are
    ignored → OPEN…OPEN…END is ONE block); scanning resumes after that END. An OPEN with no
    following END is not a complete block. A block's content is the lines strictly between the
    markers rejoined with "\\n" (empty when OPEN is immediately followed by END)."""
    lines = text.split("\n")
    blocks: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        if lines[i].strip() == _SENTINEL_OPEN:
            j = i + 1
            while j < n and lines[j].strip() != _SENTINEL_END:
                j += 1
            if j < n:
                blocks.append("\n".join(lines[i + 1 : j]))
                i = j + 1
                continue
            break
        i += 1
    return blocks


def parse_structured(text: str, model_cls):
    """Marker-aware wrapper around :func:`_select_and_validate` (layers 2+3).

    Models are told (via :data:`SENTINEL_DIRECTIVE`) to wrap their single JSON answer between
    the two line-anchored sentinel markers. When ≥1 complete marker block exists, extraction
    PREFERS a marker-delimited payload over any decoy JSON elsewhere in the reply and
    FAILS-CLOSED (raises) when a marker block exists but no block validates — it never
    silently falls through to an out-of-band decoy. When there are no complete blocks
    (markerless reply, or an unterminated OPEN), it delegates to ``_select_and_validate`` on
    the full original reply, byte-for-byte unchanged."""
    blocks = _sentinel_blocks(text)
    if blocks:
        for content in reversed(blocks):
            try:
                return _select_and_validate(content, model_cls)
            except StructuredOutputError:
                continue
        # No block validated — raise the LAST doc-order block's error (fail-closed; never
        # fall through to an out-of-band decoy in the full reply).
        return _select_and_validate(blocks[-1], model_cls)
    return _select_and_validate(text, model_cls)


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


def check_response(response: Any) -> None:
    """Fail-closed guard for a COMPLETED model turn (task 8303, refusal-as-content). Raises
    :class:`UnretryableOutputError` on any refusal/truncation signal so a refusal is NEVER
    returned as a usable structured result (the silent-success failure of pydantic-ai #5221,
    where a content-policy refusal comes back as a normal-looking text part).

    Two layers, so the guarantee does not depend on a single provider-adapter detail:

    1. The normalized ``response.finish_reason`` via :func:`check_stop_reason`. On the current
       Anthropic path this ALREADY closes the gap: pydantic-ai maps Anthropic
       ``stop_reason='refusal'`` to ``finish_reason='content_filter'`` (in this repo's
       ``_UNRETRYABLE_STOP_REASONS``), and OpenAI surfaces ``content_filter`` directly.
    2. **Defense in depth:** a raw refusal/truncation signal in ``response.provider_details``
       — pydantic-ai stashes the raw provider ``finish_reason`` there and, for a refusal, a
       ``'refusal'`` explanation key (Anthropic ``stop_details``). This catches a refusal even
       if a future adapter stops mapping it onto ``finish_reason`` (leaving it ``None``/
       ``stop``) — the exact #5221 shape — so rebar stays fail-closed regardless.

    A clean turn (normal finish_reason, no refusal signal) passes. Use this at every point the
    runner is about to RETURN or PARSE a model turn's output as a structured result."""
    check_stop_reason(getattr(response, "finish_reason", None))
    details = getattr(response, "provider_details", None)
    if isinstance(details, dict):
        raw = details.get("finish_reason")
        if raw in _UNRETRYABLE_STOP_REASONS or "refusal" in details:
            msg = _BAD_STOP_REASONS.get(raw) if isinstance(raw, str) else None
            raise UnretryableOutputError(
                msg or "the model refused to answer (provider refusal signal in provider_details)"
            )


# Default bounded retry budget for the structured-output path (layer 4): ONE retry to
# the SAME model with the validation error fed back (Pydantic AI's default budget is 1;
# the research recommends raising it to ~2). The deterministic validator is the arbiter.
OUTPUT_RETRIES = 2
