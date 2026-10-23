"""Share verification, decision, and coaching rules across review gates.

Pass 2 verifies cited findings through independent binary questions under one
registered contract. Pass 3 applies deterministic validity, impact, and veto
rules with gate-supplied thresholds. Pass 4 maps surviving advisories to an
affirmative move registry. Gate-specific rubrics, routing, context, verification
preambles, and move content stay with each consumer. The shared kernel keeps
plan and code review on one decision and verification vocabulary."""

from __future__ import annotations

from .coach import (
    MOVE_REGISTRY_SCHEMA,
    applicable_moves,
    coach,
    coach_listing,
    move_applies,
    render_coach_notes,
    validate_move_registry,
    validate_subject,
)
from .decide import (
    DEFAULT_BLOCK_THRESHOLD,
    GRADED_BINARY,
    dedup_key,
    finding_body,
    impact,
    impact_code,
    impact_plan,
    is_contentless_finding,
    pass3_decide,
    pass3_over_findings,
    severity_label,
    suppress_and_dedup,
    validity,
)
from .discovery import (
    DISCOVERY_NAMESPACE_VERSION,
    OUTCOME_KINDS,
    CheckpointEnvelope,
    DiscoveryStagePlan,
    DiscoveryStageResult,
    DiscoveryUnitPlan,
    LocalOperationExhausted,
    MemoryCheckpointStore,
    SystemicDiscoveryError,
    UnitOutcome,
    Usage,
    execute_stage,
    unit_trace,
)
from .telemetry import (
    collect_contract_violations,
    decide_outcome_counts,
    drain_contract_violations,
    record_contract_violation,
)
from .verify import (
    DEFAULT_VERIFY_WINDOW_HEADROOM,
    VERIFIER_RULES,
    VERIFIER_RULES_SCAFFOLD,
    VerificationReshape,
    code_review_verification_model,
    finding_listing,
    max_output_cfg,
    merge_verifications_by_index,
    model_max_output_tokens,
    plan_review_verification_model,
    register_verification_contract,
    reshape_verifications,
    resolve_verifier_model,
    verification_model,
    verify_findings,
    verify_instructions,
    verify_request_chunks,
)

__all__ = [
    # Pass-3 — deterministic decision
    "DEFAULT_BLOCK_THRESHOLD",
    # Pass-2 — finding verifier + the verification contract
    "DEFAULT_VERIFY_WINDOW_HEADROOM",
    "DISCOVERY_NAMESPACE_VERSION",
    "GRADED_BINARY",
    "MOVE_REGISTRY_SCHEMA",
    "OUTCOME_KINDS",
    "VERIFIER_RULES",
    "VERIFIER_RULES_SCAFFOLD",
    # RP-06 — the shared discovery-execution kernel
    "CheckpointEnvelope",
    "DiscoveryStagePlan",
    "DiscoveryStageResult",
    "DiscoveryUnitPlan",
    # Pass-4 — coach mechanism + move-registry schema
    "LocalOperationExhausted",
    "MemoryCheckpointStore",
    "SystemicDiscoveryError",
    "UnitOutcome",
    "Usage",
    "VerificationReshape",
    "applicable_moves",
    "coach",
    "coach_listing",
    "code_review_verification_model",
    # Shared run-scoped telemetry — contract-violation sink + decide-boundary outcome_counts
    "collect_contract_violations",
    "decide_outcome_counts",
    "dedup_key",
    "drain_contract_violations",
    "execute_stage",
    "finding_body",
    "finding_listing",
    "impact",
    "impact_code",
    "impact_plan",
    "is_contentless_finding",
    "max_output_cfg",
    "merge_verifications_by_index",
    "model_max_output_tokens",
    "move_applies",
    "pass3_decide",
    "pass3_over_findings",
    "plan_review_verification_model",
    "record_contract_violation",
    "register_verification_contract",
    "render_coach_notes",
    "reshape_verifications",
    "resolve_verifier_model",
    "severity_label",
    "suppress_and_dedup",
    "unit_trace",
    "validate_move_registry",
    "validate_subject",
    "validity",
    "verification_model",
    "verify_findings",
    "verify_instructions",
    "verify_request_chunks",
]
