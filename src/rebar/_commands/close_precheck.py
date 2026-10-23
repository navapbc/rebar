"""Completion-verification close gate pre-close checks."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Mapping
from typing import Any

from rebar import config
from rebar._commands import close_disposition, txn
from rebar._commands._seam import CommandError
from rebar._store import freshness

logger = logging.getLogger(__name__)
_NON_COMPLETION_BUG_CLASSES = close_disposition.DISPOSITION_CLASSES
_NO_DISPOSITION = object()
_BOT_ALERT_DETECTORS = frozenset(
    {
        "dependency-advisory-canary",
        "heartbeat-canary",
        "binding-drift-canary",
    }
)
_STALE_REF_MESSAGE = (
    "this checkout could not refresh or see its remote refs before deciding whether a "
    "referencing commit exists. Fetch the code clone or retry from a current worktree; "
    "this is distinct from a proven absence of a rebar-ticket trailer."
)


def _is_live_ticket(ticket_id: str, tracker: str) -> bool:
    """Whether ``ticket_id`` resolves to a usable, non-retired ticket."""
    from rebar._engine_support.resolver import resolve_ticket_id
    from rebar.reducer import reduce_ticket

    resolved = resolve_ticket_id(ticket_id, tracker)
    if resolved is None:
        return False
    try:
        state = reduce_ticket(os.path.join(tracker, resolved))
    except Exception:  # noqa: BLE001 -- malformed/unreadable targets never earn a gate bypass
        return False
    return bool(
        isinstance(state, dict)
        and not state.get("error")
        and not state.get("archived")
        and state.get("status") not in {"archived", "deleted"}
    )


def _has_live_replacement_link(
    ticket_id: str,
    ticket_type: str,
    close_class: str,
    tracker: str,
) -> bool:
    """True for a usable replacement on non-completion bug close."""
    if close_class not in _NON_COMPLETION_BUG_CLASSES:
        return False
    if ticket_type != "bug" and close_class not in close_disposition.ADMINISTRATIVE_CLASSES:
        return False
    return (
        close_disposition.replacement_of(
            ticket_id, tracker, require_live=True, on_subject_unreadable="stop"
        )
        is not None
    )


def _recorded_replacement_target(ticket_id: str, tracker: str) -> str | None:
    """Return the named replacement even when it is unusable."""
    return close_disposition.replacement_of(
        ticket_id, tracker, require_live=False, on_subject_unreadable="continue"
    )


def _ensure_duplicate_close_is_linked(
    ticket_id: str, ticket_type: str, close_class: str, tracker: str
) -> None:
    """Reject duplicate close without a usable canonical ticket."""
    del ticket_type  # both classes owe a link on every ticket type (ticket fc20)
    if close_class not in ("duplicate", "superseded"):
        return
    if close_class == "duplicate":
        relation, remedy = "duplicates", f"rebar link {ticket_id} <canonical> duplicates"
    else:
        relation, remedy = "supersedes", f"rebar link <replacement> {ticket_id} supersedes"
    named = _recorded_replacement_target(ticket_id, tracker)
    if named:
        detail = (
            f"its '{relation}' link names {named}, which is archived, deleted, or no longer "
            "resolvable, so there is no replacement ticket left to point at. Re-link it to a "
            f"live one:\n  {remedy}"
        )
    else:
        detail = (
            f"it records no '{relation}' link, so it names no replacement ticket. Record the "
            f"relation first:\n  {remedy}"
        )
    raise CommandError(
        f"Error: cannot close {ticket_id} as --class {close_class}: {detail}\n"
        "The replacement ticket may already be closed — the gate requires only that the link "
        "exist, never that its target still be open. Without it there is nothing to verify: "
        "the work lives on the replacement ticket, not here, so completion verification "
        "would ask this ticket to prove work it never owned. "
        'Override with --force="<reason>" if there is genuinely no replacement.',
        returncode=1,
    )


def _resolved_completion_scope(
    ticket_id: str,
    tracker: str,
    *,
    metrics: dict[str, int] | None = None,
    ticket_view: Any | None = None,
) -> tuple[str, set[str]]:
    """Resolve the ticket, descendants, and disposition replacements to canonical ids."""
    from rebar._commands.close_scope import expand_with_disposition_replacements
    from rebar._engine_support.descendants import list_descendants
    from rebar._engine_support.resolver import resolve_ticket_id

    started_ns = time.monotonic_ns() if metrics is not None else 0
    if ticket_view is not None:
        resolved_id = ticket_view.resolve(ticket_id) or ticket_id
        accepted_ids = {resolved_id, *ticket_view.transitive_descendant_ids(resolved_id)}
        accepted_ids = expand_with_disposition_replacements(
            accepted_ids, tracker, ticket_view=ticket_view
        )
        if metrics is not None:
            metrics["descendant_ids"] = len(accepted_ids) - 1
            metrics["descendant_scope_ms"] = (time.monotonic_ns() - started_ns) // 1_000_000
        return resolved_id, accepted_ids
    resolved_id = resolve_ticket_id(ticket_id, tracker) or ticket_id
    accepted_ids = {resolved_id}
    descendants = list_descendants(ticket_id, tracker)
    for bucket in ("epics", "stories", "tasks", "bugs"):
        for desc_id in descendants.get(bucket, []):
            desc_resolved = resolve_ticket_id(desc_id, tracker)
            if desc_resolved is not None:
                accepted_ids.add(desc_resolved)
    accepted_ids = expand_with_disposition_replacements(accepted_ids, tracker)
    if metrics is not None:
        metrics["descendant_ids"] = len(accepted_ids) - 1
        metrics["descendant_scope_ms"] = (time.monotonic_ns() - started_ns) // 1_000_000
    return resolved_id, accepted_ids


def _check_work_landed(
    ticket_id: str,
    resolved_id: str,
    accepted_ids: set[str],
    tracker: str,
    code_root: str,
    *,
    metrics: dict[str, int] | None = None,
    ticket_view: Any | None = None,
) -> None:
    """The two DET landing checks, kept out of ``_completion_precheck`` so that function
    stays within its complexity ceiling: (1) a ticket recording ``file_impact`` must have a
    referencing commit at all, and (2) the linked commits' diffs must stay inside the
    declared impact."""
    from rebar._engine_support import field_reads

    started_ns = time.monotonic_ns() if metrics is not None else 0
    view_kwargs = {} if ticket_view is None else {"ticket_view": ticket_view}
    if not _union_file_impact(accepted_ids, tracker, **view_kwargs):
        if metrics is not None:
            metrics["landing_check_ms"] = (time.monotonic_ns() - started_ns) // 1_000_000
        return
    referencing = (
        _referencing_commits(accepted_ids, tracker, code_root)
        if metrics is None and ticket_view is None
        else _referencing_commits(accepted_ids, tracker, code_root, metrics=metrics)
        if ticket_view is None
        else _referencing_commits(
            accepted_ids, tracker, code_root, metrics=metrics, ticket_view=ticket_view
        )
    )
    attached = _attached_commit_shas(accepted_ids, tracker, **view_kwargs)
    landed = [*attached, *referencing]
    metrics_kw = {"metrics": metrics} if metrics is not None else {}
    own_impact = (
        ticket_view.field_value(ticket_id, "file_impact")
        if ticket_view is not None
        else field_reads.file_impact(ticket_id, tracker)
    )
    if own_impact and not landed:
        from rebar._commands import link_revert

        probe = link_revert._remote_probe(code_root)
        if probe == "present":
            if link_revert._refresh_remote_refs(code_root):
                referencing = _referencing_commits(
                    accepted_ids,
                    tracker,
                    code_root,
                    metrics=metrics,
                    ticket_view=ticket_view,
                    include_upstream=True,
                )
            else:
                raise CommandError(
                    f"Error: cannot close {ticket_id}: {_STALE_REF_MESSAGE}",
                    returncode=1,
                )
        elif probe == "unknown":
            raise CommandError(
                f"Error: cannot close {ticket_id}: {_STALE_REF_MESSAGE}",
                returncode=1,
            )
        landed = [*attached, *referencing]
    if own_impact and not landed:
        raise CommandError(
            f"Error: cannot close {ticket_id}: it records file_impact (a code change) but no "
            f"commit references it (nor any of its descendants). Add a "
            f"'rebar-ticket: {resolved_id}' trailer to the commit "
            'that implements it, then retry (or override with --force="<reason>"). '
            "Completion verification cannot confirm the work landed without a referencing commit.",
            returncode=1,
        )
    _check_file_impact_vs_diff(
        accepted_ids,
        landed,
        tracker,
        code_root,
        **view_kwargs,
        **metrics_kw,
    )
    if metrics is not None:
        metrics["landing_check_ms"] = (time.monotonic_ns() - started_ns) // 1_000_000


def _attached_commit_shas(
    accepted_ids: set[str], tracker: str, *, ticket_view: Any | None = None
) -> list[str]:
    """SHAs recorded on the ticket (or a descendant) by COMMITS events — the
    ``rebar attach-commits`` repair surface."""
    from rebar.reducer import reduce_ticket

    shas: list[str] = []
    for ticket in sorted(accepted_ids):
        try:
            records = (
                ticket_view.field_value(ticket, "commits")
                if ticket_view is not None
                else (reduce_ticket(os.path.join(tracker, ticket)) or {}).get("commits")
            )
        except Exception:  # The legacy live path treats an unreadable sibling as unattached.
            if ticket_view is not None:
                raise
            continue
        for record in records or []:
            sha = record.get("sha") if isinstance(record, Mapping) else record
            if isinstance(sha, str) and sha.strip():
                shas.append(sha.strip())
    return shas


def _union_file_impact(
    accepted_ids: set[str], tracker: str, *, ticket_view: Any | None = None
) -> list[str]:
    """``file_impact`` paths declared by the ticket OR any transitive descendant.

    Deliberately the SAME id set the referencing-commit scan credits. Reading only THIS
    ticket's impact would false-BLOCK a parent that declares ``file_impact`` when its
    children's commits touch child-owned paths, because those commits ARE credited to the
    parent. Unioning keeps the two scopes symmetric by construction.
    """
    from rebar._engine_support import field_reads

    paths: list[str] = []
    for ticket in sorted(accepted_ids):
        entries = (
            ticket_view.field_value(ticket, "file_impact")
            if ticket_view is not None
            else field_reads.file_impact(ticket, tracker)
        )
        for entry in entries or []:
            path = entry.get("path") if isinstance(entry, Mapping) else entry
            if isinstance(path, str) and path.strip():
                paths.append(path.strip())
    return paths


def _check_file_impact_vs_diff(
    accepted_ids: set[str],
    referencing: list[str],
    tracker: str,
    code_root: str,
    *,
    metrics: dict[str, int] | None = None,
    ticket_view: Any | None = None,
) -> None:
    """DET close check: every changed path of a linked commit must be declared or exempt.

    Deterministic and LLM-free. Commit discovery unions the two sources that already exist:
    the COMMITS events written by ``rebar attach-commits``, and the referencing-commit scan
    above. Git errors are handled asymmetrically because the two sources differ in kind: a
    scanned commit is local by construction, so failing to read it is anomalous and fails
    CLOSED, whereas an attached SHA may legitimately live in another clone (or simply be
    unfetched), so it is skipped rather than blocking a repair surface built to record it.
    """
    from rebar._engine_support import commit_impact

    started_ns = time.monotonic_ns() if metrics is not None else 0
    view_kwargs = {} if ticket_view is None else {"ticket_view": ticket_view}
    impact = _union_file_impact(accepted_ids, tracker, **view_kwargs)
    if not impact:
        if metrics is not None:
            metrics["diff_validation_ms"] = (time.monotonic_ns() - started_ns) // 1_000_000
        return  # nothing declared anywhere in scope — out of scope for this check

    attached = _attached_commit_shas(accepted_ids, tracker, **view_kwargs)
    checked: set[str] = set()
    for sha in attached + list(referencing):
        if sha in checked:
            continue
        checked.add(sha)
        if commit_impact.is_merge_commit(sha, code_root):
            continue  # a merge authors nothing of its own; its parents are scanned instead
        paths = commit_impact.changed_paths(sha, code_root)
        if paths is None:
            if sha not in attached:
                raise CommandError(
                    f"Error: cannot close: commit {sha} references this ticket but could not "
                    "be read from this repository, so its changed paths cannot be verified "
                    'against the recorded file_impact (override with --force="<reason>").',
                    returncode=1,
                )
            continue  # an attached SHA absent from this clone is not evidence of a problem
        undeclared = commit_impact.undeclared_paths(paths, impact, repo_root=code_root)
        if undeclared:
            raise CommandError(
                f"Error: cannot close: commit {sha} changes "
                + ", ".join(undeclared)
                + ", which the recorded file_impact does not declare, so the work went "
                "beyond the reviewed plan. Declare them with "
                "`rebar set-file-impact <ticket> ...` — widening file_impact is a MATERIAL "
                "plan change, so it invalidates the plan-review attestation "
                "(stale-material) and `rebar review-plan <ticket>` must be re-run before "
                'the close will succeed (or override with --force="<reason>").',
                returncode=1,
            )
    if metrics is not None:
        metrics["diff_validation_ms"] = (time.monotonic_ns() - started_ns) // 1_000_000


def _referencing_commits(
    accepted_ids: set[str],
    tracker: str,
    repo_root,
    metrics: dict[str, int] | None = None,
    ticket_view: Any | None = None,
    include_upstream: bool = False,
) -> list[str]:
    """SHAs of commits referencing ANY of ``accepted_ids``, newest first."""
    from rebar._engine_support import commit_impact

    scan_kwargs: dict[str, Any] = {"metrics": metrics}
    if ticket_view is not None:
        scan_kwargs["resolver"] = ticket_view.resolve
    if include_upstream:
        scan_kwargs["include_upstream"] = True
    commits = (
        commit_impact.referencing_commits(
            accepted_ids,
            tracker,
            str(repo_root),
            **scan_kwargs,
        )
        or []
    )
    if metrics is not None:
        metrics["referencing_commits_found"] = len(commits)
    return commits


def _emit_completion_sidecar(
    completion_sidecar, result, ticket_id: str, repo_root, *, is_pass: bool
) -> None:
    """Best-effort persistence of the completion verdict sidecar.
    Warn on stderr when the record is dropped without changing close outcome."""
    try:
        emitted = completion_sidecar.emit(result, material=None, repo_root=repo_root)
    except Exception:
        logger.warning("completion sidecar emit raised; close outcome unchanged", exc_info=True)
        emitted = False
    if emitted:
        return
    import sys

    if is_pass:
        sys.stderr.write(
            f"Warning: closing {ticket_id} WITHOUT a durable COMPLETION_VERDICT sidecar — the "
            "PASS completion record could not be written (write-lock contention or store "
            "error). The close still succeeds and stays certified; only the offline-queryable "
            "artifact was lost.\n"
        )
    else:
        sys.stderr.write(
            f"Warning: completion FAIL for {ticket_id} recorded WITHOUT a durable "
            "COMPLETION_VERDICT sidecar — the record could not be written (write-lock "
            "contention or store error). The FAIL still blocks the close; only the "
            "offline-queryable artifact was lost.\n"
        )


def _raise_on_verifier_fault(
    result: dict, items: list, ticket_id: str, resolved_id: str, repo_root
) -> None:
    """Raise retryable exit 11 when a non-PASS verdict names no failing
    criterion. The ticket stays open because the verifier gave no usable
    finding, rather than evidence of unmet work."""
    if result.get("verdict_obtainable") is not False:
        return
    detail = str((items[0].get("detail") if items else "") or "")
    from rebar.llm import completion_sidecar as _cs

    result.setdefault("ticket_id", resolved_id)
    _emit_completion_sidecar(_cs, result, ticket_id, repo_root, is_pass=False)
    raise CommandError(
        f"Error: cannot close {ticket_id}: the completion verifier produced NO usable verdict "
        f"— {detail} Re-run the close to retry the verification.",
        returncode=11,
    )


def _applicable_close_classes(ticket_type: str) -> str:
    """List close classes in canonical order. Bugs use all classes.
    Other ticket types use administrative classes only."""
    from rebar._commands import close_disposition

    if ticket_type == "bug":
        return ", ".join(txn.CLOSE_CLASSES)
    return ", ".join(c for c in txn.CLOSE_CLASSES if c in close_disposition.ADMINISTRATIVE_CLASSES)


def _verification_fail_message(
    ticket_id: str, ticket_type: str, result: dict, items: list, lines: list[str]
) -> str:
    """Explain a completion failure. Label evidence-search exhaustion as
    insufficient evidence and append the verdict's remediation."""
    noun = "criterion" if len(items) == 1 else "criteria"
    if result.get("evidence_sufficient") is False:
        headline = (
            f"Error: completion verification FAILED for {ticket_id} — insufficient evidence "
            f"for {len(items)} {noun} (nothing positively refuted); not closing.\n"
        )
    else:
        headline = (
            f"Error: completion verification FAILED for {ticket_id} — {len(items)} unmet "
            "criteria; not closing.\n"
        )
    message = headline + "\n".join(lines)
    guidance = result.get("remediation")
    if guidance:
        message += "\n\n  " + guidance
    # Include attempt, cached PASS, and unmet counts only when the verifier
    # resumed.
    from rebar._commands import close_autoresume

    message += close_autoresume.attempts_note(result)
    # Show applicable disposition classes for deliberately unfinished work.
    message += (
        "\n\n  If this ticket's work is NOT meant to be completed (it is a duplicate, its "
        "premise is obsolete, it was superseded, or it is a deliberate wontfix), close it as "
        f"an attested disposition instead: --class <value> — one of: "
        f"{_applicable_close_classes(ticket_type)}. obsolete/wontfix take --reason=<text>; "
        "duplicate/superseded need a live replacement link."
    )
    if ticket_type == "bug":
        message += (
            " not_a_bug/escalated take --reason=<text> or a live replacement link (bug d54b)."
        )
    return message


