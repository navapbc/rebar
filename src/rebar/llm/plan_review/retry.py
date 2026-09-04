"""Exact review-plan retry (story RP-06 S5 — ``rebar review-plan <id> --retry``).

An explicit operator override that RESUMES only the exact latest retained review when
that review is a retryable ``INDETERMINATE`` — reusing the checkpointed findings of the
units that already succeeded and issuing model calls only for the missing units, under a
FRESH per-invocation attempt budget. It layers ONLY two things over the existing
chunk-checkpoint resume seam (:func:`sizing.load_checkpoint`, already exercised by a
normal re-run): a latest-review ELIGIBILITY gate and a fresh attempt budget. It
introduces no second resume mechanism.

Eligibility (all must hold, else REFUSE before any model call → exit 2 + full-review
remedy on stderr):

* the latest usable ``REVIEW_RESULT`` sidecar payload is ``INDETERMINATE``;
* it carries a versioned ``discovery_journal`` (``version`` / ``namespace_version`` both
  current — a legacy/corrupt/version-mismatched journal never seeds retry);
* it has at least one RETRYABLE missing unit — a journal unit of kind ``failed`` /
  ``cancelled``, or at least one recorded ``budget-cap-shed`` finding; and
* it is not STALE — the ticket's current material fingerprint, the review code SHA, and
  the criteria-registry version all still match the payload's stamps, and every reusable
  (``success`` / ``resumed``) unit's checkpoint still loads.

Reuse-only lineage: a review that has never been retried carries no ``retry_lineage`` and
is NOT seeded a fabricated attempt-0 (no legacy seeding); the first retry starts the
cumulative record fresh.
"""

from __future__ import annotations

from typing import Any

from rebar.llm.review_kernel import DISCOVERY_NAMESPACE_VERSION

from . import attest, checkpoints, claimability, sidecar
from .sidecar import DISCOVERY_JOURNAL_VERSION, RETRY_LINEAGE_VERSION

# Journal unit kinds whose checkpointed success can be reused (they carry a lineage digest).
_REUSABLE_KINDS = frozenset({"success", "resumed"})
# Journal unit kinds that must be re-attempted (they produced no reusable checkpoint).
_MISSING_KINDS = frozenset({"failed", "cancelled"})
# The recorded-finding marker for a criterion shed under the review-time budget cap. A shed
# criterion emits NO journal unit, so the retryable-missing set is enumerated from BOTH the
# journal (failed/cancelled) and these findings.
_SHED_REASON = "budget-cap-shed"

REMEDY = (
    "review-plan --retry resumes only the latest review, and only when it is a retryable "
    "INDETERMINATE with a current discovery journal (a failed unit or a budget-shed "
    "criterion, and no stale material/code/registry). This review is not eligible; run "
    "`rebar review-plan <id>` for a full review instead."
)


def _valid_journal(journal: Any) -> bool:
    """True when ``journal`` is a current versioned discovery journal. A missing journal
    (legacy payload) or a version/namespace mismatch (corrupt/legacy-namespace) is not
    reusable and never seeds retry."""
    if not isinstance(journal, dict):
        return False
    units = journal.get("units")
    return (
        journal.get("version") == DISCOVERY_JOURNAL_VERSION
        and journal.get("namespace_version") == DISCOVERY_NAMESPACE_VERSION
        and isinstance(units, list)
    )


