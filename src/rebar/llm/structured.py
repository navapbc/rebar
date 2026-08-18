"""The structured-output reliability stack (1268) — deterministic where possible.

Workflow LLM output feeds DETERMINISTIC steps, so schema-conformance is load-bearing.
The research (docs/experiments/workflow-remediation-pocs/structured-output-research.md)
converged on a LAYERED, deterministic-where-possible stack that RETIRES the
second-interpreter LLM (using another non-deterministic model to "fix" output is a
recognized anti-pattern):

  (1) provider-native CONSTRAINED decoding / strict json_schema where the provider
      offers it (:func:`output_mode` -> NativeOutput), else cross-provider
      PromptedOutput. PromptedOutput is also the mode used under extended thinking
      UNLESS the model is MEASURED to accept a native output constraint under thinking
      (``caps.native_output_with_thinking``); the documented Anthropic 400 was
      ``tool_choice`` x thinking ("Thinking may not be enabled when tool_choice forces
      tool use"), NOT ``outputConfig`` json_schema x thinking, which succeeds on the wire
      today (measured E1) — see :func:`output_mode`; it is not a relic of any earlier
      forced-tool mechanism;
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
from functools import lru_cache
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


# ── Layer-1 schema-complexity bound (bug 895c) ─────────────────────────────────
# Native/constrained decoding does not merely "prefer" a small schema — Anthropic COMPILES
# the contract's JSON Schema into a decoding grammar, under a documented 180-second
# compilation timeout and documented complexity caps (24 optional parameters, 16
# union-typed parameters). Past that the request does not degrade, it 400s
# (`ValidationException`: "Grammar compilation timed out." at ~185s, or "Schema is too
# complex." at ~50-60s) — and it does so on EVERY retry, forever, because the schema is a
# property of the contract, not of the request. rebar's Pass-2 verification contracts are
# all-optional by construction (every field declares a `default=`, so every field is
# optional in JSON Schema), which is exactly the axis the compiler charges for.
#
# MEASURED live (us-east-1, serial, one variable at a time), optional-parameter count vs
# native result:
#
#     review_result             10 optional   NATIVE  OK      14.0s
#     completion_verdict        14 optional   NATIVE  OK      29.0s
#     verification              22 optional   NATIVE  FAIL   185.3s
#     plan_review_verification  31 optional   NATIVE  FAIL   185.4s
#     code_review_verification  36 optional   NATIVE  FAIL   185.4s
#     plan_review_verification  (identical payload, PROMPTED)  OK  11.2s
#
# Schema BYTE size is NOT the predictor: stripping every description (8,719 -> 2,403 bytes)
# still failed, while a 4,022-byte contract passed. Optional-parameter count is.
#
# The bound sits at 16 — BELOW Anthropic's published 24 — because the published number
# UNDER-predicts: `verification` fails at 22, two under the documented cap. 16 sits inside
# the measured gap (14 = largest measured PASS, 22 = smallest measured FAIL) and deliberately
# nearer the PASS side, so it keeps every contract measured to work on the native path,
# refuses every contract measured to hang, and leaves headroom for a contract that grows a
# field or two. The union bound (12) mirrors the same conservatism against the published 16;
# the largest union count on any measured-PASSING contract is 9 (review_result,
# completion_verdict), so it downgrades nothing that is known to work.
# Downgrading is CHEAP and always safe (the prompted path returned the identical verdict in
# 11.2s); exceeding the bound is a guaranteed ~185s failure, so the asymmetry is deliberate.
_NATIVE_MAX_OPTIONAL_PROPERTIES = 16
_NATIVE_MAX_UNION_PROPERTIES = 12


# ── Layer-1 STACK-capability gate (bug 895c, variant B) ────────────────────────
# The complexity bound above assumes the native request at least REACHES the provider. On an
# older botocore it does not: boto3 validates Converse's input shape CLIENT-SIDE, and a
# botocore whose `bedrock-runtime` service model predates `outputConfig` rejects the request
# in 0.0s before a byte is sent:
#
#     FAILED in 0.0s: Parameter validation failed: Unknown parameter in input: "outputConfig",
#     must be one of: modelId, messages, system, inferenceConfig, toolConfig, …
#
# MEASURED: botocore 1.43.64 carries `outputConfig`; botocore 1.40.61 does not. The 1.40.61
# failure hit `ticket_digest` — a 722-BYTE contract, 4 optional properties — so this is NOT
# the complexity failure wearing a different hat: on an under-versioned stack EVERY native
# call fails regardless of schema size, and it degrades silently ("store-wide overlap step
# failed; no overlap findings"). Error-string matching could never have caught this from the
# complexity phrases — the two failures share no vocabulary — which is why the gate, not the
# fallback, is the load-bearing half.
#
# Scoped to BEDROCK via `caps.prompt_cache_style == "bedrock"`, the capability record's
# existing Bedrock discriminator (a profile exposing `bedrock_supports_prompt_caching`) — a
# fact read off the model PROFILE, never a provider-name string, per story S2. Non-Bedrock
# providers do not go through botocore and are untouched.


@lru_cache(maxsize=1)
def _bedrock_converse_output_config_support() -> bool | None:
    """Tri-state: does the INSTALLED botocore's ``bedrock-runtime`` Converse operation accept
    the ``outputConfig`` parameter?

    ``True`` supported, ``False`` NOT supported (or present-but-unintrospectable — fail
    closed), ``None`` when botocore is absent from this process entirely.

    ``None`` is deliberately distinct from ``False``: with no botocore installed there is no
    boto3 Bedrock transport in this process at all, so this probe has observed NOTHING about a
    live call and must not veto routing on that silence (the lean/no-``reviewbot``-extra
    install, where the Bedrock capability record is only ever exercised against a profile
    stub). Fail-closed applies where it can actually bite: botocore IS installed — a real
    Bedrock call is possible — and we cannot confirm the parameter. Cached because it reads a
    botocore JSON service model, and the answer cannot change within a process. NEVER raises."""
    try:
        import botocore.session
    except Exception:  # noqa: BLE001 — no botocore at all: nothing observed, veto nothing
        return None
    try:
        service = botocore.session.get_session().get_service_model("bedrock-runtime")
        members = getattr(service.operation_model("Converse").input_shape, "members", None)
        return "outputConfig" in (members or {})
    except Exception:  # noqa: BLE001 — botocore present but unreadable: fail CLOSED
        return False


def native_output_stack_supported(caps) -> bool:
    """Whether the installed stack can actually SEND a native output constraint for ``caps``.

    Always ``True`` off Bedrock (no botocore in the path). On Bedrock, defers to
    :func:`_bedrock_converse_output_config_support`, treating its ``None`` (no botocore
    installed) as no evidence against native. PURE apart from the cached probe, and never
    raises — an unreadable ``caps`` cannot be Bedrock-scoped, so it keeps today's routing."""
    if getattr(caps, "prompt_cache_style", None) != "bedrock":
        return True
    return _bedrock_converse_output_config_support() is not False


