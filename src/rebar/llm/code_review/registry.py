"""Code-review criteria registry — WS1 seed: the closed overlay-id enum + the
deterministic ``recommend_overlays`` filter + the base-reviewer failure fallback.

This module is the single source of truth for the overlay-id vocabulary. WS1 OWNS
the closed :data:`OVERLAY_IDS` enum (the ids the base reviewer may escalate to);
WS2 ADDS the catalog CONTENT (overlay finder prompts + ``applies_to`` globs + the
``threshold_for`` posture resolver) keyed by these same ids — it never introduces a
new id, so the base reviewer's enum and the catalog can never drift.

The enum is enforced POST-HOC (after the agent returns) by :func:`filter_recommend_overlays`
rather than as a strict JSON-Schema ``enum`` on ``overlay_id``: a ``mode="structured"``
step validates its output against the schema, and a strict enum would turn an
out-of-catalog id into a hard validation error (failing the whole base step). The
contract instead is "the model cannot ESCALATE to an unknown overlay" — an unknown id
is silently DROPPED, never errored — so a hallucinated id costs nothing.
"""

from __future__ import annotations

from typing import Any, TypeGuard

# ── The closed overlay-id vocabulary (WS1 OWNS this) ──────────────────────────────────────
# The 11 specialist overlays the base reviewer may escalate to. WS2 authors the per-id
# finder prompt + applies_to globs; adding a NEW overlay means adding its id HERE and its
# content in WS2 — the two cannot drift because both derive from this tuple.
OVERLAY_IDS: tuple[str, ...] = (
    "security",  # authn/authz, secrets, injection, unsafe deserialization
    "performance",  # hot paths, N+1, allocation, complexity regressions
    "i18n",  # localization / encoding / locale-sensitive formatting
    "a11y",  # accessibility (UI/markup/ARIA)
    "db-migrations",  # schema/data migrations, backfills, expand-contract
    "docs",  # user/operator/API docs that must track the change
    "supply-chain",  # dependency / lockfile / vendoring / provenance changes
    "api-compat",  # public API / wire / CLI / config backward-compatibility
    "iac",  # infrastructure-as-code (Terraform/CDK/K8s/Helm/Ansible)
    "tests",  # test sufficiency / regression coverage for the change
    "llm-prompts",  # prompt/contract/output-schema changes to LLM surfaces
)

# Operational policy (config, not a magic constant baked into the wire schema): the
# escalation reason is a one-liner. An overlong reason is TRUNCATED here, never errored.
REASON_MAX_CHARS = 200

# The dimension tag carried by a coverage-gap note (surfaced in the verdict's `coverage`,
# never run through Pass-2 verify — a coverage gap is not a verifiable claim about the diff).
COVERAGE_GAP_DIMENSION = "coverage-gap"

BASE_REVIEWER_ID = "code-review-base"


def is_overlay_id(value: object) -> TypeGuard[str]:
    """True iff ``value`` is a member of the closed :data:`OVERLAY_IDS` enum (narrows to
    ``str`` for the caller)."""
    return isinstance(value, str) and value in OVERLAY_IDS


def overlay_id_enum() -> list[str]:
    """The overlay-id vocabulary as a list (the single source the base-reviewer prompt
    enumerates and the schema doc references — derived from :data:`OVERLAY_IDS` so a
    drift between the prompt, the schema, and the filter is impossible)."""
    return list(OVERLAY_IDS)


def filter_recommend_overlays(
    raw: Any, *, reason_max: int = REASON_MAX_CHARS
) -> list[dict[str, str]]:
    """Normalize a base reviewer's ``recommend_overlays`` to the valid, bounded set.

    Drops (does NOT error on) any entry whose ``overlay_id`` is not in :data:`OVERLAY_IDS`
    or whose ``reason`` is missing/blank; truncates an overlong ``reason`` to
    ``reason_max``; de-duplicates by ``overlay_id`` (first occurrence wins, preserving
    order). A non-list / malformed input yields ``[]`` (fail-soft — the base reviewer is
    recall-side, never the verdict)."""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        oid = entry.get("overlay_id")
        if not is_overlay_id(oid) or oid in seen:
            continue
        reason = entry.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            continue
        seen.add(oid)  # oid narrowed to str by is_overlay_id (TypeGuard)
        out.append({"overlay_id": oid, "reason": reason.strip()[:reason_max]})
    return out


def recommend_overlay_ids(raw: Any) -> list[str]:
    """Just the valid, de-duplicated overlay ids from a ``recommend_overlays`` list
    (the escalation signal :mod:`overlay_union` (WS3) unions with the glob triggers)."""
    return [o["overlay_id"] for o in filter_recommend_overlays(raw)]


def coverage_gap_note(detail: str, *, reviewer_id: str = BASE_REVIEWER_ID) -> dict[str, Any]:
    """A finding-shaped coverage-gap note (advisory, low severity). Carried OUTSIDE the
    ``findings`` list (in ``coverage_gaps``) so it surfaces in the verdict's coverage
    without being run through Pass-2 verify."""
    return {
        "dimension": COVERAGE_GAP_DIMENSION,
        "severity": "low",
        "detail": detail,
        "reviewer_id": reviewer_id,
    }


def base_failure_result(reason: str) -> dict[str, Any]:
    """The deterministic fallback when the base reviewer step errors / times out / returns
    no structured output: EMPTY base findings, NO base-driven escalations (Round-B
    membership falls back to glob-triggered overlays only), and a coverage-gap note. Never
    a synthetic BLOCK — the base reviewer is recall-side; deterministic Pass-2/3 still runs
    on whatever findings exist."""
    return {
        "findings": [],
        "recommend_overlays": [],
        "coverage_gaps": [coverage_gap_note(f"base code-reviewer unavailable: {reason}")],
    }
