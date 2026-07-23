"""Focused direct-prerequisite review contracts and normalization helpers."""

from __future__ import annotations

import hashlib
import logging
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Literal

PREREQUISITE_CRITERION = "prerequisite-consistency"
PREREQUISITE_REASON_CODES = (
    "evaluation-error",
    "output-invalid",
    "attribution-invalid",
)
logger = logging.getLogger(__name__)
PREREQUISITE_INDETERMINATE_COUNTS: Counter[str] = Counter()
_preloaded_blocks: ContextVar[tuple[dict[str, Any], ...]] = ContextVar(
    "plan_review_preloaded_prerequisite_blocks", default=()
)


@contextmanager
def focused_inputs(blocks: list[dict[str, Any]]):
    """Publish one run's already-collected blocks without another store read."""
    token = _preloaded_blocks.set(tuple(dict(block) for block in blocks))
    try:
        yield
    finally:
        _preloaded_blocks.reset(token)


def current_blocks() -> list[dict[str, Any]]:
    return [dict(block) for block in _preloaded_blocks.get()]


def mint_finding_digest(finding: dict[str, Any]) -> str:
    """Preserve legacy identity or mint the versioned prerequisite identity."""
    text = str(finding.get("finding", ""))
    criteria = ",".join(sorted(finding.get("criteria", [])))
    prerequisite_id = finding.get("prerequisite_id")
    if prerequisite_id is None:
        encoded = (text + "|" + criteria).encode("utf-8")
    else:
        fields = ("prerequisite-finding-id-v1", text, criteria, str(prerequisite_id))
        encoded = b"".join(
            len(raw).to_bytes(4, "big") + raw for raw in (field.encode("utf-8") for field in fields)
        )
    return hashlib.sha256(encoded).hexdigest()[:16]


def prerequisite_coverage_model() -> Any:
    """Return the closed structured-output contract for focused Pass 1."""
    from pydantic import BaseModel, ConfigDict, Field, model_validator

    class PrerequisiteFinderFinding(BaseModel):
        model_config = ConfigDict(extra="forbid")
        finding: str
        criteria: list[Literal["prerequisite-consistency"]]
        location: str = ""
        evidence: list[str] = Field(default_factory=list)
        scenarios: list[str] = Field(default_factory=list)
        impact: str = ""
        checklist_item: str = ""
        suggested_fix: str = ""
        prerequisite_id: str

    class PrerequisiteCoverageRecord(BaseModel):
        model_config = ConfigDict(extra="forbid")
        prerequisite_id: str
        disposition: Literal["consistent", "finding", "indeterminate"]
        findings: list[PrerequisiteFinderFinding] = Field(default_factory=list)
        reason_code: Literal["evaluation-error", "output-invalid", "attribution-invalid"] | None = (
            None
        )
        detail: str | None = None

        @model_validator(mode="after")
        def validate_disposition(self):
            if any(f.prerequisite_id != self.prerequisite_id for f in self.findings):
                raise ValueError("finding prerequisite_id must match its enclosing record")
            if self.disposition == "consistent" and (self.findings or self.reason_code):
                raise ValueError("consistent records contain neither findings nor reason_code")
            if self.disposition == "finding" and (not self.findings or self.reason_code):
                raise ValueError("finding records require findings and no reason_code")
            if self.disposition == "indeterminate" and (self.findings or self.reason_code is None):
                raise ValueError("indeterminate records require a reason_code and no findings")
            return self

    class PrerequisiteCoverageOutput(BaseModel):
        model_config = ConfigDict(extra="forbid")
        records: list[PrerequisiteCoverageRecord] = Field(default_factory=list)

    return PrerequisiteCoverageOutput


def normalize_coverage_records(
    raw: Any, expected_ids: list[str] | tuple[str, ...]
) -> list[dict[str, Any]]:
    """Return exactly one valid record for every expected canonical id.

    Invalid/missing/duplicate/unknown output cannot assert incompatibility: every
    affected expected id becomes ``indeterminate/output-invalid``.
    """
    expected = tuple(sorted(set(expected_ids)))
    try:
        payload = {"records": raw["records"]} if isinstance(raw, dict) and "records" in raw else raw
        parsed = prerequisite_coverage_model().model_validate(payload).model_dump()
    except Exception:  # noqa: BLE001 - model output is an untrusted boundary
        return [_indeterminate(i, "output-invalid") for i in expected]
    records = parsed["records"]
    ids = [r["prerequisite_id"] for r in records]
    if len(ids) != len(set(ids)) or set(ids) != set(expected):
        return [_indeterminate(i, "output-invalid") for i in expected]
    return sorted(records, key=lambda r: r["prerequisite_id"])


def _indeterminate(
    prerequisite_id: str, reason_code: str, detail: str | None = None
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "prerequisite_id": prerequisite_id,
        "disposition": "indeterminate",
        "findings": [],
        "reason_code": reason_code,
    }
    if detail:
        record["detail"] = detail
    return record