def _partition_units(journal: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    """Split the journal's units into (reusable, missing) by kind."""
    reusable: list[dict] = []
    missing: list[dict] = []
    for unit in journal.get("units") or []:
        if not isinstance(unit, dict):
            continue
        kind = unit.get("kind")
        if kind in _REUSABLE_KINDS:
            reusable.append(unit)
        elif kind in _MISSING_KINDS:
            missing.append(unit)
    return reusable, missing


def _shed_criteria(payload: dict[str, Any]) -> list[str]:
    """The criterion ids shed under the review-time budget cap, read from the recorded
    findings (a shed criterion emits no journal unit)."""
    shed: list[str] = []
    for finding in payload.get("findings") or []:
        if isinstance(finding, dict) and finding.get("reason") == _SHED_REASON:
            shed.extend(finding.get("criteria") or [])
    return shed


def _reusable_checkpoints_present(ctx, reusable: list[dict]) -> bool:
    """True when every reusable unit's recorded checkpoint still loads (a resume join,
    exactly the one Pass-1 performs). A missing/corrupt/legacy-namespace checkpoint means
    the stored success can no longer be reused → the latest review is stale."""
    for unit in reusable:
        digest = (unit.get("lineage") or {}).get("digest")
        if not digest or checkpoints.load_checkpoint(ctx, digest) is None:
            return False
    return True


def _is_stale(ticket_id: str, payload: dict[str, Any], ctx, reusable, *, repo_root) -> bool:
    """True when the latest review no longer reflects the present plan/code/registry or
    its reusable successes can no longer be loaded.

    Fails CLOSED on an UNCOMPUTABLE current stamp: ``current_material_fingerprint`` and
    ``review_code_sha`` each return ``None`` on a read error, and a payload stamp can also
    be ``None`` (a review emitted with no material), so a bare ``!=`` would read
    ``None == None`` as a MATCH and let the retry resume against material it cannot
    confirm. An unknown current stamp is therefore treated as stale (refuse), never as a
    match — the gate's documented fail-closed posture."""
    current_material = attest.current_material_fingerprint(ticket_id, repo_root=repo_root)
    if current_material is None or payload.get("material_fingerprint") != current_material:
        return True
    current_sha = sidecar.review_code_sha(repo_root)
    if current_sha is None or payload.get("verified_at_sha") != current_sha:
        return True
    try:
        from .manifest import registry_version

        if payload.get("regver") != registry_version(repo_root):
            return True
    except Exception:  # noqa: BLE001 — cannot read the registry version → treat as stale (refuse)
        return True
    return not _reusable_checkpoints_present(ctx, reusable)


def check_eligibility(ticket_id: str, ctx, *, repo_root) -> tuple[dict[str, Any] | None, str]:
    """Decide whether ``--retry`` may resume the latest review of ``ticket_id``.

    Returns ``(payload, "")`` when eligible (``payload`` is the latest sidecar payload
    being resumed) or ``(None, reason)`` when it must be refused — where ``reason`` is a
    short machine-readable code (``no-prior-review`` / ``not-indeterminate`` /
    ``no-journal`` / ``no-retryable-missing`` / ``stale``). Read-only: no model call, no
    store write."""
    try:
        payload = sidecar.latest_review_result(ticket_id, repo_root=repo_root)
    except Exception:  # noqa: BLE001 — an unreadable sidecar degrades to "nothing to resume"
        return None, "no-prior-review"
    if not payload:
        return None, "no-prior-review"
    if payload.get("verdict") != "INDETERMINATE":
        return None, "not-indeterminate"
    journal = payload.get("discovery_journal")
    if not isinstance(journal, dict) or not _valid_journal(journal):
        return None, "no-journal"
    reusable, missing = _partition_units(journal)
    if not missing and not _shed_criteria(payload):
        return None, "no-retryable-missing"
    if _is_stale(ticket_id, payload, ctx, reusable, repo_root=repo_root):
        return None, "stale"
    return payload, ""


def refusal_verdict(ticket_id: str, ctx, *, reason: str, cfg) -> dict[str, Any]:
    """A shape-valid, unsigned ``INDETERMINATE`` verdict for an INELIGIBLE retry: zero
    model calls, no sidecar, ``coverage.retry_refused`` set with the machine-readable
    ``reason``, and the full-review remedy carried as the finding's remediation."""
    return claimability.indeterminate_verdict(
        ticket_id,
        ticket_type=getattr(ctx, "ticket_type", ""),
        finding={
            "id": "plan-review-retry-ineligible",
            "reason": f"retry-refused:{reason}",
        },
        coverage_extra={"retry_refused": True, "retry_refusal_reason": reason},
        signature_reason="retry-ineligible",
        remediation=REMEDY,
        cfg=cfg,
    )


def gate(
    ticket_id: str, ctx, *, repo_root, cfg
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The ``--retry`` entry check: returns ``(early_return, prior_payload)``.

    On an INELIGIBLE retry, ``early_return`` is a ready-to-return refusal verdict (zero
    model calls) and ``prior_payload`` is ``None``. On an eligible retry, ``early_return``
    is ``None`` and ``prior_payload`` is the latest sidecar payload being resumed (so the
    caller can extend its retry lineage). The caller returns ``early_return`` immediately
    when it is not ``None``."""
    prior, reason = check_eligibility(ticket_id, ctx, repo_root=repo_root)
    if prior is None:
        return refusal_verdict(ticket_id, ctx, reason=reason, cfg=cfg), None
    return None, prior


def _usage_from_verdict(verdict: dict[str, Any]) -> dict[str, int]:
    """The token/request usage this retry attempt spent, summed from the verdict's
    per-call coverage usage (absent keys read as zero)."""
    per_call = ((verdict.get("coverage") or {}).get("usage") or {}).get("per_call") or []
    totals = {"input_tokens": 0, "output_tokens": 0, "requests": 0}
    for call in per_call:
        if not isinstance(call, dict):
            continue
        totals["input_tokens"] += int(call.get("input_tokens") or 0)
        totals["output_tokens"] += int(call.get("output_tokens") or 0)
        totals["requests"] += 1
    return totals


def next_lineage(prior: Any, usage: dict[str, int]) -> dict[str, Any]:
    """Extend the cumulative retry lineage by one attempt WITHOUT legacy seeding.

    A ``prior`` that is not a current-version lineage record (``None`` — never retried —
    or a foreign/legacy shape) starts a FRESH record at attempt 1; it is never backfilled
    with a fabricated attempt-0. A current record is incremented and its usage accumulated."""
    if isinstance(prior, dict) and prior.get("version") == RETRY_LINEAGE_VERSION:
        attempts = int(prior.get("attempts") or 0)
        base = prior.get("cumulative_usage") or {}
    else:
        attempts = 0
        base = {}
    return {
        "version": RETRY_LINEAGE_VERSION,
        "attempts": attempts + 1,
        "cumulative_usage": {
            key: int(base.get(key) or 0) + int(usage.get(key) or 0)
            for key in ("input_tokens", "output_tokens", "requests")
        },
    }


def build_lineage(prior_payload: dict[str, Any], verdict: dict[str, Any]) -> dict[str, Any]:
    """The retry-lineage record to stamp on this retry's sidecar: the prior payload's
    lineage extended by this attempt's usage (fresh start when the prior was never
    retried)."""
    return next_lineage(prior_payload.get("retry_lineage"), _usage_from_verdict(verdict))
