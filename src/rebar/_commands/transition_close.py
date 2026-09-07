"""Finalize the transition close path after its parent cascade.

The close tail checks non-closed children and completion outside the store lock. Receipt-backed
passes publish the completion sidecar, status, and signature in one candidate commit. Other
paths commit status before optional signing. The tail also checks plan-review validity, records
best-effort force-close audit comments, cleans ticket scratch data, pushes non-bundled status
commits, and triggers compaction.

Keep transition recursion above this module to avoid an import cycle with :mod:`.transition`.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from collections.abc import Callable, Mapping
from typing import Any

from rebar import config
from rebar._commands import (
    scratch,
    txn,
)
from rebar._commands._seam import CommandError

# The completion-precheck cluster lives in close_precheck (ticket 74a3: module-size split
# along the existing call-graph seam). Re-imported here so the documented monkeypatch seam
# `transition_close._completion_precheck` keeps working unchanged.
from rebar._commands.close_precheck import _completion_precheck
from rebar._commands.completion_bundle import verdict_manifest as _verdict_manifest
from rebar.graph._unblock import batch_close_operations
from rebar.types import PLAN_REVIEW_REVIEWED_TYPES

logger = logging.getLogger(__name__)


_PLAN_REVIEW_CLOSE_TYPES = PLAN_REVIEW_REVIEWED_TYPES


def _new_close_metrics() -> dict[str, int]:
    metrics = dict.fromkeys(
        (
            "pre_verifier_total_ms structural_scan_ms material_policy_ms descendant_scope_ms "
            "landing_check_ms verifier_call_ms git_history_read_ms alias_index_build_ms "
            "ticket_ref_resolution_ms diff_validation_ms commits_inspected distinct_references "
            "descendant_ids referencing_commits_found"
        ).split(),
        0,
    )
    return metrics | {"_pre_verifier_started_ns": time.monotonic_ns()}


def _timed_close_phase(
    metrics: dict[str, int],
    metric_name: str,
    operation: Callable[..., Any],
    *args,
    **kwargs,
) -> Any:
    started_ns = time.monotonic_ns()
    result = operation(*args, **kwargs)
    metrics[metric_name] = (time.monotonic_ns() - started_ns) // 1_000_000
    return result


def _raise_plan_review_close_gate_error(ticket_id: str, check: dict[str, object]) -> None:
    """Raise the stable, separately-remediated plan-review close-gate error."""
    verdict = str(check.get("verdict", "unavailable"))
    reason = str(check.get("reason", "plan-review validity was unavailable")).rstrip(".")
    health = check.get("health")
    detail = ""
    if isinstance(health, dict):
        targets = health.get("targets") or []
        target_detail = ", ".join(
            f"{target.get('canonical_id')} {target.get('role')} {target.get('pin_status')}"
            for target in targets
            if isinstance(target, dict) and target.get("pin_status") != "current"
        )
        enforcement = health.get("enforcement_status")
        if enforcement not in ("enabled", "disabled"):
            enforcement = "enabled" if health.get("enforced") else "disabled"
        posture = (
            "advisory; enforcement disabled"
            if enforcement == "disabled" and health.get("advisory") is True
            else "enforcement disabled"
            if enforcement == "disabled"
            else "enforced"
        )
        pin_status = health.get("pin_status")
        related_material_status = health.get("related_material_status")
        if related_material_status == "no-related-material" or (
            related_material_status is None and pin_status == "current" and not targets
        ):
            pin_status = "current (no related material)"
        detail = (
            f" Health: {pin_status} "
            f"({posture}); "
            f"phase {health.get('signed_phase')} -> {health.get('required_phase')} "
            f"({health.get('phase_status')})"
        )
        if health.get("effective_execution_floor") is not None:
            detail += f"; floor={health['effective_execution_floor']}"
        if target_detail:
            detail += f"; targets: {target_detail}"
    raise CommandError(
        f"plan-review close gate: {verdict}: {reason}.{detail} "
        f"Run rebar review-plan {ticket_id} separately, then retry close.",
        returncode=1,
    )


def _is_full_sha(s: object) -> bool:
    """True for a full 40-char lowercase-hex git SHA (the shape ``head_sha`` and an attested
    ``verified-at-sha`` both take)."""
    return isinstance(s, str) and len(s) == 40 and all(c in "0123456789abcdef" for c in s.lower())


def _material_drifted(verified_sha: object, fresh_sha: object) -> bool:
    """Whether the code MATERIALLY drifted between verify and sign (story blackbear): true
    only when BOTH are real full SHAs that differ. A non-SHA / absent ``verified-at-sha``
    (an unattested/local verdict, or a test's synthetic marker) is not comparable → NOT
    drift, so the normal sign-on-PASS path is preserved."""
    return _is_full_sha(verified_sha) and _is_full_sha(fresh_sha) and verified_sha != fresh_sha


def _pin_completion_ref(ref: str | None, repo_root) -> str | None:
    """Resolve the completion target once before verification and signing.

    An explicit ref resolves locally without fetching and propagates resolution errors. A
    missing ref resolves ``HEAD`` on a best-effort basis and returns ``None`` on failure,
    preserving the lazy fallback. Returning a full SHA prevents ref movement from splitting
    verification and drift checks.
    """
    from rebar._snapshot.repo_snapshot import resolve_ref

    root = str(config.repo_root(repo_root))
    if ref is not None:
        return resolve_ref(ref, root, fetch=False)
    try:
        return resolve_ref("HEAD", root, fetch=False)
    except Exception:  # noqa: BLE001 — the DEFAULT pin is best-effort; lazy HEAD is the fallback
        return None


def sign_completion_verdict(result: dict, ticket_id: str, repo_root=None, *, signer=None) -> dict:
    """Build and sign the deterministic ``completion-verifier`` PASS manifest.

    The optional ``signer`` supplies a composed key and principal binding. Omitting it uses the
    local signing environment. The signature event is appended under ``repo_root`` and the
    signed record is returned. Signing failures propagate.
    """
    from rebar import signing as _signing

    manifest = _verdict_manifest(result, ticket_id, repo_root)
    return _signing.sign_manifest(
        ticket_id, manifest, kind="completion-verifier", repo_root=repo_root, signer=signer
    )


def record_completion_verdict(
    result: dict, ticket_id: str, repo_root=None, *, sign: bool = True
) -> dict[str, object]:
    """Record a standalone completion verdict for reuse by a close at the same ref.

    The resolved ticket receives a best-effort ``COMPLETION_VERDICT`` sidecar before any
    signature attempt. Signing occurs only when enabled for an attested, certifiable PASS with
    ``verified_at_sha``. Local, non-PASS, uncertifiable, signing-disabled, and unpinned results
    remain sidecar-only.

    Return ``signed``, ``cause``, ``sidecar_written``, and ``error``. ``cause`` is ``signed``,
    ``not_pass``, ``sign_disabled``, ``local_source``, ``not_certifiable``,
    ``no_verified_sha``, or ``sign_failed``. Sidecar and signing failures are logged without
    changing the caller's verdict.
    """
    from rebar import config as _config
    from rebar import signing as _signing
    from rebar._engine_support.resolver import resolve_ticket_id
    from rebar.llm import completion_sidecar

    # Bind the canonical id (an alias/short id resolves) so the sidecar lands in the right
    # ticket dir and the op-cert material/ticket steps match what the close later re-derives.
    tracker = str(_config.tracker_dir(repo_root))
    resolved_id = resolve_ticket_id(ticket_id, tracker) or ticket_id
    result.setdefault("ticket_id", resolved_id)

    try:
        sidecar_written = completion_sidecar.emit(result, material=None, repo_root=repo_root)
    except Exception:
        logger.warning(
            "standalone completion sidecar emit raised for %s; continuing",
            ticket_id,
            exc_info=True,
        )
        sidecar_written = False

    def _outcome(cause: str, *, error: str = "") -> dict[str, object]:
        return {"signed": False, "cause": cause, "sidecar_written": sidecar_written, "error": error}

    if str(result.get("verdict", "")).upper() != "PASS":
        return _outcome("not_pass")
    if not sign:
        return _outcome("sign_disabled")
    if result.get("source") == "local":
        return _outcome("local_source")
    if result.get("certifiable") is False:
        return _outcome("not_certifiable")
    if not result.get("verified_at_sha"):
        return _outcome("no_verified_sha")
    try:
        sign_completion_verdict(result, resolved_id, repo_root)
    except _signing.SigningError as exc:
        logger.warning("standalone completion sign failed for %s: %s", ticket_id, exc.message)
        return _outcome("sign_failed", error=exc.message)
    except Exception as exc:
        # The contract is "NEVER raises": any signing fault (a manifest build error, an
        # unexpected signing backend failure) degrades to sidecar-only, never the caller's problem.
        logger.warning(
            "standalone completion sign raised for %s; continuing", ticket_id, exc_info=True
        )
        return _outcome("sign_failed", error=str(exc))
    return {"signed": True, "cause": "signed", "sidecar_written": sidecar_written, "error": ""}


def _active_caused_by_targets(state: dict) -> list[str]:
    """The net-active ``caused_by`` targets already recorded on a reduced ticket state."""
    return [
        target
        for dep in state.get("deps") or []
        if dep.get("relation") == "caused_by" and (target := dep.get("target_id", ""))
    ]


def _resolve_caused_by_culprit(
    caused_by: str, existing: list[str], ticket_id: str, tracker: str, repo_root_str: str
) -> str | None:
    """The culprit this close should attribute the bug to, or ``None`` for "leave it alone".

    An explicit ``--caused-by`` is the operator's stated attribution and always resolves.
    An EMPTY flag falls through to :func:`rebar.metrics.blame.derive_caused_by` ONLY when the
    bug carries no ``caused_by`` edge yet (bug 10d0): blame is a guess, and a guess must never
    be added beside a proven edge — that is the wrong-target failure the ``/rebar-debug``
    guidance to always pass the flag exists to prevent. With no edge recorded, blame runs
    exactly as before.
    """
    if caused_by.strip():
        from rebar._engine_support.resolver import resolve_ticket_id

        return resolve_ticket_id(caused_by.strip(), tracker) or caused_by.strip()
    if existing:
        return None
    from rebar.metrics import blame

    return blame.derive_caused_by(ticket_id, repo_root_str, tracker)


def _apply_caused_by(
    ticket_id: str, caused_by: str, tracker: str, repo_root_str: str, repo_root
) -> None:
    """Update a bug's ``caused_by`` attribution after close without failing the close.

    A new explicit target replaces other active targets; an already-active target leaves all
    attribution unchanged. Without an explicit target, blame derives one only when no
    attribution exists. The low-level link writer permits the closed source. Resolution,
    removal, and write failures are logged and suppressed.
    """
    try:
        from rebar.reducer import reduce_ticket as _reduce

        state = _reduce(os.path.join(tracker, ticket_id)) or {}
        if state.get("ticket_type") != "bug":
            return

        existing = _active_caused_by_targets(state)
        culprit = _resolve_caused_by_culprit(caused_by, existing, ticket_id, tracker, repo_root_str)
        if not culprit or culprit == ticket_id or culprit in existing:
            return

        from rebar.graph._links import _write_link_event, remove_dependency

        tracker_dir = str(config.tracker_dir(repo_root))
        for superseded in existing:
            remove_dependency(ticket_id, superseded, tracker_dir, "caused_by")
        # Mark operator-supplied attribution as explicit and blame-derived attribution as derived.
        # Provenance lets consumers weight explicit evidence above derived guesses.
        provenance = "explicit" if caused_by.strip() else "derived"
        _write_link_event(ticket_id, culprit, "caused_by", tracker_dir, provenance=provenance)
    except Exception:
        logger.warning(
            "best-effort caused_by link on close of %s failed; close stands",
            ticket_id,
            exc_info=True,
        )


def _sign_completion_and_report(
    verified_result: dict, ticket_id: str, repo_root, ref: str | None
) -> dict:
    """Sign a non-bundled completion verdict after status has committed.

    Return ``completion_signature`` with ``signed``, ``cause``, and ``error``. The cause is
    ``signed``, ``material_drifted``, or ``sign_failed``. Material drift and signing failures
    leave the ticket closed, emit a warning, and do not raise.
    """
    import sys

    from rebar import signing as _signing

    # Status is already committed. Compare the manifest's verified SHA with a fresh target
    # resolution. Drift suppresses stale attestation without undoing the close.
    _manifest = _verdict_manifest(verified_result, ticket_id, repo_root)
    _verified_sha = _signing.verified_at_sha_from_manifest(_manifest)
    # Re-resolve the pinned target used for verification. Normally ``close_ticket`` passes an
    # immutable SHA for either default HEAD or an explicit ref. Only a failed default pin leaves
    # ``ref`` unset and falls back to live HEAD here.
    if ref and ref != "HEAD":
        from rebar._snapshot.repo_snapshot import resolve_ref

        _fresh_sha = resolve_ref(ref, str(config.repo_root(repo_root)), fetch=False)
    else:
        _fresh_sha = _signing.head_sha(config.repo_root(repo_root))
    if _material_drifted(_verified_sha, _fresh_sha):
        completion_signature = {"signed": False, "cause": "material_drifted", "error": ""}
        sys.stderr.write(
            f"Warning: closed {ticket_id} WITHOUT a completion signature — the code drifted "
            f"between verify ({str(_verified_sha)[:12]}) and sign ({str(_fresh_sha)[:12]}); "
            "not attesting stale state. To certify, reopen and re-close against the verified "
            f"commit: `rebar reopen {ticket_id}`, move it back to in_progress, then re-close "
            f"with `--ref {_verified_sha}`. (A plain re-close of an already-closed ticket is a "
            "no-op — reopen first.)\n"
        )
    else:
        try:
            # The shared producer step (story ee0b) — same seam call the trusted op-cert gate
            # service uses on a PASS verdict, so both producers mint the cert identically.
            sign_completion_verdict(verified_result, ticket_id, repo_root)
            completion_signature = {"signed": True, "cause": "signed", "error": ""}
        except _signing.SigningError as exc:
            # DEGRADE, never wedge (story 8d8e): op-cert signing needs ssh-keygen (OpenSSH
            # >= 8.9) and a writable tracker. When neither can produce a key the close ALREADY
            # committed, so this is the same closed-without-signature outcome as --force:
            # warn and skip signing (exit 0). Re-close once OpenSSH >= 8.9 is installed.
            completion_signature = {
                "signed": False,
                "cause": "sign_failed",
                "error": exc.message,
            }
            # Lead with what actually happened. The old text appended the raw signing error
            # to the warning, so a lock timeout read as "flock: could not acquire lock after
            # 60s" and an agent reasonably concluded the CLOSE had failed — while the close
            # had in fact committed ~60s earlier (bug silvern-dewy-damselfly). Say plainly
            # that the close LANDED and only the signature did not.
            sys.stderr.write(
                f"Warning: {ticket_id} IS CLOSED — the close committed. Only the completion "
                f"signature failed, so the ticket is closed WITHOUT one. Do NOT re-run the "
                f"transition (it would be a no-op). The signing error was: {exc.message} "
                f"Once signing is available, `rebar reopen {ticket_id}`, move it back to "
                "in_progress, and re-close to certify.\n"
            )
    return completion_signature


def _trigger_compaction(
    target_status: str, tracker: str, ticket_id: str, repo_root_str: str
) -> None:
    """Trigger operation-linked compaction after a close.

    The caller has released the store lock and completed its push. ``maybe_compact`` uses
    store-size-independent eligibility checks and normally delegates folding to a detached
    worker, so closing does not wait for the fold. This trigger keeps compaction available
    without a scheduled sweep.
    """
    if target_status != "closed":
        return
    from rebar._commands import compact_trigger

    compact_trigger.maybe_compact(tracker, ticket_id, repo_root=repo_root_str)


def _hint_disposition_alternative(close_class: str) -> None:
    """Suggest the attested disposition path for an administrative-shaped force close.

    Emit the hint for an administrative class or no class. Suppress it for bug-only classes
    because their bypass concerns completion. The stderr hint does not alter the committed close.
    """
    from rebar._commands import close_disposition

    if close_class and close_class not in close_disposition.ADMINISTRATIVE_CLASSES:
        return
    import sys

    sys.stderr.write(
        "Hint: if this close is administrative (duplicate / obsolete / superseded / wontfix "
        "rather than completed work), --class <value> closes it through the attested "
        "disposition path — obsolete/wontfix with --reason=<text>, duplicate/superseded with "
        "a live replacement link — no --force needed.\n"
    )


def _plan_review_close_recheck(
    ticket_id: str,
    ticket_state: Mapping[str, Any],
    *,
    repo_root,
    close_class: str,
    close_reason: str,
    tracker: str,
) -> Callable[[Mapping[str, Any]], None] | None:
    """Check plan-review validity before close and return its locked recheck.

    Configuration errors propagate and blocking results raise. Return ``None`` when
    ``gates.gate_ran`` reports a skip. Otherwise return a closure that repeats the check against
    locked state and fails closed if validity changes.
    """
    from rebar._commands import gates

    check = gates.close_plan_review_gate_check(
        ticket_id,
        ticket_state,
        repo_root=repo_root,
        close_class=close_class,
        close_reason=close_reason,
        tracker=tracker,
    )
    if not check.get("ok"):
        _raise_plan_review_close_gate_error(ticket_id, check)
    if not gates.gate_ran(check):
        return None

    def plan_review_recheck(locked_state: Mapping[str, Any]) -> None:
        locked_check = gates.close_plan_review_gate_check(
            ticket_id,
            locked_state,
            repo_root=repo_root,
            close_class=close_class,
            close_reason=close_reason,
            tracker=tracker,
        )
        if not locked_check.get("ok"):
            _raise_plan_review_close_gate_error(ticket_id, locked_check)

    return plan_review_recheck


def close_ticket(
    ticket_id: str,
    current_status: str,
    target_status: str,
    tracker: str,
    repo_root_str: str,
    repo_root,
    *,
    reason: str,
    close_reason: str = "",
    force_close: str,
    close_class: str = "",
    caused_by: str = "",
    ref: str | None = None,
) -> dict:
    """Run the close tail and return ``{ticket_id, from, to, newly_unblocked, noop}``.

    Structural and completion checks run outside the write lock. A receipt-bearing PASS
    publishes its three close artifacts together; other paths retain the established locked
    STATUS write and optional post-close signature. Non-close transitions write directly."""
    close_metrics = _new_close_metrics()
    newly_unblocked: list[str] = []
    if target_status == "closed":
        batch = _timed_close_phase(
            close_metrics,
            "structural_scan_ms",
            batch_close_operations,
            ticket_ids=[ticket_id],
            tracker_dir=tracker,
        )
        open_children = batch["open_children"]
        newly_unblocked = batch["newly_unblocked"]
        if open_children:
            count = len(open_children)
            # Child closure is structural integrity, not a quality gate: even a forced close
            # cannot put a parent over open children. Resolve/close or re-home them first.
            raise CommandError(
                f"Error: cannot close ticket '{ticket_id}' while it has {count} unresolved "
                "(non-closed) child ticket(s) — the child-closure invariant cannot be bypassed "
                "(not even with --force). Close or resolve these children first, or "
                "detach them (re-home), then close:\n" + "\n".join(open_children),
                returncode=1,
            )

    # Run completion verification outside the write lock. An applicable FAIL or unavailable
    # verifier blocks the close. A receipt-backed PASS publishes its sidecar, status, and
    # signature atomically. A non-bundled PASS signs after status. Qualified dispositions use a
    # deterministic sign signal. Force closes bypass completion and plan-review checks and do
    # not sign.
    #
    # Closing an ``idea`` rejects unimplemented work, so neither close check applies. The
    # open-child guard still applies. ``completion_signature`` stays absent for non-close
    # transitions and idea rejection.
    completion_signature: dict[str, object] | None = None
    verified_result: dict[str, Any] | None = None
    completion_expectation = ""
    plan_review_recheck = None
    if target_status == "closed" and current_status != "idea":
        # Pin the default or explicit ref once, then pass its SHA through verification and signing.
        ref = _pin_completion_ref(ref, repo_root)
        from rebar.reducer import reduce_ticket as _reduce

        ticket_state = _reduce(os.path.join(tracker, ticket_id)) or {}
        ticket_type = ticket_state.get("ticket_type", "")
        class_refusal = txn.close_class_refusal(
            str(ticket_type),
            close_class,
            close_reason=close_reason,
            force_close_reason=force_close,
            ticket_id=ticket_id,
            tracker=tracker,
        )
        if class_refusal:
            raise CommandError(f"Error: {class_refusal}", returncode=1)
        if not force_close and ticket_type in _PLAN_REVIEW_CLOSE_TYPES:
            plan_review_recheck = _timed_close_phase(
                close_metrics,
                "material_policy_ms",
                _plan_review_close_recheck,
                ticket_id,
                ticket_state,
                repo_root=repo_root,
                close_class=close_class,
                close_reason=close_reason,
                tracker=tracker,
            )

        precheck_result, completion_expectation = _completion_precheck(
            ticket_id,
            ticket_type,
            repo_root_str,
            repo_root,
            reason=close_reason,
            force_close=force_close,
            close_class=close_class,
            ref=ref,
            metrics=close_metrics,
        )
        if precheck_result is not None and not isinstance(precheck_result, dict):
            raise CommandError(
                "Error: completion precheck returned an invalid result shape", returncode=1
            )
        verified_result = precheck_result
    elif target_status == "closed":
        # `idea -> closed` is a reject/drop, not a completion: the gate never applied.
        completion_expectation = "not_applicable"

    from rebar._commands import _seam

    env_id = _seam.env_id(config.tracker_dir(repo_root))
    author = _seam.author("Unknown")
    from rebar._commands import completion_bundle

    completion_signature, atomic_close = completion_bundle._publish_close(
        verified_result,
        ticket_id=ticket_id,
        tracker=tracker,
        repo_root=repo_root,
        ref=ref,
        env_id=env_id,
        author=author,
        current_status=current_status,
        target_status=target_status,
        close_class=close_class,
        close_reason=close_reason,
        force_close=force_close,
        completion_expectation=completion_expectation,
        pre_status_check=plan_review_recheck,
        legacy_signer=_sign_completion_and_report,
    )

    # For a committed bug close, add ``caused_by`` only when atomic delivery is safe. The
    # explicit target wins over blame derivation. Link failures never undo the close.
    atomic_delivery = str((atomic_close or {}).get("delivery", ""))
    caused_by_safe = atomic_close is None or atomic_delivery in {
        "pushed",
        "pushed_after_ambiguous_ack",
        "local_only",
        "already_present",
    }
    if target_status == "closed" and caused_by_safe:
        _apply_caused_by(ticket_id, caused_by, tracker, repo_root_str, repo_root)

    # Reopen validity is computed from ``last_reopened_at`` during reads. Attestation records
    # remain immutable and retain every kind.

    # Force-close audit comment (best-effort, silenced — matches bash || true).
    if target_status == "closed" and force_close:
        # A force bypass closes without a completion signature and reports a distinct cause.
        completion_signature = {"signed": False, "cause": "force_bypassed", "error": ""}
        _hint_disposition_alternative(close_class)
        session = _resolve_session(tracker)
        body = (
            "FORCE_CLOSE: close gate(s) bypassed by user approval — no completion/signature "
            f'attestation was signed. Reason: "{force_close}". Session: {session}.'
        )
        try:
            from rebar._commands import leaf

            leaf.comment(ticket_id, body, repo_root=repo_root)
        except Exception:
            logger.warning(
                "could not write FORCE_CLOSE audit comment on %s; continuing",
                ticket_id,
                exc_info=True,
            )

    # Clean ticket scratch data after close. Unfolded logs remain valid, so compaction is
    # optional. Manual and scheduled sweeps remain available. The operation-linked trigger
    # delegates eligible folding after the store lock is released and the push completes,
    # keeping compaction outside the close transaction.
    if target_status == "closed":
        scratch.cleanup_for_ticket(repo_root_str, ticket_id)

    # The STATUS commit is now in the local tickets branch but unpushed —
    # txn.transition_core commits inline and does not go through write_and_push. Trigger
    # the same best-effort push so a trailing transition (the last write of a session)
    # isn't stranded (bug prone-octet-cheek).
    if atomic_close is None:
        from rebar._store import push

        push.push_after_commit(tracker)

    # A pending atomic delivery must not immediately launch another tracker mutation whose
    # generic push recovery could merge past the receipt conflict this close just refused.
    # The next healthy operation/scheduled sweep can trigger compaction after delivery is
    # resolved; compaction is optional housekeeping, never part of close correctness.
    if target_status != "closed" or caused_by_safe:
        _trigger_compaction(target_status, tracker, ticket_id, repo_root_str)

    result: dict = {
        "ticket_id": ticket_id,
        "from": current_status,
        "to": target_status,
        "newly_unblocked": newly_unblocked,
        "noop": False,
    }
    if completion_signature is not None:
        result["completion_signature"] = completion_signature
    if atomic_close is not None:
        result["atomic_close"] = atomic_close
    return result


def _resolve_session(tracker: str) -> str:
    """Resolve the event-provenance session id for the FORCE_CLOSE audit comment.

    Delegates to the shared :func:`rebar._commands.session_id.resolve_session_id`
    (epic crust-fetch-stump, story 6014) — which now INCLUDES ``CLAUDE_CODE_SESSION_ID``
    (its former omission here was the FORCE_CLOSE bug) — then keeps this call site's
    LOCAL cosmetic fallback (short git HEAD, then ``"unknown"``) so the audit comment is
    always a non-empty string. The shared resolver itself never returns HEAD.
    """
    from rebar._commands.session_id import resolve_session_id

    return resolve_session_id() or _short_head(tracker) or "unknown"


def _short_head(_tracker: str) -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout.strip()
    except Exception:  # noqa: BLE001 — short-HEAD is a session-id nicety; fall open to "" if git is unavailable
        return ""
