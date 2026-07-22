"""Plan-review claim-gate checks + the completion-delivery predicate.

Split out of :mod:`attest` to keep that module under the 800-LOC cap. This is the
gate-facing read surface:

* :func:`claim_gate_check` — the fast, local, no-LLM/no-network check the ``claim`` path
  runs to decide whether a certified, still-current plan-review attestation exists.
* :func:`plan_review_status` — its read-only currency-query sibling (same verdict a
  ``claim`` would reach, plus the attestation's bound ``verified_at_sha`` / ``signed_at``).
* :func:`delivered_now` — the completion-awareness predicate the manifest assembler uses.

It depends one-directionally on :mod:`attest` for the validity core (``compute_validity``
and the manifest-anchor helpers), imported lazily inside the function bodies so module load
order never matters. :mod:`attest` re-exports these names at its foot, so historical
``attest.<name>`` imports and monkeypatch sites stay stable.
"""

from __future__ import annotations

import logging
from typing import Any

from .manifest import _MANIFEST_PREFIX
from .pin_health import PlanValidityProfile

logger = logging.getLogger(__name__)


def _attested_delivered(ticket: dict[str, Any], *, repo_root=None) -> bool:
    """Require closed status plus a completion attestation valid on this ticket's state."""
    import rebar

    from .attest import compute_validity

    if ticket.get("status") != "closed":
        return False
    tid = ticket.get("ticket_id")
    if not tid:
        return False
    try:
        sig = rebar.verify_signature(tid, kind="completion-verifier", repo_root=repo_root)
        if sig.get("verdict") != "certified":
            return False
        return bool(
            compute_validity(sig, ticket, "completion-verifier", repo_root=repo_root).get("valid")
        )
    except Exception:  # noqa: BLE001 — never let a signature read crash the predicate; fail closed
        logger.warning("delivered_now: attestation read failed for %s", tid, exc_info=True)
        return False


def _supersedes_child(candidate: dict[str, Any], child_id: str) -> bool:
    """True when ``candidate`` carries a ``candidate -supersedes-> child`` link. A ``supersedes``
    link is stored on the SOURCE ticket's ``deps`` as ``{"relation": "supersedes",
    "target_id": <child>}`` (``add_dependency`` writes to the source dir; ``supersedes`` is never
    hierarchy-promoted), so "A supersedes child" is A's dep whose ``target_id`` is the child."""
    for dep in candidate.get("deps") or []:
        if (
            isinstance(dep, dict)
            and dep.get("relation") == "supersedes"
            and dep.get("target_id") == child_id
        ):
            return True
    return False


def delivered_now(child: dict[str, Any], siblings: list[dict[str, Any]], *, repo_root=None) -> bool:
    """Return verified delivery, directly or through a live in-container superseder.

    Bare closed status never suffices; completion attestations are checked on read. The
    superseder branch is deliberately non-recursive and only considers supplied siblings.
    """
    if _attested_delivered(child, repo_root=repo_root):
        return True

    child_id = child.get("ticket_id")
    if not child_id:
        return False
    child_parent = child.get("parent_id")
    for a in siblings or []:
        if not isinstance(a, dict):
            continue
        a_id = a.get("ticket_id")
        if a_id is None or a_id == child_id:
            continue
        if a.get("parent_id") != child_parent:  # not an in-epic sibling
            continue
        if not _supersedes_child(a, child_id):
            continue
        # A is a LIVE in-epic vehicle: actively open/in_progress, OR closed-and-attested
        # (branch (A) on A — NON-recursive: A's own supersede chain is never followed).
        if a.get("status") in ("open", "in_progress"):
            return True
        if _attested_delivered(a, repo_root=repo_root):
            return True
    return False


