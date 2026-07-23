"""Shared posture for the opt-in close/claim verification gates.

The completion-verification *close* gate (``transition.py``) and the plan-review
*start-work* gate (``claim.py`` + ``transition.py``) each resolve a single ``verify.*``
config flag and, on an **unreadable** config, fail the gate **OPEN** (skip it) with a
stderr warning rather than blocking every operation on a broken config. That resolution
+ fail-open posture is identical between them and is exactly the kind of copy-pasted,
security-relevant logic that drifts between sessions — so it lives here, once.

The **plan-review start-work gate** itself (:func:`plan_review_precheck`) also lives
here, once: starting work on a ticket goes through it whether via ``claim`` (open ->
in_progress + assignee, atomically) or a plain ``transition open -> in_progress``, so
the two entry points cannot diverge in what they require before code is touched.

What the *completion* gate does when enabled (run the LLM completion verifier) is
gate-specific and stays in ``transition.py``.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Mapping
from typing import Any, cast

__all__ = ["close_plan_review_gate_check", "gate_enabled", "plan_review_precheck"]

logger = logging.getLogger(__name__)


def _claim_gate_reason(check: Mapping[str, object]) -> str:
    """Render the shared health payload into a stable claim-gate diagnosis."""
    reason = str(check.get("reason", "plan-review validity was unavailable"))
    verdict = str(check.get("verdict", "stale"))
    health = check.get("health")
    if verdict != "stale-pin-drift" or not isinstance(health, Mapping):
        return reason
    targets = health.get("targets")
    if not isinstance(targets, list):
        return reason
    stale_ids = [
        str(target.get("canonical_id"))
        for target in targets
        if isinstance(target, Mapping) and target.get("pin_status") == "stale-pin-drift"
    ]
    if not stale_ids:
        return reason
    return f"{reason} ({verdict}; targets: {', '.join(stale_ids)})"


def gate_enabled(
    cfg_root: str, attr: str, *, ticket_id: str, gate_label: str, extra: str = ""
) -> bool:
    """Resolve an opt-in ``verify.<attr>`` gate flag, failing OPEN on an unreadable config.

    ``attr`` is a ``VerifyConfig`` attribute name (e.g.
    ``"require_completion_verification_for_close"``). Returns ``True`` when the gate is
    enabled, ``False`` when it is off OR when the config can't be read — in the latter
    case a single-line warning is printed to stderr (``gate_label`` + optional ``extra``
    clause), so the skip is observable and never silent.

    Rationale for fail-OPEN: these are opt-in, default-off gates; an unreadable config
    must not auto-enable a (possibly billable) gate across every repo and ticket. The
    stronger signature gate fail-CLOSES independently, and a missing attestation is
    itself the "not validated" signal CI checks.
    """
    from rebar.config import ConfigError, load_config

    try:
        return bool(getattr(load_config(cfg_root).verify, attr))
    except ConfigError as exc:
        print(
            f"Warning: could not read rebar config ({exc}); {gate_label} is skipped "
            f"for {ticket_id}{extra}.",
            file=sys.stderr,
        )
        return False


def close_plan_review_gate_check(
    ticket_id: str, ticket_state: Mapping[str, Any], *, repo_root=None
) -> dict[str, object]:
    """Locally validate the opt-in plan-review close requirement.

    This deliberately verifies an already-created attestation only: it never starts a
    review, invokes an LLM, or contacts the network.  ``CLOSE`` keeps the plan and
    policy freshness checks while allowing implementation code to change during work.
    """
    if not gate_enabled(
        str(repo_root),
        "require_plan_review_for_close",
        ticket_id=ticket_id,
        gate_label="the plan-review close gate",
        extra=" (other close gates still apply)",
    ):
        return {"ok": True, "verdict": "disabled", "reason": "plan-review close gate is disabled"}
    if ticket_state.get("ticket_type") not in ("task", "story", "epic"):
        return {"ok": True, "verdict": "exempt", "reason": "ticket type is exempt"}

    try:
        from rebar import signing
        from rebar._engine_support import reads as ticket_reads
        from rebar.llm.plan_review import attest
        from rebar.llm.plan_review.pin_health import PlanValidityProfile

        with ticket_reads.local_read_context():
            verified = signing.verify_signature(ticket_id, kind="plan-review", repo_root=repo_root)
            validity = attest.compute_validity(
                verified,
                cast(dict[str, Any], ticket_state),
                "plan-review",
                repo_root=repo_root,
                profile=PlanValidityProfile.CLOSE,
            )
        result = {
            "ok": bool(validity.get("valid")),
            "verdict": str(validity.get("verdict", "unavailable")),
            "reason": str(validity.get("reason", "plan-review validity was unavailable")),
        }
        if isinstance(validity.get("health"), dict):
            result["health"] = validity["health"]
        return result
    except Exception:  # noqa: BLE001 -- local signature/plan reads must fail closed
        record = {"event": "plan_review_close_gate_unavailable", "ticket_id": ticket_id}
        logger.warning(
            "plan-review close gate unavailable: %s", record, extra=record, exc_info=True
        )
        return {
            "ok": False,
            "verdict": "unavailable",
            "reason": "could not verify the plan-review attestation locally",
        }


def plan_review_precheck(ticket_id: str, cfg_root: str, repo_root, *, force_reason: str) -> None:
    """The plan-review gate guarding the START of work on a ticket — the single
    method both ``claim`` and ``transition open -> in_progress`` call.

    When ``verify.require_plan_review_for_claim`` is on, starting a work ticket
    requires a fresh, certified plan-review attestation (earn one with
    ``rebar review-plan <id>``). This is a FAST, LOCAL HMAC verify + freshness/
    material binding — NO LLM and NO network call (the heavy review is out-of-band).
    Bugs and session_logs are EXEMPT. A non-empty ``force_reason`` bypasses with an
    audit comment (it is a reason STRING, not a bool — the bool ``--force`` flag is
    converted to a reason by the callers). Raises :class:`CommandError` (block) when
    the attestation is absent/stale/wrong. Returns ``None`` (allow) when the gate is
    off, the ticket is exempt, the bypass reason is set, or the attestation is valid.

    ``cfg_root`` is the REPO root (parent of the tracker), where ``.rebar/config.conf``
    lives. Consolidated here (out of ``claim.py``) so the claim and transition entry
    points enforce IDENTICAL requirements before code is touched.
    """
    from rebar import config
    from rebar._commands._seam import CommandError
    from rebar.reducer import reduce_ticket as _reduce

    # Shared resolution + fail-OPEN-on-unreadable-config posture (see gate_enabled),
    # mirroring the completion close gate so the two can't drift.
    if not gate_enabled(
        cfg_root,
        "require_plan_review_for_claim",
        ticket_id=ticket_id,
        gate_label="the plan-review start-work gate",
    ):
        return None
    ticket_type = (_reduce(os.path.join(str(config.tracker_dir(repo_root)), ticket_id)) or {}).get(
        "ticket_type", ""
    )
    if ticket_type in ("bug", "session_log", "code_review", "identity"):
        return None  # exempt from the plan-review gate
    if force_reason:
        # Audit the bypass (best-effort) so a forced start is a durable signal.
        try:
            from rebar._commands import leaf

            leaf.comment(
                ticket_id,
                "FORCE_CLAIM: plan-review gate bypassed by user approval — no plan-review "
                f'attestation was verified. Reason: "{force_reason}".',
                repo_root=repo_root,
            )
        except Exception:  # noqa: BLE001 — best-effort force-claim audit comment; broad-but-logged, the start proceeds
            logger.warning(
                "could not write FORCE_CLAIM audit comment on %s; continuing",
                ticket_id,
                exc_info=True,
            )
        return None
    from rebar import llm  # LAZY — preserves optionality (claim_gate_check is stdlib-only though)

    check = llm.claim_gate_check(ticket_id, repo_root=repo_root)
    if check.get("ok"):
        return None
    # DEGRADE remediation (story 8d8e): op-cert signing needs ssh-keygen (OpenSSH >= 8.9). When it
    # is unavailable the review could not MINT the attestation, so name the concrete fix in-band.
    ssh_hint = ""
    try:
        from rebar.attest import sshsig

        if sshsig.ssh_keygen_version() is None:
            ssh_hint = (
                "  Signing requires OpenSSH >= 8.9 (ssh-keygen) to mint the op-cert attestation —\n"
                "  install OpenSSH, then run `rebar review-plan` to earn it.\n"
            )
    except Exception:  # noqa: BLE001 — the ssh-keygen probe is advisory; never let it break the gate
        pass
    raise CommandError(
        f"Error: cannot start work on {ticket_id}: {_claim_gate_reason(check)}.\n"
        "  The plan-review gate is enabled (verify.require_plan_review_for_claim) — it\n"
        "  guards starting work via both `claim` and `transition open in_progress`.\n"
        f"{ssh_hint}"
        "  Recovery: run the plan review to earn an attestation, then start work:\n"
        f"    rebar review-plan {ticket_id}\n"
        f"    rebar claim {ticket_id}   (or: rebar transition {ticket_id} open in_progress)\n"
        '  Override (requires user approval): claim --force="<reason>", or '
        'transition --force --reason="<reason>".',
        returncode=1,
    )