def _schema_objects(node: Any):
    """Yield every sub-schema in ``node`` that declares ``properties`` (an object schema),
    walking nested dicts/lists — which reaches ``$defs`` (nested contract models),
    ``properties`` values, ``items``, and ``anyOf`` branches alike. ``$ref`` is deliberately
    NOT expanded: each ``$def`` is visited exactly once at its definition site, so a model
    referenced from three fields is counted once, matching what the grammar compiler sees."""
    if isinstance(node, dict):
        if isinstance(node.get("properties"), dict):
            yield node
        for value in node.values():
            yield from _schema_objects(value)
    elif isinstance(node, list):
        for item in node:
            yield from _schema_objects(item)


def schema_complexity(model_cls) -> tuple[int, int]:
    """``(optional_property_count, union_property_count)`` over ``model_cls``'s FULL JSON
    Schema — the two axes Anthropic's grammar compiler charges for (see the bound above).

    A property is OPTIONAL when it is not listed in its own object's ``required`` list (the
    all-``default=`` contracts list nothing as required, so every field counts). A property is
    UNION-typed when its sub-schema carries ``anyOf`` or gives ``type`` as a list — the shape
    ``X | None`` compiles to. Counted across nested objects and ``$defs``, not just the top
    level, because a contract's cost is dominated by the per-finding sub-model it repeats."""
    schema = model_cls.model_json_schema()
    optional = union = 0
    for obj in _schema_objects(schema):
        required = obj.get("required")
        required_names = set(required) if isinstance(required, list) else set()
        for name, prop in obj["properties"].items():
            if name not in required_names:
                optional += 1
            if isinstance(prop, dict) and ("anyOf" in prop or isinstance(prop.get("type"), list)):
                union += 1
    return optional, union