def claim_gate_check(ticket_id: str, *, repo_root=None) -> dict[str, Any]:
    """The fast, local claim-path check for the PLAN-REVIEW gate. Returns
    ``{ok: bool, reason: str, verdict: str}``.

    ``ok`` is True only when a CERTIFIED plan-review attestation exists (verified strictly
    from the kind-keyed map) AND :func:`compute_validity` passes — its reviewed code has not
    drifted, it binds the current material fingerprint, and it post-dates any reopen. NO LLM
    and NO network — a pure local HMAC verify + a light fingerprint recompute + hashing a
    handful of dependency files."""
    from rebar import _reads, signing

    from .attest import compute_validity

    try:
        result = signing.verify_signature(ticket_id, kind=_MANIFEST_PREFIX, repo_root=repo_root)
    except Exception as exc:  # noqa: BLE001 — signing subsystem unavailable → fail-closed at the gate; broad-but-logged
        # Fail closed (the gate denies the claim) but log: a broken signing subsystem
        # is an operator-actionable failure, not a routine denial.
        logger.warning("signing unavailable; failing the claim gate closed", exc_info=True)
        return {"ok": False, "reason": f"signing-unavailable: {exc}", "verdict": "error"}

    if not result.get("verified"):
        return {
            "ok": False,
            "reason": f"no certified plan-review attestation (signature: {result.get('verdict')})",
            "verdict": result.get("verdict", "unsigned"),
        }
    # We requested kind="plan-review" strictly, so a certified result IS a plan-review
    # attestation (no separate wrong-manifest check needed). Layer freshness/lifecycle.
    try:
        state = _reads.show_ticket(ticket_id, repo_root=repo_root)
    except Exception:  # noqa: BLE001 — unreadable state → fail closed below via compute_validity's material/None paths
        state = {}
    validity = compute_validity(
        result,
        state,
        _MANIFEST_PREFIX,
        repo_root=repo_root,
        profile=PlanValidityProfile.DEFAULT,
    )
    if not validity["valid"]:
        return {
            "ok": False,
            "reason": validity["reason"],
            "verdict": validity.get("verdict", "stale"),
        }
    return {"ok": True, "reason": "certified plan-review attestation", "verdict": "certified"}


def plan_review_status(ticket_id: str, *, repo_root=None) -> dict[str, Any]:
    """Read-only currency query: is ``ticket_id``'s plan-review attestation valid RIGHT NOW?

    Wraps :func:`claim_gate_check` — the EXACT local check the ``claim`` gate runs, so the answer
    is precisely what a ``claim`` would decide — and enriches it with the attestation's bound
    ``verified_at_sha`` and ``signed_at`` so a caller can see WHAT the plan was reviewed against
    (the moving-base-ref question that motivates this seam). NO LLM and NO network: the same fast
    local reads (HMAC verify + light fingerprint recompute + a few dependency-file hashes) the
    claim gate does — never a billable review. It is the cheap answer to "should I re-gate before
    I implement?" that avoids re-running the full review just to learn the verdict.

    Returns ``{ok, verdict, reason, verified_at_sha, signed_at}`` where ``verdict`` is the
    :func:`compute_validity` classifier — ``certified`` when current, else one of ``stale-code`` /
    ``stale-head`` / ``stale-material`` / ``stale-regver`` / ``stale-reopened`` / ``unsigned`` /
    ``wrong-kind`` / ``malformed-pin`` / ``unverifiable-material`` / ``error``. ``verified_at_sha``
    is the code anchor the plan was reviewed against — the pinned verified-at-sha for a
    scoped/attested review, else the signed HEAD for an unscoped/local one — and ``signed_at`` the
    sign timestamp; both are ``None`` when no readable certified attestation exists.
    """
    from rebar import signing

    from .attest import _authoritative_head, _authoritative_manifest

    gate = claim_gate_check(ticket_id, repo_root=repo_root)
    status: dict[str, Any] = {
        "ok": bool(gate.get("ok")),
        "verdict": gate.get("verdict", "unsigned"),
        "reason": gate.get("reason", ""),
        "verified_at_sha": None,
        "signed_at": None,
    }
    try:
        sig = signing.verify_signature(ticket_id, kind=_MANIFEST_PREFIX, repo_root=repo_root)
        if sig.get("verified"):
            # The code anchor the plan was reviewed against: the pinned verified-at-sha when the
            # review was scoped/attested (--source attested), else the signed HEAD for an
            # unscoped/local review (which has no pinned step but binds the head it saw).
            status["verified_at_sha"] = signing.verified_at_sha_from_manifest(
                _authoritative_manifest(sig)
            ) or _authoritative_head(sig)
            status["signed_at"] = sig.get("signed_at")
    except Exception:  # noqa: BLE001 — enrichment only; the gate verdict already stands
        logger.warning("plan_review_status: could not read bound sha for %s", ticket_id)
    return status
