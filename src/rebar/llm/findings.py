"""The findings data model — the universal output of every LLM review operation.

The **JSON Schema is the single source of truth** (``common.schema.json#/$defs/
{finding,citation,severity}`` + ``review_result.schema.json``). This module is
stdlib-only: it normalizes raw agent output into that shape, validates it against
the canonical schema, and resolves ``file`` citations against the real repo so a
hallucinated ``path:line`` never ships. The Pydantic mirror used by the runner's
structured-output contract is built lazily (``findings_response_model``) so this
module imports without pydantic.
"""

from __future__ import annotations

import os
from typing import Any

from rebar import schemas
from rebar.llm.errors import LLMError

SEVERITIES = ("critical", "high", "medium", "low", "info")
CITATION_KINDS = ("file", "url", "source")


class FindingsError(LLMError):
    """Raised when review output cannot be coerced into a valid ReviewResult.

    Subclasses ``LLMError`` so the CLI/MCP/library layers catch it with the same
    ``except LLMError`` as every other framework failure (a schema-invalid model
    response is an *expected* failure mode, not an uncaught traceback)."""


def _coerce_citation(raw: Any) -> dict:
    """Coerce one citation into the {kind, …} schema shape. A bare string becomes
    a freeform ``source`` citation; a dict missing ``kind`` is inferred from its
    populated fields (path→file, url→url, else source)."""
    if isinstance(raw, str):
        return {"kind": "source", "description": raw}
    if not isinstance(raw, dict):
        return {"kind": "source", "description": str(raw)}
    c = dict(raw)
    kind = c.get("kind")
    if kind not in CITATION_KINDS:
        if c.get("path"):
            kind = "file"
        elif c.get("url"):
            kind = "url"
        else:
            kind = "source"
    c["kind"] = kind
    for line_key in ("line_start", "line_end"):
        if line_key in c and c[line_key] is not None:
            try:
                value = int(c[line_key])
            except (TypeError, ValueError):
                c.pop(line_key, None)
                continue
            # Negative line numbers violate the schema's `minimum: 0`; drop rather
            # than let one bad citation field fail validation of the whole review.
            if value < 0:
                c.pop(line_key, None)
            else:
                c[line_key] = value
    # Strip null-valued keys: a model that emits `"path": null` / `"url": null` on
    # a `source` citation would otherwise fail the schema (those fields are typed
    # `string`). None means absent.
    return {k: v for k, v in c.items() if v is not None}


def normalize_finding(raw: dict, *, reviewer_id: str | None = None) -> dict:
    """Coerce one raw finding into the canonical ``finding`` shape (best-effort,
    schema-validated downstream). Unknown severities clamp to ``info``."""
    f = dict(raw)
    sev = str(f.get("severity", "")).strip().lower()
    f["severity"] = sev if sev in SEVERITIES else "info"
    f["dimension"] = str(f.get("dimension") or f.get("category") or "general").strip()
    f.pop("category", None)
    f["detail"] = str(f.get("detail") or f.get("description") or f.get("body") or "").strip()
    f.pop("description", None)
    f.pop("body", None)
    cits = f.get("citations") or []
    if not isinstance(cits, list):
        cits = [cits]
    f["citations"] = [_coerce_citation(c) for c in cits]
    # `confidence` is a soft, optional field — clamp to [0,1] (or drop if
    # non-numeric) so a sloppy model value can't sink an otherwise-good review.
    conf = f.get("confidence")
    if conf is not None:
        try:
            f["confidence"] = min(1.0, max(0.0, float(conf)))
        except (TypeError, ValueError):
            f.pop("confidence", None)
    if reviewer_id and not f.get("reviewer_id"):
        f["reviewer_id"] = reviewer_id
    # Drop keys the model left explicitly null: optional schema fields (title,
    # reviewer_id, …) are typed `string`, so a None value fails validation —
    # `None` means "absent", so strip it rather than sink the whole review.
    return {k: v for k, v in f.items() if v is not None}


def build_result(
    findings: list[dict],
    *,
    runner: str,
    model: str | None = None,
    trace_id: str | None = None,
    target: dict | None = None,
    reviewers: list[str] | None = None,
    summary: str | None = None,
    reviewer_id: str | None = None,
) -> dict:
    """Assemble a ``review_result`` dict from raw findings + provenance."""
    result: dict = {
        "findings": [normalize_finding(f, reviewer_id=reviewer_id) for f in findings],
        "runner": runner,
        "model": model,
        "trace_id": trace_id,
    }
    if target is not None:
        result["target"] = target
    if reviewers is not None:
        result["reviewers"] = reviewers
    if summary is not None:
        result["summary"] = summary
    return result