def _administrative_disposition(
    ticket_id: str, ticket_type: str, close_class: str, close_reason: str, tracker: str
):
    """Return a deterministic disposition sign signal when a reason or usable
    replacement qualifies. Otherwise keep normal verification or return an
    unsigned disposition result."""
    from rebar._commands import close_disposition

    if close_class in close_disposition.REASON_REQUIRED_CLASSES:
        return close_disposition.verdict(ticket_id, close_class, tracker, close_reason=close_reason)
    if _has_live_replacement_link(ticket_id, ticket_type, close_class, tracker):
        return close_disposition.verdict(ticket_id, close_class, tracker)
    return _NO_DISPOSITION


def _bot_alert_disposition(ticket_id: str, close_class: str, close_reason: str, tracker: str):
    if close_class != "env_integration":
        return _NO_DISPOSITION
    from rebar.reducer import reduce_ticket

    try:
        state = reduce_ticket(os.path.join(tracker, ticket_id)) or {}
    except Exception as exc:
        raise CommandError(
            "Error: --class env_integration requires bot-alert detected_by provenance, "
            "but the ticket could not be read.",
            returncode=1,
        ) from exc
    detected_by = str(state.get("detected_by") or "")
    if detected_by not in _BOT_ALERT_DETECTORS:
        return _NO_DISPOSITION
    if not close_reason:
        raise CommandError(
            "Error: --class env_integration bot-alert closes require --reason=<text> "
            "describing the observed recovery.",
            returncode=1,
        )
    return {
        "verdict": "PASS",
        "disposition": close_class,
        "detected_by": detected_by,
        "close_reason": close_reason,
        "model": "none (deterministic disposition)",
        "runner": "close_precheck.bot_alert_disposition",
        "findings": [],
    }


