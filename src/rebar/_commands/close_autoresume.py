"""Bounded auto-resume for the completion-verification close gate (ticket b5f8-d3f9-39d9-4195).

When the close gate's verifier FAILs on pure evidence-search EXHAUSTION — every unmet
criterion carries the framework-set ``evidence_sufficient: false`` marker, so nothing was
positively refuted — the correct next action is mechanical: run the verifier again. The
cross-run verdict cache (ticket 8d74) banks per-criterion PASSes at finalize even on an
overall FAIL, so a re-run seeds the credited criteria and spends its whole fresh budget on
the formerly-insufficient remainder. That was demonstrated live (ticket 8d74's own close:
first attempt FAILed on exhaustion; an identical re-run closed clean with zero
operator-added evidence). Bounded-search exhaustion is the gate's problem to resolve, not
the operator's, so :func:`verify_with_auto_resume` automates the re-run instead of asking a
human to retype the same command.

The loop lives at the ONE close-gate verification seam shared by the CLI, MCP, and library
paths — ``close_precheck._completion_precheck``'s ``llm.verify_completion`` call site (all
three flow through ``transition_close.close_ticket``) — extracted here as a helper along
that call seam so ``_completion_precheck`` stays at its frozen complexity ceiling and
``close_precheck.py`` under the module-size cap. ``gate_dispatch`` /
``CompletionAgentStep`` are verdict PRODUCERS inside ``verify_completion`` and are not
touched; a resumption is a WHOLE-VERIFIER re-run, not a partial continuation.

Qualification REUSES the framework-owned
:func:`rebar.llm.completion_reconcile._insufficiency_only` predicate unchanged (the
per-criterion markers are framework-derived at the banking/assembly seams — a model cannot
mint them), so there is no parallel reimplementation, no new schema field, and no
model-facing vocabulary. The loop is bounded twice: by ``verify.auto_resume_max``
resumptions per close invocation (default 2; 0 disables), AND by strict progress — a
resumption dispatches only while the just-finished attempt strictly increased the
cache-credited PASS count over the attempt before it (the first FAIL's credited count is
the baseline). A zero-progress attempt means the next re-run would be an identical spin
(same seeded cache, same budget, same remainder), so the loop stops early and surfaces the
failure honestly even with resumptions remaining.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: The per-attempt trail key attached to the returned verdict (and carried onto the durable
#: COMPLETION_VERDICT sidecar record by ``completion_sidecar.build_payload``).
TRAIL_KEY = "auto_resume_trail"


def _max_resumes(cfg_root: str | None) -> int:
    """The configured resumption bound, ``verify.auto_resume_max`` (default 2; 0 disables)."""
    from rebar.config import load_config

    return int(load_config(cfg_root).verify.auto_resume_max)


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
    ticket_id: str, *, ref: str | None, repo_root, cfg_root: str | None
) -> dict[str, Any]:
    """Run ``llm.verify_completion`` with bounded auto-resume on insufficiency-only FAILs.

    Every attempt re-invokes the SAME existing entry point with the SAME arguments — in
    particular the same pinned ``ref`` (a ``--ref`` close resumes against that ref, and the
    default close against ``HEAD``). ``graph=False`` / ``source="attested"`` / ``fetch=False``
    carry the call-site rationale documented at the seam this was extracted from: the gate
    verifies THIS ticket's own criteria against an immutable, signable snapshot resolved from
    the local object DB. Verifier exceptions propagate unchanged to the caller's fail-closed
    handling. The returned verdict — PASS from any attempt, or the final FAIL — carries the
    per-attempt trail under :data:`TRAIL_KEY` (attempt number, cache-credited PASS count,
    remaining unmet count) WHEN a resumption was dispatched, so a resumed outcome stays
    diagnosable from the close output and the durable sidecar record alone; a single-attempt
    verdict keeps its exact prior shape (no trail key)."""
    from rebar import llm  # LAZY — preserves the core optionality contract
    from rebar.llm import completion_reconcile

    reused = _reusable_attested_pass(ticket_id, ref=ref, repo_root=repo_root)
    if reused is not None:
        return reused

    max_resumes = _max_resumes(cfg_root)
    trail: list[dict[str, Any]] = []
    prev_credited: int | None = None
    while True:
        # graph=False: the close gate verifies THIS ticket's OWN completion criteria, NOT its
        # whole descendant subtree. Children are separate tickets gated on their own close; the
        # agent reads the actual code regardless of whether child ticket TEXT is inlined, so
        # graph=True would only bloat the context and make an epic close re-verify the entire
        # feature in one run (impractical — it blows the step budget). The standalone
        # `rebar verify-completion <id> --graph` remains available for a deep human review.
        # source="attested", ref="HEAD" (epic raze-vet-ditch S4): the close gate verifies an
        # IMMUTABLE snapshot of the committed tree being closed (HEAD), not the live mutable
        # checkout — the fix for the motivating wrong-branch false-negative (the verdict is
        # reproducible + branch-independent) AND it makes the verdict SIGNABLE so the close signs
        # a `verified-at-sha` attestation (the child-closure gate trusts only children closed
        # with a certified signature). HEAD resolves offline (no origin needed) and is "the state
        # about to be pushed" for the single-dev flow. `source=local` (opt-in) is the read-only
        # verify-before-push back-out that never signs.
        # fetch=False: ref="HEAD" always resolves from the LOCAL object DB, so there is no
        # reason to hit the network — and fetching the real origin on every close would add
        # latency and a failure surface (a slow/unreachable remote) to a purely local verify.
        result = llm.verify_completion(
            ticket_id,
            graph=False,
            source="attested",
            ref=(ref or "HEAD"),
            fetch=False,
            repo_root=repo_root,
        )
        credited = _cache_credited_count(result)
        remaining = _unmet_count(result)
        attempt = len(trail) + 1
        trail.append({"attempt": attempt, "cache_credited": credited, "remaining_unmet": remaining})
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
    # Attach the trail only when a resumption actually ran: a single-attempt verdict keeps
    # its exact prior shape, so no existing consumer or pinned record changes.
    if len(trail) > 1:
        result[TRAIL_KEY] = trail
    return result
