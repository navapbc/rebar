"""Pass-3 of the plan-review gate: the `plan_review_decide` op and its operator-attested pre-step.

Extracted from `workflow_ops.py` (ticket b5fe), which stood at 794 LOC against the absolute
800-line CI cap in `.github/module-size-limit.txt` while holding two of the five
`orchestrator.finalize_verdict` call sites. `plan_review_decide` alone was 213 of those lines —
27% of the file and 2.05x the next-largest op — so relieving the file means moving it, not
trimming around it.

The operator-attested enrichment cluster (`operator_attested_ac_texts`, `_norm`,
`enrich_operator_attested`) travels with it because `enrich_operator_attested` has exactly ONE
caller in `src/` — `plan_review_decide` — and runs as a Pass-3 pre-step by construction: it clears
`ac_unverifiable` BEFORE Pass-3 reads the verifications. `_ticket_id` comes along because `decide`
is its heaviest caller; `workflow_ops` re-imports it rather than keeping a second copy (a third
copy already exists at `workflow/steps.py:36`, and more would be drift-prone).

HONEST LIMIT: this RELOCATES the absorber rather than dissolving it. `plan_review_decide` is still
~213 lines; it now lives in a file with room instead of one at the cap. The follow-up — deliberately
NOT bundled here because it is behaviour-adjacent and needs its own RED-first test — is the verb cut
inside the op: lifting the prerequisite-coverage normalisation (the `normalized_coverage` loop, at
`workflow_ops.py:572-629` before this move) plus the blocking→indeterminate reclassification that
depends on it (`workflow_ops.py:646-677`) into `prerequisites.py` as a pure function over
(coverage records, invalid ids, too-large ids).

TWO IMPORT PINS are load-bearing here and are asserted by `tests/unit/test_decide_ops_seam.py`:

  * `_OPERATOR_ATTESTED_AC_RE` is a re-export of `det_operator_attested._OPERATOR_ATTESTED_TAG_RE`
    by OBJECT IDENTITY — `tests/unit/test_det_floor_operator_attested.py:153` asserts `is`, not
    equality, so recompiling the pattern here would pass `==` and fail that test.
  * `orchestrator` is reached by MODULE FORM plus attribute access, never flattened to bare-name
    imports. `tests/interfaces/lifecycle/test_plan_review_execution_floor_lifecycle.py`
    monkeypatches `orchestrator.pass3_over_findings` and then calls this op; a flattened import
    would bind the original function at import time, so the test would pass while exercising
    unpatched code.

This module is a LEAF: it must not import `workflow_ops` at module scope — `workflow_ops` imports
it (to populate the `@register_step` process-global registry as an import side effect), so a
module-scope back-import would close a cycle.
"""

from __future__ import annotations

import logging
from typing import Any

from rebar.llm.plan_review.det_operator_attested import (
    _OPERATOR_ATTESTED_TAG_RE as _OPERATOR_ATTESTED_AC_RE,
)
from rebar.llm.workflow.executor import StepContext, register_step
from rebar.llm.workflow.step_contracts import _ticket_id

logger = logging.getLogger(__name__)


# ── a8e5 Component 3: operator-attested AC awareness (pure DET, ADR-0043) ─────────────────────
# An AC tagged `- [ ] [operator-attested] …` has "done" evidence that inherently lives OUTSIDE
# the codebase (a deploy, a live drill), so a plan-review finding that flags it as
# in-session-UNVERIFIABLE (the ac_unverifiable hard-override axis) is a FALSE POSITIVE: the
# in-session unverifiability is by design. We DET-detect the tag and, for a finding that
# references such an AC, clear ac_unverifiable BEFORE impact_plan reads it — leaving the kernel
# impact_plan/pass3 math byte-unchanged (the fact is injected upstream, not taught to the kernel).
# The tag matcher ``_OPERATOR_ATTESTED_AC_RE`` is a re-export of det_operator_attested's
# canonical ``_OPERATOR_ATTESTED_TAG_RE`` (single source, ticket b080) so this enrichment path
# and the plan-time operator-attested lint (det_operator_attested.operator_evidence_ac_gaps)
# agree on "tagged".