def _gate_skip_expectation(ticket_id: str, code_root: str, force_close: str) -> str | None:
    """Record gate_off when disabled or force_bypassed when an enabled gate is
    forced. Resolve gate state first. Unreadable config raises instead of
    producing a skip."""
    from rebar._commands import gates

    state = gates.gate_enabled(
        code_root,
        "require_completion_verification_for_close",
        ticket_id=ticket_id,
        gate_label="the completion-verification close gate",
    )
    if not state:
        return "gate_off"
    if force_close:
        # close, but withhold the signed confirmation (no verify, no sign)
        return "force_bypassed"
    return None


def _verify_with_duration_metrics(
    ticket_id: str,
    *,
    ref: str | None,
    code_root: str,
    metrics: dict[str, int] | None = None,
    ticket_view: Any | None = None,
    ticket_read_mode: str | None = None,
) -> dict:
    """Compatibility seam for duration tests and transition-close monkeypatches."""
    from rebar._commands.completion_verifier_run import run_close_verifier

    return run_close_verifier(
        ticket_id,
        ref=ref,
        code_root=code_root,
        metrics=metrics,
        ticket_view=ticket_view,
        ticket_read_mode=ticket_read_mode,
        clock=time.monotonic_ns,
    )


def _optional_metrics_kwargs(
    metrics: dict[str, int] | None,
) -> dict[str, dict[str, int]]:
    """Omit the optional collector keyword entirely for legacy call compatibility."""
    return {} if metrics is None else {"metrics": metrics}


