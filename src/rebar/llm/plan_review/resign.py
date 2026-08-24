"""The cheap re-sign path for a plan-review attestation (ticket middle-actinium-thrush).

A ``rebar review-plan`` that computes a signable PASS but whose SIGN step fails (recorded
as ``signature.signed=False`` + ``error``) leaves the expensive verdict WITHOUT the durable
product the claim gate consumes — the asymmetric op-cert (plan-review) attestation. Re-running
the full multi-pass LLM
review to recover it is ~10 minutes of billable work for a result already computed and
persisted in the ``REVIEW_RESULT`` sidecar.

:func:`resign_plan_review` is the recovery: it reads the LATEST persisted ``REVIEW_RESULT``
sidecar (NO LLM, NO network), verifies the recorded verdict is a signable PASS AND that the
plan/material has not changed since the review (the sidecar's recorded material fingerprint
still equals the freshly-recomputed one), reconstructs the minimal verdict, and calls
:func:`attest.sign_plan_review` to persist the SAME attestation a normal signing PASS would
have written — so a subsequent ``claim`` passes the gate.

STALENESS GUARD: the recorded fingerprint must equal ``current_material_fingerprint`` NOW.
If the plan drifted the old verdict is stale, so we REFUSE (and tell the user to run a full
``rebar review-plan``) rather than sign a verdict that no longer describes the plan. A
positively-detected DEPENDENCY-material drift (a pinned direct child/prerequisite's material
changed or was lost — pin_status ``stale-pin-drift``/``stale-pin-missing``) now invalidates
UNCONDITIONALLY, regardless of ``verify.enforce_plan_material_pins`` (bug 790c): sign-review
must not bypass the dependency invalidation ``sign_manifest`` already enforces at review time.
Only the metadata-quality pin states (``legacy-unpinned``/``malformed-pin``) stay governed by
the enforce flag.

Optionality: stdlib + core signing only (the sidecar reader, the attestation machinery, and
``current_material_fingerprint`` are all import-light) — it does NOT need the ``[agents]``
extra or a model key, because it never runs the LLM tiers.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any

from . import attest, sidecar

logger = logging.getLogger(__name__)


def _generation_child_states(initial_generation) -> tuple[list[dict[str, Any]] | None, dict | None]:
    """Return the exact direct-child states captured with a review generation."""
    try:
        snapshot = getattr(initial_generation, "relation_snapshot", None)
        if snapshot is None:
            return None, {"event": "plan_review_child_impact_snapshot_invalid", "reason": "missing"}
        states = getattr(snapshot, "ticket_states_by_id", None)
        if not isinstance(states, dict):
            return None, {"event": "plan_review_child_impact_snapshot_invalid", "reason": "states"}
        children: list[dict[str, Any]] = []
        for child_id in snapshot.child_ids:
            state = states.get(child_id)
            if not isinstance(state, dict):
                return None, {
                    "event": "plan_review_child_impact_snapshot_invalid",
                    "reason": "missing-child-state",
                    "child_id": str(child_id),
                }
            if state.get("ticket_id") != child_id:
                return None, {
                    "event": "plan_review_child_impact_snapshot_invalid",
                    "reason": "child-id-mismatch",
                    "child_id": str(child_id),
                }
            children.append(state)
        return children, None
    except (AttributeError, TypeError):
        return None, {"event": "plan_review_child_impact_snapshot_invalid", "reason": "malformed"}


def _material_delta(payload: dict[str, Any], ticket_id: str, repo_root) -> str:
    """Name the component that moved between the recorded review and the live ticket.

    The reviewed per-component fingerprints ride on the sidecar (``material_parts``), so
    this is an exact diff for any review recorded after bug 94a3. An older sidecar has only
    the composite, which cannot be decomposed — say so rather than recite a list."""
    from .material_diff import _current_components, describe_delta

    try:
        recorded = payload.get("material_parts")
        if isinstance(recorded, dict) and recorded:
            signed = {k: (v[0], int(v[1])) for k, v in recorded.items() if len(v) == 2}
            current = _current_components(ticket_id, repo_root)
            if signed and current is not None:
                return describe_delta(signed, current) or "no material component differs"
    except Exception:
        logger.warning("could not diff recorded material for %s", ticket_id, exc_info=True)
    return (
        "the changed component cannot be named: this review predates per-component fingerprinting"
    )


def _collect_baseline_generation(
    ticket_id: str, repo_root, recorded_verdict: str
) -> tuple[Any, dict[str, Any] | None]:
    """Collect resign's pre-signing generation baseline as ``(generation, refusal)``.

    Ignore UNTRACKED files in the SHARED tickets-tracker (same rule as the review
    preflight, bug d7cb-22ae): this is a READ that fingerprints the COMMITTED
    tracker head, and an untracked path cannot change that head — so it cannot
    change the answer. This machine runs many concurrent sessions against ONE
    tracker symlinked into each of them, where an untracked ``.tmp-event-*`` is the
    NORMAL transient state of another session's in-flight atomic write (write temp,
    then rename) — not crash debris. Under the strict check, signing raced those
    sessions and failed with store-read-failure at a rate that scaled with
    concurrency. The authoritative under-lock re-check already tolerates them (see
    generation.py's under-lock re-collect, ``collect(..., ignore_untracked=True)``);
    tracked dirty state (modified/staged/unmerged) still fails, as it must.

    A transient ``store-read-failure`` — the STAGED half of that race, a peer's
    in-flight ``git add``→``git commit`` index — is retried with the same bounded
    attempts/jittered backoff as ``generation.sign_manifest``'s loop (bug 90c1-c112,
    parity sibling of ec1e): resign's own signing step already inherits that retry,
    so aborting terminally here made the recovery fail the exact concurrency it
    exists to recover from. Deterministic snapshot reasons (missing/ambiguous/
    malformed/reducer/id-mismatch) and any other failure stay terminal — the
    recovery remains a structured no-throw API."""
    from . import generation
    from .relation_snapshot import PlanRelationSnapshotError

    refusal = {"ok": False, "signed": False, "ticket_id": ticket_id, "verdict": recorded_verdict}
    for attempt in range(1, generation.MAX_GENERATION_ATTEMPTS + 1):
        try:
            baseline = generation.collect(ticket_id, repo_root=repo_root, ignore_untracked=True)
        except PlanRelationSnapshotError as exc:
            if exc.reason == "store-read-failure":
                record = {
                    "event": "plan_review_resign_collect_retry",
                    "attempt": attempt,
                    "reason": exc.reason,
                }
                logger.warning("plan_review_resign_collect_retry: %s", record, extra=record)
                time.sleep(random.uniform(0, generation.STORE_READ_RETRY_BACKOFF_SECONDS * attempt))
                continue
            return None, {
                **refusal,
                "reason": f"plan review generation could not be collected: {exc}",
            }
        except Exception as exc:  # noqa: BLE001 - recovery remains a structured no-throw API
            return None, {
                **refusal,
                "reason": f"plan review generation could not be collected: {exc}",
            }
        return baseline, None
    return None, {
        **refusal,
        "reason": (
            "plan review generation remained unreadable after "
            f"{generation.MAX_GENERATION_ATTEMPTS} attempts (transient store-read-failure: "
            "a concurrent session's in-flight tracker write) — retry `rebar sign-review`"
        ),
    }


def resign_plan_review(ticket_id: str, *, repo_root=None) -> dict[str, Any]:
    """Cheaply (re)persist the plan-review attestation for an ALREADY-COMPUTED, still-valid
    PASS verdict — WITHOUT re-running the multi-pass LLM review.

    Returns a result dict ``{ok, signed, ticket_id, verdict, reason, signature?}``:

    * ``ok=True`` (``signed=True``) — the latest ``REVIEW_RESULT`` sidecar records a PASS whose
      material fingerprint still matches the current plan; the attestation was re-signed and the
      claim gate now passes.
    * ``ok=False`` (``signed=False``) — REFUSED, with a ``reason``: no sidecar at all, the latest
      sidecar is not a signable PASS (BLOCK / INDETERMINATE / degraded), the review ran with
      ``--source local`` (never certifiable — bug 5128-0856), or the plan changed since
      the review (stale — run a full ``rebar review-plan``). NEVER signs a non-PASS / degraded /
      local-source / stale verdict.

    NO LLM and NO network — a sidecar read, a light fingerprint recompute, and a local op-cert
    (asymmetric SSHSIG/Ed25519) sign inside an attested gate session resolved from the LOCAL
    object DB (``fetch=False``),
    so the recovered attestation binds a committed ``verified-at-sha`` like any other.
    """
    payload = sidecar.latest_review_result(ticket_id, repo_root=repo_root)
    if payload is None:
        return {
            "ok": False,
            "signed": False,
            "ticket_id": ticket_id,
            "verdict": None,
            "reason": (
                "no REVIEW_RESULT sidecar found for this ticket — run `rebar review-plan` "
                "to produce (and sign) a plan-review verdict"
            ),
        }

    recorded_verdict = str(payload.get("verdict") or "").upper()
    coverage = payload.get("coverage") or {}
    # Never-sign guard (mirrors attest.sign_plan_review): only a clean PASS with no
    # systemic-degrade resolution_class is a certifiable result. A non-PASS / degraded sidecar
    # is refused up-front with a clear message (sign_plan_review would raise on it anyway).
    if recorded_verdict != "PASS" or coverage.get("resolution_class"):
        return {
            "ok": False,
            "signed": False,
            "ticket_id": ticket_id,
            "verdict": recorded_verdict or None,
            "reason": (
                f"the latest review was not a signable PASS (verdict={recorded_verdict or 'n/a'}"
                + (
                    f", resolution_class={coverage.get('resolution_class')!r}"
                    if coverage.get("resolution_class")
                    else ""
                )
                + ") — run `rebar review-plan` to produce a fresh verdict"
            ),
        }

    # Local-source refusal (bug 5128-0856): a `--source local` review reads the in-place
    # checkout — uncommitted edits included — so its PASS is not a certifiable basis, and
    # re-signing it here would reopen exactly the hole the review-time never-sign guard
    # closed. Only an explicit "local" refuses: legacy payloads predate the field (None)
    # and are treated as the attested production default they almost certainly were.
    if payload.get("source") == "local":
        return {
            "ok": False,
            "signed": False,
            "ticket_id": ticket_id,
            "verdict": recorded_verdict or None,
            "reason": (
                "the latest review ran with --source local, which never signs (the recorded "
                "PASS was reached against the in-place checkout, not a committed snapshot) — "
                "run `rebar review-plan` with the attested source to review and sign"
            ),
        }

    initial_generation, collect_refusal = _collect_baseline_generation(
        ticket_id, repo_root, recorded_verdict
    )
    if collect_refusal is not None:
        return collect_refusal

    children, child_state_error = _generation_child_states(initial_generation)
    if child_state_error:
        return {
            "ok": False,
            "signed": False,
            "ticket_id": ticket_id,
            "verdict": "INDETERMINATE",
            "reason": (
                "the review generation lacks a complete direct-child state snapshot; "
                "run `rebar review-plan` to re-review and sign"
            ),
            "child_impact_state_error": child_state_error,
        }

    # STALENESS GUARD: the plan/material must not have changed since the review. Recompute the
    # current material fingerprint (NO LLM) and require it to equal the sidecar's recorded one.
    recorded_material = payload.get("material_fingerprint")
    current_material = initial_generation.own_material
    if recorded_material is None or current_material is None:
        return {
            "ok": False,
            "signed": False,
            "ticket_id": ticket_id,
            "verdict": recorded_verdict,
            "reason": (
                "could not compare the plan's material fingerprint against the recorded review "
                "(missing/unreadable) — run `rebar review-plan` to re-review and sign"
            ),
        }
    if recorded_material != current_material:
        return {
            "ok": False,
            "signed": False,
            "ticket_id": ticket_id,
            "verdict": recorded_verdict,
            "reason": (
                "the plan changed since the review — "
                f"{_material_delta(payload, ticket_id, repo_root)}"
                " — so the recorded PASS is stale; run `rebar review-plan` to re-review and sign"
            ),
        }

    try:
        phase_metadata = sidecar.parse_review_phase_metadata(payload)
        from .pin_health import review_phase_status

        phase_status = review_phase_status(
            initial_generation.phase,
            phase_metadata["phase"],
            phase_metadata["priority_floor"],
        )
    except sidecar.SidecarReviewPhaseError:
        phase_metadata = {"phase": "planning", "priority_floor": None}
        phase_status = "malformed"
    except Exception:  # noqa: BLE001 - unreadable current state cannot authorize recovery
        phase_metadata = {"phase": "planning", "priority_floor": None}
        phase_status = "malformed"
    if phase_status != "compatible":
        return {
            "ok": False,
            "signed": False,
            "ticket_id": ticket_id,
            "verdict": recorded_verdict,
            "reason": f"review phase metadata is {phase_status}; run `rebar review-plan`",
            "health": {"phase_status": phase_status},
        }

    enforced = attest._read_enforce_plan_material_pins(repo_root)
    try:
        reviewed_pins = sidecar.parse_reviewed_related_material(payload)
        pin_health = attest.derive_plan_material_pin_health(
            reviewed_pins, repo_root=repo_root, enforced=enforced
        )
    except sidecar.ReviewedRelatedMaterialError:
        pin_health = {"pin_status": "malformed-pin", "enforced": enforced, "targets": []}
    # Bug 790c: a positively-detected DEPENDENCY change/loss must invalidate the recorded PASS
    # UNCONDITIONALLY — independent of verify.enforce_plan_material_pins (which defaults False).
    # sign_manifest already aborts at review time when a direct child/prerequisite's material
    # changes (related_material is part of the immutable PlanReviewGeneration identity), so the
    # cheap sign-review recovery MUST NOT re-certify past that same drift; otherwise it silently
    # bypasses the invalidation the review-time path enforces. Only the metadata-quality states
    # (legacy-unpinned/malformed-pin) stay governed by the enforce flag, as before.
    dependency_changed = pin_health["pin_status"] in ("stale-pin-drift", "stale-pin-missing")
    if dependency_changed or (
        enforced and pin_health["pin_status"] not in ("current", "legacy-unpinned")
    ):
        if dependency_changed:
            changed_ids = ", ".join(
                str(target["canonical_id"])
                for target in pin_health["targets"]
                if target["pin_status"] in ("stale-pin-drift", "stale-pin-missing")
            )
            reason = (
                "a dependency's plan material changed since the review ("
                + changed_ids
                + "), so the recorded PASS no longer reflects it — run `rebar review-plan` "
                "to re-review and sign"
            )
        else:
            reason = (
                "reviewed related-ticket material is no longer valid "
                f"({pin_health['pin_status']}) — run `rebar review-plan` to re-review and sign"
            )
        return {
            "ok": False,
            "signed": False,
            "ticket_id": ticket_id,
            "verdict": pin_health["pin_status"],
            "reason": reason,
            "health": pin_health,
        }

    # Reconstruct the minimal verdict the attestation binds. The sidecar slims finding CITATIONS
    # out, so dependency scoping falls to the ticket's current file_impact (dependency_hashes reads
    # it from the store) hashed at the current code — the recovery attestation binds current code,
    # exactly what the claim gate re-checks. counts/model/runner ride from the sidecar.
    verdict: dict[str, Any] = {
        "verdict": "PASS",
        "ticket_id": payload.get("ticket_id") or ticket_id,
        "ticket_type": payload.get("ticket_type"),
        "model": payload.get("model"),
        "runner": payload.get("runner"),
        "coverage": coverage,
    }
    try:
        # Sign inside an ATTESTED gate session (bug 5128-0856): sign_plan_review's
        # no-null-pin invariant refuses to mint an attestation without a committed
        # verified_at_sha, and this recovery path used to sign outside any session — deps
        # hashed from the working tree, no pin. Resolving the configured gate ref here
        # (fetch=False: local object DB only, preserving the NO-network contract; drift
        # visibility is as fresh as the last fetch) binds the recovery attestation to the
        # same committed basis a fresh attested review would — which is exactly what the
        # claim gate re-checks against. Source is pinned to attested: minting a
        # claim-valid artifact is this function's whole job, so a configured local
        # back-out must not silently downgrade it (the sign would only be refused later).
        import contextlib

        from rebar.llm import gate_source

        try:
            handle = gate_source.resolve_gate_handle(
                None, gate_source.SOURCE_ATTESTED, repo_root, fetch=False
            )
            session: contextlib.AbstractContextManager = gate_source.gate_read_root(handle)
        except Exception:
            # no-null-pin invariant then refuses an unattested basis with a clear reason
            # (surfaced by the structured-refusal handler below), instead of resign dying
            # on ref resolution with a different, less actionable error.
            logger.warning("attested snapshot unavailable for sign-review", exc_info=True)
            session = contextlib.nullcontext()
        with session:
            sig = attest.sign_plan_review(
                verdict,
                material=current_material,
                review_phase=phase_metadata["phase"],
                priority_floor=phase_metadata["priority_floor"],
                initial_generation=initial_generation,
                children=children,
                repo_root=repo_root,
            )
    except Exception as exc:
        # Relation failures are an unsigned, retry-after-repair gate outcome, not
        # an opaque signing failure.  Keep the public no-throw recovery contract
        # while preserving the stable reason/reference fields used by CLI/MCP.
        from .relation_snapshot import PlanRelationSnapshotError

        if isinstance(exc, PlanRelationSnapshotError):
            record = {
                "event": "plan_relation_snapshot_error",
                "reason": exc.reason,
                "canonical_id": exc.canonical_id,
                "reference": exc.reference,
            }
            logger.error("plan relation snapshot failed: %s", record, extra=record)
            return {
                "ok": False,
                "signed": False,
                "ticket_id": ticket_id,
                "verdict": "INDETERMINATE",
                "reason": (
                    "repair or remove the unreadable plan relationship, then rerun "
                    "`rebar review-plan`; no attestation was signed"
                ),
                "plan_relation_snapshot_error": record,
            }
        logger.warning("cheap re-sign failed to persist the attestation", exc_info=True)
        return {
            "ok": False,
            "signed": False,
            "ticket_id": ticket_id,
            "verdict": recorded_verdict,
            "reason": f"the attestation could not be persisted: {exc}",
        }
    return {
        "ok": True,
        "signed": True,
        "ticket_id": ticket_id,
        "verdict": "PASS",
        "reason": "re-signed the plan-review attestation from the latest REVIEW_RESULT sidecar "
        "(no LLM review re-run)",
        "signature": {
            "signed": True,
            "key_id": sig.get("key_id"),
            "head_sha": sig.get("head_sha"),
        },
    }


# --------------------------------------------------------------------------------------
# The passed-but-unsigned classifier (ticket ammonic-amoral-nabarlek)
#
# The rule "a plan review that PASSED but whose attestation FAILED to persist is RETRYABLE,
# not success" used to live only in the CLI (`_cli/_llm_commands.py`), so the MCP surface —
# the one autonomous agents actually drive — returned `verdict: PASS` raw and the agent
# proceeded to `claim`, which the gate then refused because the signature it consumes was
# never written. The discrimination now lives HERE, below both front-ends: this module
# already owns `resign_plan_review`, the recovery the classifier names, and is import-light
# by contract (no `[agents]` extra), so a front-end can always reach it.
# --------------------------------------------------------------------------------------

#: Attestation persisted — ``signature.signed`` is true.
ATTESTATION_SIGNED = "signed"
#: No attestation was attempted (``--no-sign`` / not-signable / drift / readonly MCP): a
#: ``reason``, no ``error``, or no signature block at all. Not a failure — the review never
#: promised an attestation.
ATTESTATION_SKIPPED = "skipped"
#: The plan (own OR a pinned dependency's) changed before the verdict could be signed. The
#: recorded review is STALE; only a fresh full review recovers it.
ATTESTATION_PLAN_CHANGED = "plan_changed"
#: Signing ABORTED reading the plan's relationships. Re-signing re-collects the same
#: unreadable state (bug 94a3), so the recovery is to repair the relationship and re-review.
ATTESTATION_RELATION_UNREADABLE = "relation_unreadable"
#: A transient/recoverable signing failure (a lock, a retry event, any non-material error).
#: Nothing materially changed, so the cheap no-LLM re-sign applies.
ATTESTATION_SIGN_FAILED = "sign_failed"
#: The signature failed AND the recovery sidecar never persisted, so there is no recorded PASS
#: for the cheap no-LLM re-sign to read back. Only a fresh full review recovers it.
ATTESTATION_SIDECAR_LOST = "sidecar_lost"


class PlanReviewAttestation:
    """The classified attestation outcome of a plan-review result.

    ``retryable`` is the single bit both front-ends branch on: the CLI maps it to exit 11
    and prints :attr:`message` to stderr; the MCP tool attaches the whole thing to the
    returned payload so an agent can branch on ``cause``/``recovery_tool`` WITHOUT parsing
    English. ``recovery_tool`` names the rebar operation that recovers the state —
    ``sign_review`` for the cheap re-sign, ``review_plan`` for a stale or unreadable plan.
    """

    __slots__ = ("cause", "error", "message", "recovery_tool", "retryable", "signed")

    def __init__(
        self,
        *,
        signed: bool,
        retryable: bool,
        cause: str,
        error: str = "",
        recovery_tool: str | None = None,
        message: str = "",
    ) -> None:
        self.signed = signed
        self.retryable = retryable
        self.cause = cause
        self.error = error
        self.recovery_tool = recovery_tool
        self.message = message

    def as_dict(self) -> dict[str, Any]:
        """The structured, machine-branchable form attached to an MCP result."""
        return {
            "signed": self.signed,
            "retryable": self.retryable,
            "cause": self.cause,
            "error": self.error,
            "recovery_tool": self.recovery_tool,
            "message": self.message,
        }


def _is_relation_read_failure(signature: dict[str, Any]) -> bool:
    """Whether a ``plan_review_sign_aborted`` failure was the relation-snapshot READ giving up.

    That subset is the one ``sign-review`` cannot recover (it re-collects the same generation),
    so it needs different advice from the other aborts that share the base-class event. The
    reason vocabulary is ``PlanRelationSnapshotError.REASONS``, the single source."""
    if signature.get("event") != "plan_review_sign_aborted":
        return False
    try:
        from .relation_snapshot import PlanRelationSnapshotError

        reasons = PlanRelationSnapshotError.REASONS
    except Exception:  # noqa: BLE001 — the [agents] extra may be absent; fall back to generic advice
        return False
    return str(signature.get("error", "")).strip() in reasons


def classify_plan_review_attestation(result: dict[str, Any]) -> PlanReviewAttestation:
    """Classify whether a plan-review ``result`` actually LEFT an attestation behind.

    A signable PASS whose attestation was ATTEMPTED but FAILED to persist (``signed`` False
    WITH an ``error``, not a deliberate ``reason`` skip) is NOT a silent success: the review's
    sole durable product — the signature the claim gate consumes — was lost to a recoverable
    condition, so a later ``claim`` still fails the gate. Such a result is ``retryable``, and
    ``cause`` says which recovery applies. A deliberately-unsigned PASS and a
    successfully-signed PASS are both non-retryable.

    The cheap no-LLM ``sign-review`` recovery is named only when something durable actually
    survived: it re-signs from the recovery sidecar, so a result reporting
    ``sidecar_emitted`` explicitly False gets ``review_plan`` instead."""
    sig = result.get("signature") or {}
    if not (sig.get("signed") is False and sig.get("error")):
        persisted = sig.get("signed") is True
        return PlanReviewAttestation(
            signed=persisted,
            retryable=False,
            cause=ATTESTATION_SIGNED if persisted else ATTESTATION_SKIPPED,
        )
    error = str(sig.get("error"))
    tid = result.get("ticket_id") or "<id>"
    if sig.get("event") == "plan_review_generation_changed":
        # A real material change (own OR dependency — the error already names which): the
        # recorded review is STALE and cannot be cheaply re-signed. The only recovery is a
        # fresh full review; sign-review would (correctly) refuse.
        return PlanReviewAttestation(
            signed=False,
            retryable=True,
            cause=ATTESTATION_PLAN_CHANGED,
            error=error,
            recovery_tool="review_plan",
            message=(
                "plan review PASSED but the plan changed before it could be signed: "
                f"{error}\n"
                "the recorded review is stale — run `rebar review-plan` to re-review "
                "and sign.\n"
            ),
        )
    if _is_relation_read_failure(sig):
        # `plan_review_sign_aborted` is the BASE-class event and also covers arbitrary
        # terminal signing errors, for which `sign-review` IS the right recovery. Only the
        # relation-snapshot READ failures are hopeless that way: `sign-review` re-collects the
        # same generation and hits the same unreadable state, so the generic advice sent the
        # reader in a circle (bug 94a3). Discriminate on the reason, not the event.
        return PlanReviewAttestation(
            signed=False,
            retryable=True,
            cause=ATTESTATION_RELATION_UNREADABLE,
            error=error,
            recovery_tool="review_plan",
            message=(
                "plan review PASSED but signing was ABORTED reading the plan's "
                f"relationships: {error}\n"
                "`rebar sign-review` would re-collect the same unreadable state. Repair or "
                "remove the plan relationship the reason names, then run "
                f"`rebar review-plan {tid}` again.\n"
            ),
        )
    if result.get("sidecar_emitted") is False:
        # The signature AND the recovery sidecar were both lost — one contention episode can
        # take the pair, and the sidecar is written only AFTER the sign attempt. Keyed on an
        # explicit False, never on absence: a result that does not carry the field says
        # nothing about the sidecar, and guessing there would misroute a recoverable failure.
        # `sign-review`
        # reads the sidecar back, so with none written it cannot re-sign this PASS: it finds
        # the PREVIOUS round's record and refuses (bug inborn-asbestine-moray). Advertising it
        # here sent the reader into that dead end at the cost of a whole review. Worse, that
        # stale record is a CONTRADICTING verdict, so it must not be read as current either.
        return PlanReviewAttestation(
            signed=False,
            retryable=True,
            cause=ATTESTATION_SIDECAR_LOST,
            error=error,
            recovery_tool="review_plan",
            message=(
                "plan review PASSED but NEITHER the attestation NOR the recovery record "
                f"persisted: {error}\n"
                "nothing durable survives from this review, so there is nothing left to "
                f"re-sign — run `rebar review-plan {tid}` again.\n"
                "any plan-review verdict still recorded for this ticket predates this review "
                "and is NOT current.\n"
            ),
        )
    # A TRANSIENT/retryable failure (retry event, a lock, or any non-material error) that left
    # the recovery sidecar behind: nothing materially changed, so the cheap no-LLM recovery
    # applies — re-persist the already-computed verdict with `sign-review`.
    return PlanReviewAttestation(
        signed=False,
        retryable=True,
        cause=ATTESTATION_SIGN_FAILED,
        error=error,
        recovery_tool="sign_review",
        message=(
            "plan review PASSED but the attestation could not be persisted: "
            f"{error}\n"
            f"run `rebar sign-review {tid}` to re-sign from the recorded review "
            "(no LLM re-run) — the claim gate needs the signature.\n"
        ),
    )