def native_output_within_bound(model_cls) -> bool:
    """Whether ``model_cls`` is small enough for provider-native constrained decoding.

    PURE and importable so the bound can be asserted on a contract directly, with no model
    call. FAIL-OPEN on a class that has no usable ``model_json_schema()`` (a stand-in/stub
    output class): an unmeasurable contract keeps whatever routing ``caps`` alone selects,
    exactly as before this bound existed — this gate only ever SUBTRACTS native routing from
    a contract it can positively show is over the line."""
    try:
        optional, union = schema_complexity(model_cls)
    except Exception:  # noqa: BLE001 — an unmeasurable contract is not a contract we downgrade
        return True
    return optional <= _NATIVE_MAX_OPTIONAL_PROPERTIES and union <= _NATIVE_MAX_UNION_PROPERTIES


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
    ``thinking`` argument is authoritative; ``supports_thinking`` exists for signed provenance.

    NativeOutput is additionally gated on SCHEMA COMPLEXITY (bug 895c): a contract whose JSON
    Schema exceeds :func:`native_output_within_bound` is routed to PromptedOutput even on a
    native-capable model, because the provider compiles that schema into a decoding grammar and
    400s (`Grammar compilation timed out.` / `Schema is too complex.`) after ~185s on every
    attempt — a permanent, un-retryable failure of that STEP, burning ~185s per run. Scope
    the claim there: a failing step does not necessarily fail its whole gate (a review has
    been observed certifying seconds after one such step failed), so this is a step-level
    correctness-and-latency fix, not a gate-outage fix. Not a slow success either. This is a
    BOUND, not a disable: contracts within it (``review_result``, ``completion_verdict``) keep
    the native path. The gate reads only ``model_cls``, never the model id, so it stays a
    property of the CONTRACT — the same reason the thinking gate reads the call, not the
    profile.

    NativeOutput is FURTHER gated on whether the installed stack can send a native output
    constraint at all (:func:`native_output_stack_supported`, bug 895c variant B): an
    under-versioned botocore rejects Bedrock's ``outputConfig`` client-side in 0.0s for EVERY
    contract, however small. That check runs FIRST, because when it fails the contract's size
    is irrelevant."""
    from pydantic_ai import NativeOutput, PromptedOutput

    if caps.native_structured_output and (not thinking or caps.native_output_with_thinking):
        if not native_output_stack_supported(caps):
            # Variant B: the installed botocore cannot even SEND `outputConfig`, so every
            # native call would 0.0s-fail client-side regardless of contract size. Warning,
            # not info — this one is a whole-stack condition an operator can fix by upgrading
            # botocore, and it silently emptied every structured step while it went unnoticed.
            logger.warning(
                "structured output: the installed botocore cannot send Bedrock's outputConfig "
                "parameter — routing contract %s to PromptedOutput (bug 895c). Upgrade botocore "
                "(>=1.43.64 carries it; 1.40.61 does not) to restore native structured output.",
                getattr(model_cls, "__name__", model_cls),
            )
            return PromptedOutput(model_cls)
        if native_output_within_bound(model_cls):
            return NativeOutput(model_cls)
        optional, union = schema_complexity(model_cls)
        # Never silent: a downgrade changes which reliability layers run, so an operator
        # debugging a verdict must be able to see WHICH contract was moved and why.
        logger.info(
            "structured output: contract %s exceeds the native grammar bound "
            "(optional properties %d, max %d; union-typed %d, max %d) — routing to "
            "PromptedOutput to avoid the provider's grammar-compilation rejection (bug 895c)",
            getattr(model_cls, "__name__", model_cls),
            optional,
            _NATIVE_MAX_OPTIONAL_PROPERTIES,
            union,
            _NATIVE_MAX_UNION_PROPERTIES,
        )
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
        if isinstance(probed, dict) and fields & set(probed) and probed not in survivors:
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