def _completion_precheck(
    ticket_id: str,
    ticket_type: str,
    cfg_root: str,
    repo_root,
    *,
    reason: str,
    force_close: str,
    close_class: str = "",
    ref: str | None = None,
    metrics: dict[str, int] | None = None,
):
    """Return the pre-close verdict and durable completion expectation.
    Use a signed completion PASS, a deterministic disposition, or an
    explicit reason why certification is absent. Fail closed on verifier
    failure or error. Import the optional LLM only when needed."""
    # session_log / code_review are lifecycle-exempt — they cannot be transitioned, so
    # transition_core will refuse this close authoritatively. Skip the gate BEFORE the (billable)
    # verifier runs, so a doomed close attempt never fires an LLM call.
    if ticket_type in ("session_log", "code_review", "identity"):
        return None, ""

    # The tracker may be relocated outside the code checkout. Resolve code/config concerns
    # from the caller's explicit root (or REBAR_ROOT / the cwd git toplevel), while tracker
    # reads below continue to use the independently resolved tracker directory.
    code_root = str(config.repo_root(repo_root))

    # Validate close-class shape before any billable verifier call. This is the same guard
    # transition_core will enforce under the write lock.
    tracker = str(config.tracker_dir(repo_root))
    if force_close:
        skip = _gate_skip_expectation(ticket_id, code_root, force_close)
        if skip:
            return None, skip

    refusal = txn.close_class_refusal(
        ticket_type, close_class, close_reason=reason, ticket_id=ticket_id, tracker=tracker
    )
    if refusal:
        raise CommandError(
            f"Error: {refusal} (checked before running completion verification).",
            returncode=1,
        )

    # Disposition closes attest why completion is not being claimed; skip the verifier.
    disposition = _administrative_disposition(ticket_id, ticket_type, close_class, reason, tracker)
    if disposition is not _NO_DISPOSITION:
        return disposition, "disposition"
    disposition = _bot_alert_disposition(ticket_id, close_class, reason, tracker)
    if disposition is not _NO_DISPOSITION:
        return disposition, "disposition"

    skip = _gate_skip_expectation(ticket_id, code_root, force_close)
    if skip:
        return None, skip
    # Gate is ON and unforced: refuse a stale store BEFORE the billable verifier (bug b928).
    freshness.assert_gate_store_fresh("the completion close gate", ticket_id, repo_root)

    # A duplicate/superseded close without a usable replacement cannot be verified as complete.
    _ensure_duplicate_close_is_linked(ticket_id, ticket_type, close_class, tracker)

    # Capture once before any completion-specific deterministic read. The same view is passed
    # by identity through every auto-resume attempt and closed here after its receipt is sealed.
    from rebar.llm.completion import capture_completion_ticket_view

    ticket_view, ticket_read_mode = capture_completion_ticket_view(
        ticket_id, code_root, fetch=False
    )
    try:
        pinned_state = ticket_view.show_ticket(ticket_id) if ticket_view is not None else None
        if pinned_state is not None:
            ticket_type = str(pinned_state.get("ticket_type") or ticket_type)
        state_kwargs = {} if pinned_state is None else {"ticket_state": pinned_state}
        txn.ensure_ac_boxes_checked(ticket_id, tracker, **state_kwargs)
        txn.ensure_attested_items_valid(ticket_id, tracker, **state_kwargs)
        metrics_kwargs = _optional_metrics_kwargs(metrics)
        view_kwargs = {} if ticket_view is None else {"ticket_view": ticket_view}
        resolved_id, accepted_ids = _resolved_completion_scope(
            ticket_id, tracker, **view_kwargs, **metrics_kwargs
        )
        _check_work_landed(
            ticket_id,
            resolved_id,
            accepted_ids,
            tracker,
            code_root,
            **view_kwargs,
            **metrics_kwargs,
        )
        result = _verify_with_duration_metrics(
            ticket_id,
            ref=ref,
            code_root=code_root,
            ticket_view=ticket_view,
            ticket_read_mode=ticket_read_mode,
            **metrics_kwargs,
        )
    finally:
        if ticket_view is not None:
            ticket_view.close()

    if str(result.get("verdict", "")).upper() != "PASS":
        items = result.get("findings", []) or []
        lines = [
            f"  - {(f.get('criterion') or f.get('dimension') or '?')}: {f.get('detail', '')}"
            for f in items[:20]
        ]
        # "No verdict obtainable" (bug 2a6f) is a VERIFIER FAULT, not a judgement that the work
        # is incomplete. Raises when marked; a plain return means this is a real FAIL.
        _raise_on_verifier_fault(result, items, ticket_id, resolved_id, repo_root)
        # Surface the verdict's remediation guidance (set by reconcile_verdict on every FAIL) so
        # the caller is pointed at the evidence channel — documenting proof that a requirement is
        # met as a comment on the ticket — rather than left with only the bare list of criteria.
        message = _verification_fail_message(ticket_id, ticket_type, result, items, lines)
        # Persist the FAIL verdict to a durable, queryable sidecar (ticket 24ec) BEFORE the
        # raise, so a completion FAIL leaves an artifact (mirroring the plan-review
        # REVIEW_RESULT sidecar) instead of vanishing. Best-effort: emit swallows its own
        # errors and returns False; it never changes the close outcome or masks the FAIL —
        # the raise below still fires unconditionally. Supply the canonical id so the record
        # lands in the resolved ticket dir, and material=None (no fingerprint is computed on
        # the FAIL path).
        from rebar.llm import completion_reconcile, completion_sidecar

        result.setdefault("ticket_id", resolved_id)
        _emit_completion_sidecar(completion_sidecar, result, ticket_id, repo_root, is_pass=False)
        raise CommandError(
            message, returncode=completion_reconcile.completion_fail_returncode(result)
        )
    # PASS: persist the lossless PASS record to the durable, queryable sidecar (story e7e0)
    # BEFORE any sign/early-return, so EVERY PASS close — including the local (opt-in) and the
    # certifiable=False (force-closed-descendant) unsigned paths below — leaves the positive
    # per-criterion `criteria[]` capture, mirroring the FAIL branch's emit. Best-effort:
    # persistence is observability and must NEVER affect the close outcome, so the emit is
    # wrapped and any exception logged and swallowed.
    from rebar._commands.completion_bundle import has_atomic_basis
    from rebar.llm import completion_sidecar

    result.setdefault("ticket_id", resolved_id)
    # A fresh lazy PASS publishes its sidecar together with STATUS + SIGNATURE in the
    # atomic close bundle. Every legacy/local/uncertifiable path retains the established
    # best-effort early sidecar behavior.
    if not has_atomic_basis(result):
        _emit_completion_sidecar(completion_sidecar, result, ticket_id, repo_root, is_pass=True)
    # local source (opt-in back-out) verified + passed but is NEVER signed (epic
    # raze-vet-ditch S4: an unattested run produces no signature). Only an EXPLICIT local
    # verdict suppresses signing; the default close path is attested and signs (a verdict with
    # no source — e.g. a legacy caller — keeps the prior sign-on-PASS behavior). A local close
    # yields a closed-without-signature ticket (the documented "not attested" signal).
    if result.get("source") == "local":
        return None, "local_source"
    # A closed-but-uncertified (force-closed) descendant WITHHOLDS certification (it
    # propagates): close WITHOUT signing, and SAY SO (bug 96d1 — this path was silent,
    # unlike the drift/SigningError arms of the same seam, which both warn).
    if result.get("certifiable") is False:
        import sys

        sys.stderr.write(
            f"Warning: closing {ticket_id} WITHOUT a completion signature — certification "
            "withheld: an uncertified (e.g. force-closed) descendant leaves the subtree "
            "unattested. Re-close that descendant through the gate, then reopen and re-close.\n"
        )
        return None, "not_certifiable"
    return result, "required"
