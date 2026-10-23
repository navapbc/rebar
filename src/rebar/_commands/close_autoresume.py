"""Bounded whole-verifier retries for insufficiency-only completion failures.
The verifier's signed evidence markers decide eligibility. Cached criterion
PASSes seed the next run. Stop after the configured maximum or when credited
PASS count stops increasing. CLI, MCP, and library closes share this seam."""

from __future__ import annotations

import logging
from time import monotonic_ns
from typing import Any

logger = logging.getLogger(__name__)

#: The per-attempt trail key attached to the returned verdict (and carried onto the durable
#: COMPLETION_VERDICT sidecar record by ``completion_sidecar.build_payload``).
TRAIL_KEY = "auto_resume_trail"


def _max_resumes(cfg_root: str | None) -> int:
    """The configured resumption bound, ``verify.auto_resume_max`` (default 2; 0 disables)."""
    from rebar.config import compose_config

    return int(compose_config(cfg_root).verify.auto_resume_max)


def _cache_credited_count(result: dict[str, Any]) -> int:
    """How many criteria this attempt credited from the cross-run verdict cache — records
    the assembly seam stamped ``seeded: true`` (a prior validated PASS) and ``met: true``.
    This is the loop's progress measure: a resumption that banked NEW passes re-seeds them,
    so the next attempt's credited count strictly grows while progress is being made."""
    return sum(
        1
        for r in result.get("criteria") or []
        if isinstance(r, dict) and r.get("met") is True and r.get("seeded") is True
    )


def _unmet_count(result: dict[str, Any]) -> int:
    """How many per-criterion records this attempt left unmet (``met: false``)."""
    return sum(
        1 for r in result.get("criteria") or [] if isinstance(r, dict) and r.get("met") is False
    )


def attempts_note(result: dict[str, Any]) -> str:
    """The refusal-message suffix describing the resumption trail, or ``""`` for a close
    that never resumed — so a single-attempt FAIL keeps its exact prior message shape."""
    trail = result.get(TRAIL_KEY) or []
    if len(trail) <= 1:
        return ""
    steps = "; ".join(
        f"attempt {t.get('attempt')}: {t.get('cache_credited')} cache-credited PASS(es), "
        f"{t.get('remaining_unmet')} unmet"
        for t in trail
    )
    return (
        f"\n\n  Auto-resume re-ran the verifier {len(trail) - 1} time(s) (bounded by "
        f"verify.auto_resume_max and by strict cache-credit progress) before this failure "
        f"surfaced: {steps}."
    )


def _manifest_value(manifest: list, field: str) -> str | None:
    """Return a value from an authenticated ``field: value`` manifest step."""
    prefix = field + ":"
    for step in manifest:
        text = str(step)
        if text.startswith(prefix):
            return text.split(":", 1)[1].strip() or None
    return None


def _reusable_attested_pass(ticket_id: str, *, ref: str | None, repo_root) -> dict[str, Any] | None:
    """Return a close-compatible PASS for a still-current same-ref completion op-cert.

    This is deliberately stricter than completion validity-on-read: a close may reuse only a
    cryptographically certified op-cert carrying an explicit authenticated SHA and material
    fingerprint, both of which still exactly match the close input.  Any absent, legacy,
    malformed, stale, or unreadable input returns ``None`` so the normal verifier runs.
    """
    try:
        from rebar.llm.completion import _pinned_ticket_view_selection

        # A previously recorded op-cert changed ticket state after its verifier receipt was
        # captured, so it cannot drive the all-or-none close transaction. The experimental
        # mode deliberately buys a fresh stable read instead of falling back to the legacy
        # two/three-commit reuse tail.
        if _pinned_ticket_view_selection(repo_root)[0]:
            return None
        from rebar import _reads, signing
        from rebar._snapshot.repo_snapshot import resolve_ref
        from rebar.llm.plan_review import attest

        state = _reads.show_ticket(ticket_id, repo_root=repo_root)
        if state.get("status") != "in_progress":
            return None

        certified = signing.verify_signature(
            ticket_id, kind="completion-verifier", repo_root=repo_root
        )
        if certified.get("verified") is not True or certified.get("opcert") is not True:
            return None

        # Legacy op-certs predate manifest binding; their plaintext mirror is not enough to
        # prove a PASS assertion.  Reuse requires the manifest extracted from the signed DSSE
        # predicate itself, never ``_authoritative_manifest``'s compatibility fallback.
        if not isinstance(certified.get("signed_manifest"), list):
            return None
        manifest = attest._authoritative_manifest(certified)
        if not manifest or str(manifest[0]).strip() != "completion-verifier: PASS":
            return None

        # Require the explicit pin from the authenticated manifest.  The op-cert's signed
        # merged-log commit must agree too; accepting its implicit HEAD fallback would let an
        # old/missing verified-at-sha manifest qualify accidentally.
        signed_sha = signing.verified_at_sha_from_manifest(manifest)
        authenticated_head = attest._authoritative_head(certified)
        if not signed_sha or signed_sha != authenticated_head:
            return None
        root = str(repo_root) if repo_root is not None else None
        target_sha = resolve_ref(ref or "HEAD", root, fetch=False)
        if target_sha != signed_sha:
            return None

        signed_material = attest._authoritative_material(certified)
        if not signed_material:
            return None
        current_material = attest.current_material_fingerprint(ticket_id, repo_root=repo_root)
        if not current_material or current_material != signed_material:
            return None

        last_reopened = state.get("last_reopened_at")
        signed_at = certified.get("signed_at")
        if last_reopened is not None and (signed_at is None or signed_at <= last_reopened):
            return None

        resolved_id = str(state.get("ticket_id") or ticket_id)
        result: dict[str, Any] = {
            "verdict": "PASS",
            "ticket_id": resolved_id,
            "criteria": [],
            "findings": [],
            "summary": "Reused a certified completion PASS for the unchanged close ref.",
            "target": {"kind": "ticket", "ticket_ids": [resolved_id]},
            "reviewers": ["completion-verifier"],
            "runner": "reused",
            "model": _manifest_value(manifest, "model"),
            "source": "attested",
            "certifiable": True,
            "verified_at_sha": target_sha,
            "coverage": {"llm_ran": False, "attestation_reused": True},
        }
        prior_runner = _manifest_value(manifest, "runner")
        if prior_runner:
            result["reuse_provenance"] = {"runner": prior_runner, "model": result["model"]}
        logger.info(
            "completion verification reused for %s: certified PASS remains current at %s",
            ticket_id,
            target_sha,
        )
        return result
    except Exception:  # any unreadable reuse input must run the verifier
        logger.warning(
            "completion attestation reuse check failed for %s; running a full verification",
            ticket_id,
            exc_info=True,
        )
        return None