def emit_indeterminate(
    record: dict[str, Any], *, ticket_id: str, model: str | None, attempts: int, bin_size: int
) -> None:
    """Emit metadata-only observability for one unresolved prerequisite."""
    reason = str(record.get("reason_code", "output-invalid"))
    PREREQUISITE_INDETERMINATE_COUNTS[reason] += 1
    fields = {
        "event": "plan_review_prerequisite_indeterminate",
        "ticket_id": ticket_id,
        "prerequisite_id": str(record.get("prerequisite_id", "")),
        "reason_code": reason,
        "model": model,
        "attempt_count": attempts,
        "bin_size": bin_size,
    }
    logger.warning("plan_review_prerequisite_indeterminate %s", fields, extra=fields)


def render_blocks(blocks: list[dict[str, Any]]) -> str:
    """Render authoritative blocks with unambiguous, stable delimiters."""
    return "\n\n".join(
        f'<prerequisite id="{block["canonical_id"]}">\n'
        f"{block.get('rendered_text', '')}\n</prerequisite>"
        for block in sorted(blocks, key=lambda b: str(b.get("canonical_id", "")))
    )


def run_focused_finder(
    runner,
    cfg,
    *,
    subject_plan: str,
    blocks: list[dict[str, Any]],
    ticket_id: str = "",
):
    """Run prerequisite-only Pass 1 over stable whole-block bins."""
    from rebar.llm.prompting import prompts
    from rebar.llm.runner import RunRequest

    from . import passes, sizing

    prompt = prompts.get_prompt(passes.PASS_PREREQUISITE_FINDER, repo_root=cfg.repo_path)
    system, _ = prompts.resolve_prompt(prompt, {"plan": subject_plan}, repo_root=cfg.repo_path)
    typed = [
        sizing.PrerequisiteBlock(str(b["canonical_id"]), str(b.get("rendered_text", "")))
        for b in blocks
    ]
    bins, oversized = sizing.pack_prerequisite_bins(
        typed, subject_plan=subject_plan, system_prompt=system, model=cfg.model
    )
    records: list[dict[str, Any]] = [
        _indeterminate(block.canonical_id, "evaluation-error", "input-too-large")
        for block in oversized
    ]

    # prerequisite_id -> (model that produced the record, number of model attempts). A ladder
    # escalation re-runs a bin on a HIGHER model, so the producing model is not always cfg.model;
    # emit_indeterminate must report what actually ran, not what was configured (client report §4).
    produced: dict[str, tuple[str | None, int]] = {}

    def run_bin(bin_: list[Any], model: str | None, attempt: int = 1) -> list[dict[str, Any]]:
        import dataclasses

        ids = [block.canonical_id for block in bin_]

        def _from(model_used: str | None, records_: list[dict[str, Any]]) -> list[dict[str, Any]]:
            # Record the producing model/attempt for every id this terminal path yielded.
            for pid in ids:
                produced[pid] = (model_used, attempt)
            return records_

        instructions = (
            "Judge each subject-to-prerequisite pair independently. Return exactly one record "
            "per id; never compare prerequisites with each other.\n\n"
            + render_blocks(
                [{"canonical_id": b.canonical_id, "rendered_text": b.rendered_text} for b in bin_]
            )
        )
        try:
            call_cfg = dataclasses.replace(cfg, model=model)
            raw = runner.run(
                RunRequest(
                    system_prompt=system,
                    instructions=instructions,
                    config=call_cfg,
                    reviewers=["plan-reviewer"],
                    mode="structured",
                    output_schema="plan_review_prerequisite_coverage",
                    execution_mode="single_turn",
                )
            )
            return _from(model, normalize_coverage_records(raw, ids))
        except Exception as exc:  # noqa: BLE001 - unresolved provider output is indeterminate
            if not sizing.is_context_limit_error(exc):
                return _from(
                    model,
                    [_indeterminate(pid, "evaluation-error", "provider-failure") for pid in ids],
                )
            if len(bin_) > 1:
                # A split is a subdivision, not a model retry: the sub-bins keep the same attempt
                # depth and each records its own producing model.
                middle = len(bin_) // 2
                return [
                    *run_bin(bin_[:middle], model, attempt),
                    *run_bin(bin_[middle:], model, attempt),
                ]
            ladder = sizing.models_at_or_above(model)
            try:
                next_model = ladder[ladder.index(model) + 1] if model in ladder else ladder[0]
            except (ValueError, IndexError):
                return _from(model, [_indeterminate(ids[0], "evaluation-error", "input-too-large")])
            return run_bin(bin_, next_model, attempt + 1)

    for bin_ in bins:
        records.extend(run_bin(bin_, cfg.model))
    records.sort(key=lambda record: record["prerequisite_id"])
    for record in records:
        if record.get("disposition") == "indeterminate":
            used_model, used_attempts = produced.get(
                str(record.get("prerequisite_id", "")), (cfg.model, 1)
            )
            emit_indeterminate(
                record,
                ticket_id=ticket_id,
                model=used_model,
                attempts=used_attempts,
                bin_size=len(blocks),
            )
    findings = [finding for record in records for finding in record.get("findings", [])]
    return records, findings