def validate_result(result: dict) -> dict:
    """Validate ``result`` against the canonical ``review_result`` schema (and its
    cross-file ``$ref``s). No-ops gracefully if ``jsonschema`` isn't installed
    (the ``dev`` extra), so the framework runs without the validation libs."""
    try:
        validator = schemas.validator(schemas.REVIEW_RESULT)
    except Exception:  # jsonschema/referencing not installed — skip deep validation
        if "findings" not in result or not isinstance(result["findings"], list):
            raise FindingsError("review result missing a 'findings' array") from None
        return result
    import jsonschema

    try:
        validator.validate(result)
    except jsonschema.ValidationError as exc:
        raise FindingsError(f"review result failed schema validation: {exc.message}") from None
    return result


def resolve_citations(result: dict, repo_path: str | None) -> dict:
    """Resolve every ``kind=file`` citation against the real repo: downgrade to a
    ``source`` note when the file doesn't exist, falls outside the repo, points at
    a denied internal-state path (``.git`` / ``.tickets-tracker`` / ``.bridge_state``
    — the same deny-list the file tools enforce, so the sandbox guarantee holds in
    the OUTPUT too), or cites lines beyond the file. Guarantees a shipped
    ``file:line`` citation actually resolves — agents hallucinate otherwise."""
    if not repo_path:
        return result
    from rebar.llm.config import denied_paths, is_denied

    root = os.path.realpath(repo_path)
    denied = denied_paths(root)
    for finding in result.get("findings", []):
        for cit in finding.get("citations", []):
            if cit.get("kind") != "file":
                continue
            path = cit.get("path")
            if not path:
                continue
            abs_path = os.path.realpath(os.path.join(root, path))
            within = abs_path == root or abs_path.startswith(root + os.sep)
            if not within or not os.path.isfile(abs_path):
                _downgrade(cit, f"unresolved file citation: {path}")
                continue
            if is_denied(abs_path, denied):
                _downgrade(cit, f"internal state path not citable: {path}")
                continue
            # Test `is not None` (not truthiness): the schema treats line_start=0 /
            # omitted as "whole file", which must not be conflated with absent.
            start, end = cit.get("line_start"), cit.get("line_end")
            needed = max(start or 0, end or 0)
            if needed > 0:
                # Stop as soon as the cited line is reached — no full-file scan for
                # citations near the top of a large file.
                count = 0
                try:
                    with open(abs_path, encoding="utf-8", errors="replace") as fh:
                        for count, _ in enumerate(fh, 1):
                            if count >= needed:
                                break
                except OSError:
                    continue
                if count < needed:  # file ended before the cited line → out of range
                    note = f"{path} (cited lines {start}-{end} exceed file length {count})"
                    _downgrade(cit, note)
    return result


def _downgrade(cit: dict, note: str) -> None:
    """Turn an unresolvable file citation into a freeform source note in place."""
    cit["kind"] = "source"
    existing = cit.get("description")
    cit["description"] = f"{existing} [{note}]" if existing else note
    for k in ("path", "line_start", "line_end"):
        cit.pop(k, None)


# ── Finalization strategy (WS-D1) ─────────────────────────────────────────────
# ONE place that turns a runner's raw output into a final dict, so FakeRunner and
# the agent runners stop duplicating build→resolve→validate.


def finalize_findings(
    findings: list[dict],
    *,
    runner: str,
    model: str | None = None,
    trace_id: str | None = None,
    target: dict | None = None,
    reviewers: list[str] | None = None,
    summary: str | None = None,
    reviewer_id: str | None = None,
    repo_path: str | None = None,
) -> dict:
    """The single review_result finalization strategy: build → resolve citations →
    validate. Every runner's findings path funnels through here (so the three
    formerly-duplicated call sites are now one)."""
    result = build_result(
        findings,
        runner=runner,
        model=model,
        trace_id=trace_id,
        target=target,
        reviewers=reviewers,
        summary=summary,
        reviewer_id=reviewer_id,
    )
    resolve_citations(result, repo_path)
    return validate_result(result)


def _final_text(outcome: dict) -> str:
    """The agent's last non-empty message text (for ``mode='text'``)."""
    for msg in reversed(outcome.get("messages") or []):
        content = getattr(msg, "content", None)
        if content:
            return content if isinstance(content, str) else str(content)
    return ""


def validate_structured(data: dict, output_schema: str | None) -> dict:
    """Best-effort validate a structured payload against a named JSON Schema.

    Public (used by the verify-completion op for its final re-validation, and by
    ``finalize_outcome``). No-ops when ``output_schema`` is unset, the schema isn't a
    packaged rebar schema, or jsonschema isn't installed — mirroring
    ``validate_result``'s graceful degradation. Raises :class:`FindingsError` on a
    real validation failure."""
    if not output_schema:
        return data
    try:
        validator = schemas.validator(output_schema)
    except Exception:  # unknown schema name or jsonschema absent — skip
        return data
    import jsonschema

    try:
        validator.validate(data)
    except jsonschema.ValidationError as exc:
        raise FindingsError(
            f"agent output failed schema {output_schema!r}: {exc.message}"
        ) from None
    return data