def verify_with_auto_resume(
    ticket_id: str,
    *,
    ref: str | None,
    repo_root,
    cfg_root: str | None,
    ticket_view: Any | None = None,
    ticket_read_mode: str | None = None,
) -> dict[str, Any]:
    """Run completion verification with bounded insufficiency retries.
    Keep the same pinned ref and arguments. Return a PASS or final failure.
    If retried, attach attempt counts, cached PASS counts, and unmet counts.
    A single-attempt verdict keeps its original shape. Exceptions propagate."""
    wrapper_started_ns = monotonic_ns()
    phase_metrics: dict[str, int] = {}
    from rebar import llm  # LAZY — preserves the core optionality contract
    from rebar.llm import completion_reconcile

    phase_metrics["verifier_wrapper_setup_ms"] = (monotonic_ns() - wrapper_started_ns) // 1_000_000
    phase_started_ns = monotonic_ns()
    reused = (
        None
        if ticket_view is not None
        else _reusable_attested_pass(ticket_id, ref=ref, repo_root=repo_root)
    )
    phase_metrics["verifier_reusable_lookup_ms"] = (monotonic_ns() - phase_started_ns) // 1_000_000
    if reused is not None:
        if ticket_read_mode is not None:
            reused["ticket_read_mode"] = ticket_read_mode
        return reused

    phase_started_ns = monotonic_ns()
    max_resumes = _max_resumes(cfg_root)
    trail: list[dict[str, Any]] = []
    prev_credited: int | None = None
    phase_metrics["verifier_resume_config_ms"] = (monotonic_ns() - phase_started_ns) // 1_000_000
    phase_metrics["verifier_attempts_ms"] = 0
    phase_metrics["verifier_between_attempts_ms"] = 0
    session_kwargs: dict[str, Any] = {}
    if ticket_view is not None:
        session_kwargs["ticket_view"] = ticket_view
    if ticket_read_mode is not None:
        session_kwargs["ticket_read_mode"] = ticket_read_mode
    while True:
        # Verify this ticket against its immutable local ref without fetching.
        # Descendants have separate close gates. The attested source permits a
        # signature. The standalone graph and local-source options remain available.
        phase_started_ns = monotonic_ns()
        result = llm.verify_completion(
            ticket_id,
            graph=False,
            source="attested",
            ref=(ref or "HEAD"),
            fetch=False,
            repo_root=repo_root,
            phase_metrics=phase_metrics,
            **session_kwargs,
        )
        phase_metrics["verifier_attempts_ms"] += (monotonic_ns() - phase_started_ns) // 1_000_000
        phase_started_ns = monotonic_ns()
        try:
            credited = _cache_credited_count(result)
            remaining = _unmet_count(result)
            attempt = len(trail) + 1
            trail.append(
                {"attempt": attempt, "cache_credited": credited, "remaining_unmet": remaining}
            )
            logger.info(
                "completion verification attempt %d for %s: verdict=%s, %d cache-credited "
                "PASS(es), %d unmet",
                attempt,
                ticket_id,
                result.get("verdict"),
                credited,
                remaining,
            )
            if str(result.get("verdict", "")).upper() == "PASS":
                break
            # Qualification: REUSE the framework-owned predicate — an unmet record WITHOUT the
            # evidence_sufficient=false marker is a genuine refutation, which no re-run can help.
            if not completion_reconcile._insufficiency_only(result):
                break
            if attempt > max_resumes:
                break  # the count bound: 1 initial attempt + max_resumes resumptions
            if prev_credited is not None and credited <= prev_credited:
                logger.info(
                    "completion auto-resume for %s stopped early: attempt %d made no "
                    "cache-credit progress (%d, was %d) — an identical re-run would spin",
                    ticket_id,
                    attempt,
                    credited,
                    prev_credited,
                )
                break
            prev_credited = credited
        finally:
            phase_metrics["verifier_between_attempts_ms"] += (
                monotonic_ns() - phase_started_ns
            ) // 1_000_000
    # Attach the trail only when a resumption actually ran: a single-attempt verdict keeps
    # its exact prior shape, so no existing consumer or pinned record changes.
    phase_started_ns = monotonic_ns()
    if len(trail) > 1:
        result[TRAIL_KEY] = trail
    phase_metrics["verifier_attempt_count"] = len(trail)
    phase_metrics["verifier_resume_count"] = max(0, len(trail) - 1)
    phase_metrics["verifier_wrapper_finalization_ms"] = (
        monotonic_ns() - phase_started_ns
    ) // 1_000_000
    phase_metrics["verifier_wrapper_total_ms"] = (monotonic_ns() - wrapper_started_ns) // 1_000_000
    result_metrics = result.get("metrics")
    if isinstance(result_metrics, dict):
        result_metrics.update(phase_metrics)
    return result
