"""Share configuration and attestation policy for claim and close gates.

Each opt-in gate resolves one ``verify.*`` flag. A readable flag yields enabled or disabled.
Unreadable configuration raises :class:`rebar.config.ConfigError` and blocks the operation
instead of becoming a policy result.

:func:`plan_review_precheck` is the common start-work check for atomic claim and every
transition into ``in_progress``. Completion-verifier execution remains in the transition
command.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from enum import Enum
from typing import Any, cast

from rebar.types import PLAN_REVIEW_EXEMPT_TYPES, PLAN_REVIEW_REVIEWED_TYPES

__all__ = [
    "GateState",
    "close_plan_review_gate_check",
    "description_cap_warning",
    "gate_enabled",
    "gate_ran",
    "log_advisory_warning",
    "log_description_cap_warning",
    "plan_review_precheck",
]

#: Plan-review exemptions re-exported from :mod:`rebar.types`, which owns the declaration
#: and derives its complement from ``TicketType``.
_PLAN_REVIEW_EXEMPT_TYPES = PLAN_REVIEW_EXEMPT_TYPES

logger = logging.getLogger(__name__)


class GateState(Enum):
    """Represent an opt-in gate flag read from valid configuration.

    ``ENABLED`` and ``DISABLED`` are policy outcomes. Unreadable configuration raises before a
    state is produced. Only ``ENABLED`` is truthy, preserving boolean call sites and test fakes.
    """

    ENABLED = "enabled"
    DISABLED = "disabled"

    def __bool__(self) -> bool:
        """Truthy only for :attr:`ENABLED`."""
        return self is GateState.ENABLED


def _claim_gate_reason(check: Mapping[str, object]) -> str:
    """Render the shared health payload into a stable claim-gate diagnosis."""
    reason = str(check.get("reason", "plan-review validity was unavailable"))
    verdict = str(check.get("verdict", "stale"))
    health = check.get("health")
    if verdict not in ("stale-pin-drift", "stale-pin-missing") or not isinstance(health, Mapping):
        return reason
    targets = health.get("targets")
    if not isinstance(targets, list):
        return reason
    stale_ids = [
        str(target.get("canonical_id"))
        for target in targets
        if isinstance(target, Mapping) and target.get("pin_status") == verdict
    ]
    if not stale_ids:
        return reason
    return f"{reason} ({verdict}; targets: {', '.join(stale_ids)})"


def gate_enabled(cfg_root: str | None, attr: str, *, ticket_id: str, gate_label: str) -> GateState:
    """Resolve ``verify.<attr>`` to an enabled or disabled gate state.

    ``attr`` names a :class:`VerifyConfig` field. Read or parse failure raises a chained
    :class:`~rebar.config.ConfigError` that identifies ``gate_label`` and ``ticket_id``. The
    gated operation therefore fails without treating a configuration fault as a default.
    """
    from rebar.config import ConfigError, compose_config

    try:
        if getattr(compose_config(cfg_root).verify, attr):
            return GateState.ENABLED
        return GateState.DISABLED
    except ConfigError as exc:
        raise ConfigError(
            f"cannot resolve {gate_label} for {ticket_id}: the rebar config could not "
            f"be read ({exc}). An unreadable config is an error (operator ruling "
            "39f8-ae7c) — fix the config file, then retry the operation."
        ) from exc


def gate_ran(check: Mapping[str, object]) -> bool:
    """Whether a gate-check payload came from a gate that ACTUALLY RAN.

    The single reader of the ``gate_ran`` stamp that
    :func:`close_plan_review_gate_check` writes on every payload it returns. Callers ask
    this instead of comparing ``verdict`` against a skip string: the verdict vocabulary
    grows (``disabled`` is a skip today), and a comparison that
    silently misclassifies a new skip verdict as "ran" is the fragility this predicate
    removes.

    An UNSTAMPED payload (the key absent) answers ``False`` — fail-safe: absent evidence
    that the gate ran must never authorise extra work.
    """
    return check.get("gate_ran") is True


def _disposition_close_exempt(
    ticket_id: str,
    close_class: str,
    tracker: str | None,
    repo_root,
    *,
    close_reason: str = "",
) -> bool:
    """Return whether an administrative close has disposition evidence.

    The completion-disposition predicate owns replacement-backed and reason-backed checks.
    Unreadable state or missing evidence returns ``False``.
    """
    from rebar._commands import close_disposition

    if close_class not in close_disposition.ADMINISTRATIVE_CLASSES:
        return False

    try:
        if tracker is None:
            from rebar import config

            tracker = str(config.tracker_dir(repo_root))
        return bool(
            close_disposition.verdict(
                ticket_id,
                close_class,
                tracker,
                close_reason=close_reason,
            )
        )
    except Exception:  # noqa: BLE001 -- an unreadable source must stay fail-CLOSED
        return False


def close_plan_review_gate_check(
    ticket_id: str,
    ticket_state: Mapping[str, Any],
    *,
    repo_root=None,
    close_class: str = "",
    close_reason: str = "",
    tracker: str | None = None,
) -> dict[str, object]:
    """Validate the opt-in plan-review close requirement using local state only.

    The check reads an existing attestation and performs no review, LLM call, or network work.
    ``CLOSE`` permits implementation changes while enforcing plan and policy freshness. An
    evidence-backed administrative disposition returns the distinct ``disposition`` verdict.
    """
    state = gate_enabled(
        str(repo_root),
        "require_plan_review_for_close",
        ticket_id=ticket_id,
        gate_label="the plan-review close gate",
    )
    if not state:
        # Disabled is a readable policy result. Unreadable configuration raises earlier.
        # Consumers use the ``gate_ran`` stamp instead of inferring execution from verdict text.
        return {
            "ok": True,
            "gate_ran": False,
            "verdict": "disabled",
            "reason": "plan-review close gate is disabled",
        }
    if ticket_state.get("ticket_type") not in PLAN_REVIEW_REVIEWED_TYPES:
        return {
            "ok": True,
            "gate_ran": True,
            "verdict": "exempt",
            "reason": "ticket type is exempt",
        }
    if _disposition_close_exempt(
        ticket_id, close_class, tracker, repo_root, close_reason=close_reason
    ):
        # A distinct verdict separates an evidence-backed disposition from type exemptions
        # and unsigned force bypasses in the audit trail.
        return {
            "ok": True,
            "gate_ran": True,
            "verdict": "disposition",
            "reason": (
                f"closed as {close_class} with an administrative disposition attestation; "
                "the plan-review attestation certifies work to be done here, and this close "
                "does not claim completed work on this ticket"
            ),
        }

    from rebar._store import freshness

    # Require a current store before reading attestation state to avoid a false ``unsigned``
    # result.
    store = freshness.store_freshness(tracker or freshness.resolve_tracker(repo_root))
    if not store["fresh"]:
        return {
            "ok": False,
            "gate_ran": True,
            "verdict": freshness.STALE_VERDICT,
            "reason": (
                f"{store['reason']} — refusing to decide the plan-review close gate "
                "against a ticket store that is not current"
            ),
        }

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
            "gate_ran": True,
            "verdict": str(validity.get("verdict", "unavailable")),
            "reason": str(validity.get("reason", "plan-review validity was unavailable")),
        }
        if isinstance(validity.get("health"), dict):
            result["health"] = validity["health"]
        return result
    except Exception:
        record = {"event": "plan_review_close_gate_unavailable", "ticket_id": ticket_id}
        logger.warning(
            "plan-review close gate unavailable: %s", record, extra=record, exc_info=True
        )
        return {
            "ok": False,
            "gate_ran": True,
            "verdict": "unavailable",
            "reason": "could not verify the plan-review attestation locally",
        }


def _plan_review_gate_applies(cfg_root: str | None, ticket_type: str, *, ticket_id: str) -> bool:
    """Return whether the claim gate is enabled and the ticket type is not exempt.

    This shared applicability check reads configuration and type only. Attestation currency is
    checked later by ``llm.claim_gate_check``.
    """
    if not gate_enabled(
        cfg_root,
        "require_plan_review_for_claim",
        ticket_id=ticket_id,
        gate_label="the plan-review start-work gate",
    ):
        return False
    return ticket_type not in _PLAN_REVIEW_EXEMPT_TYPES


def description_cap_warning(
    description: str | None, ticket_type: str, *, ticket_id: str, cfg_root: str
) -> str | None:
    """Return a save-time warning when a gated description exceeds its configured cap.

    The warning applies only when the start-work gate applies and uses the same cap as plan
    review and completion checks. It is advisory, runs after the write, and degrades to ``None``
    on lookup failure. ``cfg_root`` is the repository root used by claim configuration.
    """
    if not description:
        return None
    try:
        from rebar.config import compose_config

        limit = int(compose_config(cfg_root).verify.max_ticket_description_chars)
        chars = len(description)
        if chars <= limit:
            return None
        if not _plan_review_gate_applies(cfg_root, ticket_type, ticket_id=ticket_id):
            return None
    except Exception:  # an advisory notice must never fail a completed write
        logger.debug("could not evaluate the description cap for %s", ticket_id, exc_info=True)
        return None
    return (
        f"description for {ticket_id} is {chars:,} characters, above the "
        f"{limit:,}-character plan-review admission cap "
        f"(verify.max_ticket_description_chars). The plan-review start-work gate is "
        f"enabled for this project, so claiming {ticket_id} requires a passing review — "
        f"and review-plan will refuse admission until the description is shortened, "
        "usually by moving independent work into child tickets."
    )


def log_advisory_warning(warning: object) -> str | None:
    """Log a post-write advisory on the library channel and return it.

    The ``rebar`` logger has a ``NullHandler`` for unconfigured library callers. CLI and MCP
    entrypoints install handlers or return the warning through their own surfaces.
    """
    text = warning if isinstance(warning, str) and warning else None
    if text:
        logger.warning("%s", text)
    return text


def log_description_cap_warning(warning: object) -> str | None:
    """Log and return :func:`description_cap_warning` through the shared advisory channel."""
    return log_advisory_warning(warning)


def plan_review_precheck(
    ticket_id: str, cfg_root: str | None, repo_root, *, force_reason: str
) -> None:
    """Enforce the shared plan-review gate before claim or start-work transition.

    When enabled, non-exempt work requires a fresh certified attestation. Validation is local
    and performs no LLM or network call. Types in
    :data:`rebar.types.PLAN_REVIEW_EXEMPT_TYPES` are exempt. A non-empty ``force_reason``
    bypasses the gate and attempts a best-effort audit comment. Missing, stale, or invalid
    attestations raise :class:`CommandError`. Disabled, exempt, bypassed, and valid cases
    return ``None``.

    ``cfg_root`` identifies the repository configuration root and supports discovery when
    omitted. It must not be inferred from a relocated tracker directory.
    """
    from rebar import config
    from rebar._commands._seam import CommandError
    from rebar.reducer import reduce_ticket as _reduce

    # ``_plan_review_gate_applies`` shares configuration failure and exemption behavior with
    # the close gate and ``claim --review``.
    ticket_type = (_reduce(os.path.join(str(config.tracker_dir(repo_root)), ticket_id)) or {}).get(
        "ticket_type", ""
    )
    if not _plan_review_gate_applies(cfg_root, ticket_type, ticket_id=ticket_id):
        return None
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
        except Exception:
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
    # Op-cert signing requires ``ssh-keygen`` from OpenSSH 8.9 or newer. Include that remedy
    # when a review could not mint its attestation.
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
        'transition --force="<reason>".',
        returncode=1,
    )