def operator_attested_ac_texts(description: str) -> list[str]:
    """Extract the criterion text of every AC checklist line tagged with the EXACT
    case-insensitive token ``[operator-attested]`` (ADR-0043). The tag is stripped and the text
    trimmed. Matching is exact on the hyphenated token — a near-miss like ``[operator_attested]``
    is NOT operator-attested. Returns ``[]`` when none are tagged."""
    return [m.strip() for m in _OPERATOR_ATTESTED_AC_RE.findall(description or "")]


def _norm(s: str) -> str:
    """Whitespace/case-normalize for substring matching."""
    return " ".join((s or "").lower().split())


def enrich_operator_attested(
    findings: list[dict[str, Any]], verifs: dict[int, dict[str, Any]], description: str
) -> None:
    """DET-enrich verifications in place (mirrors code_review ``_det_enrich_verifications``): for a
    finding that REFERENCES an operator-attested AC, inject ``operator_attested=True`` into its
    ``severity_attributes`` and CLEAR the ``ac_unverifiable`` axis to ``"none"`` (an
    operator-attested AC's in-session unverifiability is by design, not a defect). A finding
    references an operator-attested AC iff a non-empty normalized operator-attested criterion text
    is a substring of the finding's combined normalized text (location + finding + checklist_item +
    evidence). Fail-safe: never raises on missing keys / bad shapes; a miss leaves the finding
    untouched (the conservative direction — a surviving advisory, never a spurious clear)."""
    oa_texts = [_norm(t) for t in operator_attested_ac_texts(description)]
    oa_texts = [t for t in oa_texts if t]
    if not oa_texts:
        return
    for i, f in enumerate(findings):
        verif = verifs.get(i)
        if not isinstance(verif, dict):
            continue
        attrs = verif.get("severity_attributes")
        if not isinstance(attrs, dict):
            attrs = {}
            verif["severity_attributes"] = attrs
        combined = _norm(
            " ".join(
                [
                    str(f.get("location", "")),
                    str(f.get("finding", "")),
                    str(f.get("checklist_item", "")),
                    " ".join(str(e) for e in (f.get("evidence") or [])),
                ]
            )
        )
        if any(oa in combined for oa in oa_texts):
            attrs["operator_attested"] = True
            # A recorded attestation IS the oracle, so it clears missing/underspecified —
            # but never broken_oracle: a factually wrong stated command is not cured by
            # attesting the outcome (story large-sleepful-needlefish).
            if attrs.get("ac_unverifiable") in ("missing_oracle", "underspecified_oracle"):
                attrs["ac_unverifiable"] = "none"
            # Same rule on the divergence axis (story doggish-nonorganic-tsetsefly, plan-v4): an
            # attestation clears a merely-cosmetic incomplete_enumeration, but NEVER a
            # contradicts_reality or omits_required_site — attesting an outcome does not make a
            # false claim about the code true, nor conjure a required site the plan omits.
            if attrs.get("divergent_implementation") == "incomplete_enumeration":
                attrs["divergent_implementation"] = "none"


