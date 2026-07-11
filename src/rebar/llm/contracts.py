"""Per-operation structured-output **contracts** — the seam that lets each operation
(and each workflow agent step) declare its OWN structured-output shape instead of the
runner hardcoding the findings model.

A contract maps a **serializable name** (the same string used as ``RunRequest.output_schema``
and as the JSON Schema name) to a builder that returns the Pydantic model the runner binds
as its structured-output contract. We key by NAME rather than passing the model object
directly (as Pydantic AI / instructor / OpenAI Agents SDK do) because ``output_schema`` is
threaded from the workflow YAML DSL, where a live class can't live — so the name is the
portable handle, and a schema-pin test keeps each model in lock-step with its JSON Schema.

Import-clean: the only module-top import is :mod:`rebar.llm.findings` (stdlib-only); every
builder imports ``pydantic`` **inside its body**, and registration merely stores a callable —
so ``import rebar.llm`` / ``import rebar.llm.contracts`` pull no heavy dependency.
"""

from __future__ import annotations

from collections.abc import Callable

from rebar.llm import findings

# name -> zero-arg builder returning a pydantic BaseModel subclass (the response model).
_CONTRACTS: dict[str, Callable[[], type]] = {}


def register_contract(name: str, builder: Callable[[], type]) -> None:
    """Register ``builder`` (a zero-arg factory returning a pydantic model) under ``name``.
    Storing a callable only — no model is built and no pydantic import happens here."""
    _CONTRACTS[name] = builder


def response_model_for(output_schema: str | None) -> type:
    """The structured-output Pydantic model for ``output_schema`` (a registered contract
    name), or the **findings** model default when it is unset/unknown. Built lazily — the
    selected builder imports pydantic internally."""
    if output_schema and output_schema in _CONTRACTS:
        return _CONTRACTS[output_schema]()
    return findings.findings_response_model()


def completion_verdict_response_model() -> type:
    """Structured-output model for the completion-verification op — mirrors
    ``completion_verdict.schema.json`` (pinned by a test). Reuses the shared ``Citation``
    model (no drift) and adds a per-finding ``criterion`` (the specific requirement that
    failed). pydantic imported lazily."""
    from pydantic import BaseModel, Field, field_validator

    Citation = findings.citation_model()

    class VerdictFinding(BaseModel):
        criterion: str = Field(
            description="The specific criterion that failed (verbatim or clearly identifying)."
        )
        detail: str = Field(description="Explanation of why the criterion is not met.")
        severity: str = Field(default="high", description="critical | high | medium | low | info.")
        dimension: str = Field(default="completion", description="Finding dimension.")
        # reason: Citation is a runtime-built pydantic model (a value, not a static type);
        # pydantic needs the real class in the annotation to validate citations.
        citations: list[Citation] = Field(  # type: ignore[valid-type]
            default_factory=list, description="Evidence: file+line / url / freeform source."
        )
        title: str | None = Field(default=None, description="Optional short headline.")
        remediation: str | None = Field(
            default=None,
            description=(
                "Optional per-finding next move. For an operator-attested criterion judged "
                "NOT MET, the concrete step that would make it pass: record proof as a "
                "ticket comment/artifact naming the reference (change URL/id), the observed "
                "outcome (votes/logs/console), and when. Distinct from the generic "
                "top-level verdict `remediation`."
            ),
        )

    class CompletionVerdict(BaseModel):
        """Structured output of the completion verifier: a PASS/FAIL verdict and, on FAIL,
        one finding per failing criterion."""

        verdict: str = Field(description="PASS or FAIL (normalized by the operation).")
        findings: list[VerdictFinding] = Field(
            default_factory=list, description="One per FAILING criterion; empty on PASS."
        )
        summary: str | None = Field(
            default=None, description="Optional summary / no-explicit-criteria PASS rationale."
        )

        @field_validator("verdict")
        @classmethod
        def _norm_verdict(cls, v: str) -> str:
            # A NORMALIZING validator (bounds in the validator, not the JSON Schema —
            # 1268): exactly ``PASS`` (case/space-insensitive) is PASS, ANYTHING else is
            # FAIL. Fail-safe: a garbled or truncated verdict (e.g. ``"PA"``) can never
            # silently pass. Idempotent with the completion op's own normalization.
            return "PASS" if str(v).strip().upper() == "PASS" else "FAIL"

    return CompletionVerdict


def ticket_digest_response_model() -> type:
    """Structured-output model for the Cupid ticket-digest op (epic only-crave-art),
    mirroring ``ticket_digest.schema.json``: four fields, all required. pydantic is
    imported inside the body (registration stores this builder, not a model)."""
    from pydantic import BaseModel, Field

    class TicketDigest(BaseModel):
        problem_keywords: list[str] = Field(
            default_factory=list, description="Salient problem/domain keywords (deduped)."
        )
        component_or_area: str = Field(
            default="", description="Component / subsystem / area the ticket concerns."
        )
        key_entities: list[str] = Field(
            default_factory=list,
            description="Named entities: config keys, schema/table names, files, functions.",
        )
        propositions: list[str] = Field(
            default_factory=list,
            description="2-6 atomic problem/repro statements; the op enforces the count bound.",
        )

    return TicketDigest


def overlap_verdict_response_model() -> type:
    """Structured-output model for one ordered-pair overlap-judge call (epic only-crave-art,
    9022), mirroring ``overlap_verdict.schema.json``. pydantic imported inside the body."""
    from pydantic import BaseModel, Field, field_validator

    class OverlapVerdict(BaseModel):
        relation: str = Field(
            default="related_distinct",
            description="First <relation> Second (closed relation enum).",
        )
        shared_artifact: str | None = Field(
            default=None, description="The concrete named shared artifact, or null."
        )
        confidence: float = Field(default=0.0, description="Confidence 0.0-1.0.")
        abstain: bool = Field(default=False, description="True when the judge is unsure.")

        @field_validator("relation")
        @classmethod
        def _norm_relation(cls, v: str) -> str:
            r = str(v).strip().lower()
            allowed = {"duplicates", "supersedes", "depends_on", "related_distinct", "unrelated"}
            return r if r in allowed else "related_distinct"

    return OverlapVerdict


# Built-ins. ``review_result`` (the default findings shape) and ``completion_verdict``.
register_contract("review_result", findings.findings_response_model)
register_contract("completion_verdict", completion_verdict_response_model)
# Cupid ticket-digest op (epic only-crave-art, ee3d). Registered here — co-located with
# ``response_model_for`` — so importing this module to call it guarantees the digest
# contract is registered before first use (no startup-import ordering assumption).
register_contract("ticket_digest", ticket_digest_response_model)
# Stage-2 overlap judge (9022) — same co-location guarantee.
register_contract("overlap_verdict", overlap_verdict_response_model)
