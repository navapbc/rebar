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
from typing import cast

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


def default_outputs(output_schema: str | None) -> dict:
    """The materializable DEFAULT-valued top-level fields of a REGISTERED contract model —
    the fields a live structured run always emits (pydantic ``model_dump(exclude_none=True)``
    of a fresh instance keeps a non-None default like ``[]``, drops a ``None`` default).

    Used by the workflow executor to guarantee an agent structured step's outputs always
    carry its model's default-valued fields (e.g. ``completion_verdict``'s ``criteria: []``),
    so downstream wiring (``${{ steps.<id>.outputs.criteria }}``) can reference them even when
    a lean/canned runner emitted a sparse payload. Returns ``{}`` for an unregistered name (no
    findings-model fallback — only a real contract has a meaningful default set)."""
    if not output_schema or output_schema not in _CONTRACTS:
        return {}
    from pydantic import BaseModel
    from pydantic_core import PydanticUndefined

    model = cast("type[BaseModel]", _CONTRACTS[output_schema]())
    out: dict = {}
    for name, fld in model.model_fields.items():
        default = fld.get_default(call_default_factory=True)
        # Skip required fields (PydanticUndefined) and None-defaults (a live run's
        # exclude_none drops those); keep concrete defaults like ``[]``.
        if default is not None and default is not PydanticUndefined:
            out[name] = default
    return out


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
                "Optional per-finding next move. For a non-codebase criterion judged "
                "NOT MET, the concrete step that would make it pass: record proof as a "
                "ticket comment/artifact naming the reference (change URL/id), the observed "
                "outcome (votes/logs/console), and when. Distinct from the generic "
                "top-level verdict `remediation`."
            ),
        )

    class Criterion(BaseModel):
        """One POSITIVE per-criterion evaluation record — the lossless PASS capture that rides
        alongside the failures-only ``findings``. One per evaluated criterion (met or not)."""

        criterion: str = Field(
            description="The evaluated criterion (verbatim or clearly identifying)."
        )
        met: bool = Field(description="Whether this criterion is demonstrably met.")
        # reason: Citation is a runtime-built pydantic model (a value, not a static type);
        # pydantic needs the real class in the annotation to validate the citation.
        citation: Citation | None = Field(  # type: ignore[valid-type]
            default=None, description="Evidence: file+line / url / freeform source (nullable)."
        )
        kind: str = Field(
            description="codebase-verifiable | non-codebase (the criterion's evidence kind)."
        )

    class CompletionVerdict(BaseModel):
        """Structured output of the completion verifier: a PASS/FAIL verdict and, on FAIL,
        one finding per failing criterion."""

        verdict: str = Field(description="PASS or FAIL (normalized by the operation).")
        findings: list[VerdictFinding] = Field(
            default_factory=list, description="One per FAILING criterion; empty on PASS."
        )
        criteria: list[Criterion] = Field(
            default_factory=list,
            description=(
                "Positive per-criterion evaluation records (met status + citation + kind); "
                "empty on the legacy path."
            ),
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


def overlap_verdict_batch_response_model() -> type:
    """Structured-output model for ONE BATCHED overlap-judge call (c403): the shared digest is
    judged against several candidates at once, so the response is a LIST of verdicts, each
    echoing the ``candidate_id`` it was given.

    The entry SUBCLASSES the single-pair model so the relation enum, its normalizing validator
    and the three other fields stay single-sourced — a batched relation and a single-pair one
    must never be able to drift apart. No JSON Schema file: like ``plan_review_novelty``, this
    is an internal contract with no ``--output`` surface.

    The entry's judgement fields are REQUIRED here, unlike on the single-pair parent (ticket
    d147). The parent's all-defaulted shape is a deliberate safe-default for ONE object — a
    sparse payload still reads as a non-surfacing verdict. Repeated across a LIST it is not
    safe, and measurably was not: with every field optional the model omitted ``confidence``,
    which defaulted to 0.0 and so could never clear ``overlap_conf_threshold``, and its
    multi-entry tool arguments degenerated outright (whole entries collapsing into the first
    string field). Live 6-candidate batches went from malformed-and-abstaining to 6/6 clean
    verdicts once these four fields were made required, because a required field is what tells
    the model the value is not optional to think about. ``shared_artifact`` stays optional: a
    null there is a MEANINGFUL answer ("no artifact I can name"), not an omission.

    A model that now omits a required field fails validation and — with the judge's
    ``structured_retry_limit=0`` — abstains the batch, which is the fail-safe direction: the
    overlap step is advisory, and a dropped finding is cheaper than a false one."""
    from pydantic import BaseModel, Field

    OverlapVerdict = overlap_verdict_response_model()

    class OverlapVerdictEntry(OverlapVerdict):  # type: ignore[misc,valid-type]
        # Declared FIRST so it is the first argument the model writes for each entry, anchoring
        # the entry to the id it is answering for before it starts judging.
        candidate_id: str = Field(
            description="The candidate_id given in the request, echoed back verbatim.",
        )
        relation: str = Field(
            description="REQUIRED. First <relation> Second (closed relation enum).",
        )
        confidence: float = Field(
            description=(
                "REQUIRED. Your honest confidence (0.0-1.0) in the relation stated for THIS "
                "candidate. Judge it per entry — never omit it, and never leave it at 0.0 "
                "unless you genuinely have no confidence in your own verdict."
            ),
        )
        abstain: bool = Field(
            description="REQUIRED. True when the judge is unsure about this candidate.",
        )

    class OverlapVerdictBatch(BaseModel):
        verdicts: list[OverlapVerdictEntry] = Field(
            default_factory=list, description="Exactly one entry per candidate in the request."
        )

    return OverlapVerdictBatch


def epic_bug_screen_verdict_response_model() -> type:
    """Structured-output model for one single-turn epic-close bug-screen call (ticket 4b54),
    mirroring ``epic_bug_screen_verdict.schema.json``. The normalizing validator's DEFAULT is
    ``C`` (unrelated) — any out-of-vocabulary, malformed, or missing verdict coerces to the
    NON-SURFACING value (the overlap_verdict safe-default idiom): a garbled screen output
    degrades open and never fabricates an A-candidate. pydantic imported inside the body."""
    from pydantic import BaseModel, Field, field_validator

    class EpicBugScreenVerdict(BaseModel):
        verdict: str = Field(
            default="C",
            description=(
                "Forced choice: A = defect in something this epic changed/built or behavior "
                "its AC claims; B = same subsystem, pre-existing or adjacent; C = unrelated."
            ),
        )
        citation: str = Field(
            default="",
            description="One line naming the epic deliverable / bug content that justifies it.",
        )

        @field_validator("verdict")
        @classmethod
        def _norm_verdict(cls, v: str) -> str:
            r = str(v).strip().upper()
            return r if r in {"A", "B", "C"} else "C"

    return EpicBugScreenVerdict


# Built-ins. ``review_result`` (the default findings shape) and ``completion_verdict``.
register_contract("review_result", findings.findings_response_model)
register_contract("completion_verdict", completion_verdict_response_model)
# Cupid ticket-digest op (epic only-crave-art, ee3d). Registered here — co-located with
# ``response_model_for`` — so importing this module to call it guarantees the digest
# contract is registered before first use (no startup-import ordering assumption).
register_contract("ticket_digest", ticket_digest_response_model)
# Stage-2 overlap judge (9022) — same co-location guarantee.
register_contract("overlap_verdict", overlap_verdict_response_model)
# The batched form of the same judge call (c403) — one entry per candidate.
register_contract("overlap_verdict_batch", overlap_verdict_batch_response_model)
# The epic-close bug screen's forced-choice verdict (4b54) — same co-location guarantee.
register_contract("epic_bug_screen_verdict", epic_bug_screen_verdict_response_model)