@register_step(
    "plan_review_decide",
    input_schema="plan_review_decide_input",
    output_schema="plan_review_decide_output",
    description=(
        "Pass-3 of the gate: route the size-ladder's too_big findings (DET-style BLOCKS) and "
        "budget-shed findings (INDETERMINATE), run the deterministic pass3_decide over the rest "
        "(Pass-1 findings + the Pass-2 verifier's verifications), then merge the DET-floor "
        "findings and apply the advisory cap. Emits the verdict partition the coach assembles. "
        "Reuses orchestrator.pass3_over_findings + partition_findings (no duplicated decision)."
    ),
)
def plan_review_decide(ctx: StepContext) -> dict[str, Any]:
    """too_big/shed routing + Pass-3 over (batch findings, verifier verifications) →
    merge DET findings → cap → the verdict partition (blocking/surfaced/overflow/...)."""
    from rebar.llm import review_kernel

    from . import context_assembly, orchestrator

    findings = list(ctx.inputs.get("findings") or [])
    raw_verifs = list(ctx.inputs.get("verifications") or [])
    det_blocks = list(ctx.inputs.get("det_blocking") or [])
    det_advisories = list(ctx.inputs.get("det_advisory") or [])
    # The workflow schema requires this; direct legacy/unit callers model planning by omission.
    review_phase = ctx.inputs.get("review_phase", "planning")
    has_prerequisites = bool(ctx.inputs.get("has_prerequisites", False))
    prerequisite_coverage = list(ctx.inputs.get("prerequisite_coverage") or [])
    prerequisite_findings = list(ctx.inputs.get("prerequisite_findings") or [])
    prerequisite_raw_verifs = list(ctx.inputs.get("prerequisite_verifications") or [])
    prerequisite_input_too_large_ids = {
        str(value) for value in (ctx.inputs.get("prerequisite_input_too_large_ids") or [])
    }
    if has_prerequisites:
        required_focused = {
            "prerequisite_coverage",
            "prerequisite_findings",
            "prerequisite_verifications",
        }
        if not required_focused.issubset(ctx.inputs):
            raise ValueError("has_prerequisites=true requires all focused review arrays")
        from .prerequisites import prerequisite_coverage_model

        prerequisite_coverage_model().model_validate({"records": prerequisite_coverage})
        coverage_ids = [str(r.get("prerequisite_id", "")) for r in prerequisite_coverage]
        if not coverage_ids or len(coverage_ids) != len(set(coverage_ids)):
            raise ValueError("focused prerequisite coverage must contain unique records")

    # The Pass-2 verifier (the workflow's `verify` prompt step) emits a flat list of
    # `{index, severity_attributes, binary}`; reshape it to the `{index: {...}}` map Pass-3
    # consumes via the SHARED structural reshape seam (review_kernel.reshape_verifications) — the
    # SINGLE place the verifier→decide keying contract lives, so this op no longer re-implements
    # the silent-drop. `valid_indices` is the batch index domain the verifier ran over. The map is
    # byte-identical to the prior inline reshape; what is NEW is the contract-violation REPORT
    # (malformed / duplicate / out-of-range indices) — surfaced loudly per the expand-contract
    # posture (ERROR log + a run-scoped record drained into verdict coverage) with NO change to
    # the decisions/verdict (a finding with no verification still degrades to INDETERMINATE).
    reshape = review_kernel.reshape_verifications(raw_verifs, valid_indices=range(len(findings)))
    verifs = reshape.verifications
    if reshape.has_violations:
        logger.error(
            "plan-review Pass-2 verification contract violation (findings degrade to "
            "INDETERMINATE; verdict unchanged): %s",
            reshape.summary(),
        )
        review_kernel.record_contract_violation(reshape.summary())

    # a8e5 Component 3: operator-attested AC awareness. Clear ac_unverifiable on a finding that
    # flags an operator-attested AC as in-session-unverifiable BEFORE Pass-3 reads it (fail-open:
    # any read failure skips enrichment, never breaks the decide step).
    try:
        _desc = context_assembly.assemble_context(
            _ticket_id(ctx), repo_root=ctx.repo_root
        ).description
        enrich_operator_attested(findings, verifs, _desc)
    except Exception:
        logger.debug("operator-attested enrichment skipped", exc_info=True)

    # The size-ladder's "too big at the largest model" findings are DET-style BLOCKS;
    # budget-shed findings are pre-decided INDETERMINATE. Both bypass Pass-2/3. The rest are
    # decided by pass3_over_findings with verifications re-keyed to the rest's 0-based index
    # (the verifier ran over the full batch list; we pick the matching verifications).
    too_big = [
        {
            **f,
            "decision": "block",
            "severity": "critical",
            "priority": 1.0,
            "validity": 1.0,
            "impact": 1.0,
        }
        for f in findings
        if f.get("_too_big")
    ]
    shed = [f for f in findings if f.get("_shed")]
    rest: list[dict[str, Any]] = []
    rest_verifs: dict[int, dict[str, Any]] = {}
    for i, f in enumerate(findings):
        if f.get("_too_big") or f.get("_shed"):
            continue
        verif = verifs.get(i)
        if verif is not None:  # absent == None to the consumer's verifs.get(i)
            rest_verifs[len(rest)] = verif
        rest.append(f)
    focused_reshape = review_kernel.reshape_verifications(
        prerequisite_raw_verifs, valid_indices=range(len(prerequisite_findings))
    )
    invalid_prerequisites: set[str] = set()
    if focused_reshape.has_violations:
        invalid_prerequisites.update(
            str(f.get("prerequisite_id", "")) for f in prerequisite_findings
        )
    focused_findings: list[dict[str, Any]] = []
    focused_verifs: dict[int, dict[str, Any]] = {}
    for index, finding in enumerate(prerequisite_findings):
        pid = str(finding.get("prerequisite_id", ""))
        if pid in prerequisite_input_too_large_ids:
            continue
        verification = focused_reshape.verifications.get(index)
        attribution = (
            (verification.get("binary") or {}).get("prerequisite_attribution_valid", "na")
            if isinstance(verification, dict)
            else "na"
        )
        if not pid or attribution != "yes":
            invalid_prerequisites.add(pid)
            continue
        assert verification is not None
        focused_verifs[len(rest) + len(focused_findings)] = dict(verification)
        focused_findings.append(finding)

    normalized_coverage: list[dict[str, Any]] = []
    seen_coverage_ids: set[str] = set()
    for record in sorted(prerequisite_coverage, key=lambda r: str(r.get("prerequisite_id", ""))):
        pid = str(record.get("prerequisite_id", ""))
        if not pid or pid in seen_coverage_ids:
            invalid_prerequisites.add(pid)
            continue
        seen_coverage_ids.add(pid)
        if pid in prerequisite_input_too_large_ids:
            normalized_coverage.append(
                {
                    "prerequisite_id": pid,
                    "disposition": "indeterminate",
                    "findings": [],
                    "reason_code": "evaluation-error",
                    "detail": "input-too-large",
                }
            )
        elif pid in invalid_prerequisites:
            normalized_coverage.append(
                {
                    "prerequisite_id": pid,
                    "disposition": "indeterminate",
                    "findings": [],
                    "reason_code": "attribution-invalid",
                }
            )
        else:
            normalized_coverage.append(record)
    if has_prerequisites and not normalized_coverage:
        raise ValueError("has_prerequisites=true requires complete prerequisite coverage")

    combined_verifs = dict(rest_verifs)
    combined_verifs.update(focused_verifs)
    decided = [
        *too_big,
        *shed,
        *orchestrator.pass3_over_findings(
            [*rest, *focused_findings],
            combined_verifs,
            execution_review=review_phase == "execution",
        ),
    ]

    parts = orchestrator.partition_findings(
        det_blocks, det_advisories, decided, advisory_cap=orchestrator.DEFAULT_ADVISORY_CAP
    )
    prerequisite_indeterminate = any(
        record.get("disposition") == "indeterminate" for record in normalized_coverage
    )
    if prerequisite_indeterminate:
        from .prerequisites import emit_indeterminate

        for record in normalized_coverage:
            if (
                record.get("reason_code") == "attribution-invalid"
                or record.get("prerequisite_id") in prerequisite_input_too_large_ids
            ):
                emit_indeterminate(
                    record,
                    ticket_id=_ticket_id(ctx),
                    model=None,
                    attempts=1,
                    bin_size=len(normalized_coverage),
                )
        retained_det = [f for f in parts["blocking"] if f.get("tier") == "DET"]
        reclassified = [f for f in parts["blocking"] if f.get("tier") != "DET"]
        parts["blocking"] = retained_det
        parts["indeterminate"] = [
            *parts["indeterminate"],
            *(
                {
                    **f,
                    "decision": "indeterminate",
                    "reason": "prerequisite-coverage-indeterminate",
                }
                for f in reclassified
            ),
        ]
    outcome_counts = review_kernel.decide_outcome_counts(raw_verifs, findings, reshape)
    return {
        **dict(parts),
        "prerequisite_coverage": normalized_coverage,
        "outcome_counts": outcome_counts,
    }