def finalize_outcome(
    outcome: dict,
    *,
    mode: str = "findings",
    output_schema: str | None = None,
    runner: str,
    model: str | None = None,
    trace_id: str | None = None,
    target: dict | None = None,
    reviewers: list[str] | None = None,
    repo_path: str | None = None,
    reviewer_id: str | None = None,
) -> dict:
    """Finalize an agent outcome per ``mode`` — the generalized strategy (WS-D1).

    * ``findings`` (default) — the review_result pipeline (unchanged for the review
      ops). Requires a ``structured_response``; a missing one is a hard
      :class:`StructuredOutputError` (an empty review must never read as a clean one).
    * ``structured`` — return the agent's structured payload, validated against
      ``output_schema`` when given.
    * ``text`` — return ``{text, runner, model, trace_id}`` from the final message.
    """
    if mode == "text":
        return {
            "text": _final_text(outcome),
            "runner": runner,
            "model": model,
            "trace_id": trace_id,
        }

    structured = outcome.get("structured_response")
    if structured is None:
        from rebar.llm.errors import StructuredOutputError

        raise StructuredOutputError(
            "the agent returned no structured output (no structured_response). "
            "Treating this as a failed run rather than a clean one."
        )
    # exclude_none: a Pydantic optional left unset dumps as explicit ``null``, which a
    # shape-only schema (fields typed ``string``) rejects in `validate_structured` BEFORE
    # the op can normalize — and the structured-output path is free-generation +
    # code-validation, so optional-None leakage is real (BI-1). Harmless for the findings
    # path (normalize_finding strips nulls anyway); also hardens mode="structured" steps.
    data = (
        structured.model_dump(exclude_none=True)
        if hasattr(structured, "model_dump")
        else {k: v for k, v in dict(structured).items() if v is not None}
    )

    if mode == "structured":
        payload = validate_structured(data, output_schema)
        return {**payload, "runner": runner, "model": model, "trace_id": trace_id}

    # mode == "findings"
    return finalize_findings(
        data.get("findings", []),
        runner=runner,
        model=model,
        trace_id=trace_id,
        target=target,
        reviewers=reviewers,
        summary=data.get("summary"),
        reviewer_id=reviewer_id,
        repo_path=repo_path,
    )


def citation_model():
    """Lazily build the ``Citation`` structured-output model, mirroring
    ``common.schema.json#/$defs/citation``. Factored out (not nested) so OTHER output
    contracts (e.g. ``completion_verdict``) reuse the SAME citation shape — one source
    of truth, no drift. pydantic imported here, never at module top."""
    from pydantic import BaseModel, Field

    class Citation(BaseModel):
        kind: str = Field(description="One of: file | url | source.")
        path: str | None = Field(default=None, description="Repo-relative file path (kind=file).")
        line_start: int | None = Field(default=None, description="1-based start line (kind=file).")
        line_end: int | None = Field(default=None, description="1-based end line (kind=file).")
        url: str | None = Field(default=None, description="URL evidence (kind=url).")
        description: str | None = Field(
            default=None, description="Freeform source/evidence (kind=source)."
        )

    return Citation


def finding_model():
    """Lazily build the ``Finding`` structured-output model, mirroring
    ``common.schema.json#/$defs/finding`` (with ``citations: list[Citation]``). The
    schema-pin test reaches ``Citation`` through ``Finding.model_fields['citations']``,
    so keep that annotation typed."""
    from pydantic import BaseModel, Field, field_validator

    Citation = citation_model()

    class Finding(BaseModel):
        severity: str = Field(description="One of: critical | high | medium | low | info.")
        dimension: str = Field(
            description="Category/dimension, e.g. 'security', 'acceptance-criteria'."
        )
        detail: str = Field(description="Human-readable description of the finding.")
        title: str | None = Field(default=None, description="Optional short headline.")
        citations: list[Citation] = Field(
            default_factory=list, description="Evidence: file+line / url / freeform."
        )
        confidence: float | None = Field(default=None, description="Optional confidence 0..1.")
        reviewer_id: str | None = Field(
            default=None, description="Reviewer that produced this finding."
        )

        @field_validator("confidence")
        @classmethod
        def _bound_confidence(cls, v: float | None) -> float | None:
            # The bound lives HERE (a normalizing validator), NOT in the JSON Schema —
            # so the schema stays inside Anthropic's strict-grammar subset (1268). An
            # out-of-range model value is clamped to [0, 1] rather than rejected.
            return None if v is None else max(0.0, min(1.0, v))

    return Finding


def findings_response_model():
    """Build (lazily) the Pydantic model the runner binds as its structured-output
    contract. Mirrors ``common.schema.json#/$defs/finding``;
    pinned against the JSON Schema by a test so the two never drift. Requires
    pydantic (the ``agents`` extra) — imported here, not at module top."""
    from pydantic import BaseModel, Field

    Finding = finding_model()

    class ReviewFindings(BaseModel):
        """Structured output of an LLM review: the findings and an optional summary."""

        findings: list[Finding] = Field(description="All findings; [] if none.")
        summary: str | None = Field(default=None, description="Optional reviewer summary.")

    return ReviewFindings
