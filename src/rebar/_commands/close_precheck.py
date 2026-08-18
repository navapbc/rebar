"""The completion-verification close gate's PRE-close cluster (extracted from
transition_close.py at the existing call-graph seam — ticket 74a3; the file had crossed the
locked 800-LOC module-size cap).

:func:`_completion_precheck` is the gate's pre-lock half — the deterministic prechecks
(bug-class, replacement-link disposition, AC-checkbox completeness, file-impact →
referencing-commit) followed by the billable ``llm.verify_completion`` run and its
fail-closed error shaping. Its private helpers (:func:`_is_live_ticket`,
:func:`_has_live_replacement_link`, :func:`_recorded_replacement_target`,
:func:`_ensure_duplicate_close_is_linked`, :func:`_referencing_commit_exists`) are called only
from here. ``transition_close`` re-imports the public seam names so existing monkeypatch targets
(``transition_close._completion_precheck``) and test imports keep working unchanged.
"""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Mapping

from rebar import config
from rebar._commands import txn
from rebar._commands._seam import CommandError

logger = logging.getLogger(__name__)

_NON_COMPLETION_BUG_CLASSES = frozenset(
    {"duplicate", "not_a_bug", "escalated", "obsolete", "superseded", "wontfix"}
)

#: A sentinel distinguishing "this close is not a disposition at all" (keep normal completion
#: verification) from a disposition path that yielded no sign signal (``None`` — the close
#: proceeds unsigned). See :func:`_administrative_disposition`.
_NO_DISPOSITION = object()


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
    """True when a non-completion bug close names a live replacement.

    Replacement relationships are directional: either this bug duplicates a
    canonical ticket, or another ticket supersedes this bug. Reduced state and
    the inbound reader both expose only net-active links, including links baked
    into snapshots.
    """
    if close_class not in _NON_COMPLETION_BUG_CLASSES:
        return False
    if ticket_type != "bug":
        from rebar._commands import close_disposition

        # A non-bug close reaches the disposition path only through the ADMINISTRATIVE
        # subset (ticket fc20) — not_a_bug/escalated stay bug-only vocabulary, which
        # transition_core also refuses authoritatively at write time.
        if close_class not in close_disposition.ADMINISTRATIVE_CLASSES:
            return False

    from rebar.reducer import reduce_ticket

    try:
        state = reduce_ticket(os.path.join(tracker, ticket_id))
    except Exception:  # noqa: BLE001 -- an unreadable source must retain fail-closed verification
        return False
    if not isinstance(state, dict):
        return False

    for dep in state.get("deps") or []:
        if dep.get("relation") != "duplicates":
            continue
        target = dep.get("target_id", dep.get("target", ""))
        if target and _is_live_ticket(str(target), tracker):
            return True

    from rebar.reducer._inbound import find_inbound_relationships

    try:
        inbound = find_inbound_relationships(ticket_id, tracker)
    except Exception:  # noqa: BLE001 -- a failed graph read must retain fail-closed verification
        return False
    return any(
        link.get("relation") == "supersedes"
        and _is_live_ticket(str(link.get("from_id") or ""), tracker)
        for link in inbound.get("inbound_links") or []
    )


def _recorded_replacement_target(ticket_id: str, tracker: str) -> str | None:
    """The replacement ticket this bug NAMES, ignoring whether that target is usable.

    :func:`_has_live_replacement_link` answers "is there a usable replacement?"; this answers
    the strictly weaker "was one ever recorded?". The two differ in exactly one case — a link
    exists but its target is archived, deleted, or no longer resolvable — and that case earns a
    different remedy ("re-link to a live canonical") from having recorded nothing at all ("run
    ``rebar link``"). Kept SEPARATE from the predicate above rather than folded into it because
    that predicate's bool signature is a monkeypatch target in the disposition-attestation suite
    (bug 738a), and because the two questions genuinely differ.

    Reads only reduced state and the inbound graph — no LLM, no network. Any unreadable source
    yields ``None``, which routes to the more conservative "you named none" message.
    """
    from rebar.reducer import reduce_ticket

    try:
        state = reduce_ticket(os.path.join(tracker, ticket_id))
    except Exception:  # noqa: BLE001 -- an unreadable source degrades to the generic remedy
        state = None
    if isinstance(state, dict):
        for dep in state.get("deps") or []:
            if dep.get("relation") != "duplicates":
                continue
            target = str(dep.get("target_id", dep.get("target", "")) or "")
            if target:
                return target

    from rebar.reducer._inbound import find_inbound_relationships

    try:
        inbound = find_inbound_relationships(ticket_id, tracker)
    except Exception:  # noqa: BLE001 -- a failed graph read degrades to the generic remedy
        return None
    for link in inbound.get("inbound_links") or []:
        if link.get("relation") != "supersedes":
            continue
        source = str(link.get("from_id") or "")
        if source:
            return source
    return None


def _ensure_duplicate_close_is_linked(
    ticket_id: str, ticket_type: str, close_class: str, tracker: str
) -> None:
    """Block a ``--class duplicate`` close that names no usable canonical, naming the remedy.

    Before this existed the close fell through to the completion verifier, which correctly
    FAILED (a duplicate's defect is not resolved by the duplicate) but offered only two
    impossible remedies: finish work that belongs to the canonical ticket, or mark the criterion
    ``[operator-attested]`` — a false attestation. The one action that works, recording the
    link, was never named. Observed on bug 9b70, where the fix was a single
    ``rebar link 9b70 6a81 duplicates``.

    Deliberately NOT a claim about the canonical's status: a duplicate of an ALREADY-CLOSED
    canonical is the common case, so the gate asks only that the link exist.

    Scoped to the REPLACEMENT-BEARING classes — ``duplicate`` and (ticket fc20) ``superseded``,
    on ANY ticket type — rather than the whole ``_NON_COMPLETION_BUG_CLASSES`` set: ``not_a_bug``
    asserts there is no defect and ``escalated`` may point outside the tracker, so neither owes a
    link — since bug d54b both are reason-required instead (a live replacement link still
    satisfies them first), and the reason-only administrative classes (obsolete/wontfix) are
    justified by their ``--reason``; those keep their own paths.

    A GUARD FUNCTION, not an inline branch, on purpose — ``_completion_precheck`` sits at its
    recorded ceiling in ``.github/complexity-baseline.json``, which is shrink-only, so the
    decision points live here and the call site stays unconditional.
    """
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


def _check_work_landed(
    ticket_id: str, resolved_id: str, accepted_ids: set[str], tracker: str, code_root: str
) -> None:
    """The two DET landing checks, kept out of ``_completion_precheck`` so that function
    stays within its complexity ceiling: (1) a ticket recording ``file_impact`` must have a
    referencing commit at all, and (2) the linked commits' diffs must stay inside the
    declared impact."""
    from rebar._engine_support import field_reads

    if not _union_file_impact(accepted_ids, tracker):
        return
    referencing = _referencing_commits(accepted_ids, tracker, code_root)
    if field_reads.file_impact(ticket_id, tracker) and not referencing:
        raise CommandError(
            f"Error: cannot close {ticket_id}: it records file_impact (a code change) but no "
            f"commit references it (nor any of its descendants). Add a "
            f"'rebar-ticket: {resolved_id}' trailer to the commit "
            'that implements it, then retry (or override with --force="<reason>"). '
            "Completion verification cannot confirm the work landed without a referencing commit.",
            returncode=1,
        )
    _check_file_impact_vs_diff(accepted_ids, referencing, tracker, code_root)


def _attached_commit_shas(accepted_ids: set[str], tracker: str) -> list[str]:
    """SHAs recorded on the ticket (or a descendant) by COMMITS events — the
    ``rebar attach-commits`` repair surface."""
    from rebar.reducer import reduce_ticket

    shas: list[str] = []
    for ticket in sorted(accepted_ids):
        try:
            state = reduce_ticket(os.path.join(tracker, ticket))
        except Exception:  # noqa: BLE001 -- an unreadable sibling never blocks a close
            continue
        for record in (state or {}).get("commits") or []:
            sha = record.get("sha") if isinstance(record, Mapping) else record
            if isinstance(sha, str) and sha.strip():
                shas.append(sha.strip())
    return shas


def _union_file_impact(accepted_ids: set[str], tracker: str) -> list[str]:
    """``file_impact`` paths declared by the ticket OR any transitive descendant.

    Deliberately the SAME id set the referencing-commit scan credits. Reading only THIS
    ticket's impact would false-BLOCK a parent that declares ``file_impact`` when its
    children's commits touch child-owned paths, because those commits ARE credited to the
    parent. Unioning keeps the two scopes symmetric by construction.
    """
    from rebar._engine_support import field_reads

    paths: list[str] = []
    for ticket in sorted(accepted_ids):
        for entry in field_reads.file_impact(ticket, tracker) or []:
            path = entry.get("path") if isinstance(entry, Mapping) else entry
            if isinstance(path, str) and path.strip():
                paths.append(path.strip())
    return paths


def _check_file_impact_vs_diff(
    accepted_ids: set[str], referencing: list[str], tracker: str, code_root: str
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

    impact = _union_file_impact(accepted_ids, tracker)
    if not impact:
        return  # nothing declared anywhere in scope — out of scope for this check

    for sha in _attached_commit_shas(accepted_ids, tracker) + list(referencing):
        if commit_impact.is_merge_commit(sha, code_root):
            continue  # a merge authors nothing of its own; its parents are scanned instead
        paths = commit_impact.changed_paths(sha, code_root)
        if paths is None:
            if sha in referencing:
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
                + ", which the recorded file_impact does not declare. Declare them with "
                "`rebar set-file-impact <ticket> ...` and retry "
                '(or override with --force="<reason>").',
                returncode=1,
            )


def _referencing_commits(accepted_ids: set[str], tracker: str, repo_root) -> list[str]:
    """SHAs of commits referencing ANY of ``accepted_ids`` via a ``rebar-ticket:`` trailer
    (or a leading ``<id>:`` subject token), newest first.

    Each extracted candidate is put through the SAME shared resolver the commit-ticket gate
    uses (:func:`resolve_ticket_id`), so every id form — full / short / alias / Jira key /
    prefix — matches any of the accepted ids. Resolves run ``quiet`` — these are historical
    candidates the user never supplied, so an unrelated ambiguity is noise, not a diagnostic
    (bug af11); ambiguous candidates still resolve to ``None`` either way, so the decision is
    unchanged. Resolves are cached across commits. A git failure (not a repo, no commits)
    yields ``[]`` (no referencing commit found)."""
    from rebar._commands.verify_commit import extract_ticket_refs
    from rebar._engine_support.resolver import resolve_ticket_id

    proc = subprocess.run(
        ["git", "-C", str(repo_root), "log", "--format=%H%x1f%B%x00"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return []
    resolved_cache: dict[str, str | None] = {}
    found: list[str] = []
    for entry in proc.stdout.split("\0"):
        sha, _, message = entry.partition("\x1f")
        sha = sha.strip()
        if not sha:
            continue
        for ref in extract_ticket_refs(message):
            if ref not in resolved_cache:
                resolved_cache[ref] = resolve_ticket_id(ref, tracker, quiet=True)
            if resolved_cache[ref] in accepted_ids:
                found.append(sha)
                break
    return found


def _referencing_commit_exists(accepted_ids: set[str], tracker: str, repo_root) -> bool:
    """Whether ANY commit references one of ``accepted_ids`` — the long-standing bool
    contract, preserved verbatim as a thin wrapper over :func:`_referencing_commits` so
    existing call sites and the documented monkeypatch target keep working unchanged."""
    return bool(_referencing_commits(accepted_ids, tracker, repo_root))


def _emit_completion_sidecar(
    completion_sidecar, result, ticket_id: str, repo_root, *, is_pass: bool
) -> None:
    """Emit the PASS/FAIL COMPLETION_VERDICT sidecar, warning on stderr if it is DROPPED.

    Best-effort persistence (observability, never a gate): a failed/raised emit never changes
    the close outcome. But a dropped record — most often a write-lock ``LockTimeout`` that
    outlasted the retry (ticket ab54) — is no longer silent: inspect ``emit``'s bool and, on
    failure, write an explicit ``Warning: ... WITHOUT ...`` line naming the ticket (matching
    the transition_close convention) instead of leaving only a swallowed ``logger.warning``."""
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
    """Raise when the verdict is a "no verdict obtainable" FAULT (bug 2a6f), else return.

    ``reconcile_verdict`` marks a non-PASS verdict from which no failing criterion could be
    identified — a truncated or garbled structured turn. That is the verifier failing to answer,
    NOT evidence a criterion is unmet, and it used to surface as a bare
    ``- (unspecified): verifier returned FAIL without itemizing the failing criterion``, leaving
    the caller with nothing to remediate. Reported here BEFORE the unmet-criteria message so the
    caller is told to re-run instead of hunting for a requirement that was never evaluated, and
    with the retryable exit 11 every other transient degrade uses. Still fail-closed — raising
    means the ticket does not close. A separate function (rather than inline) so the caller's
    branch count is unchanged."""
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
    """The ``--class`` values a close of THIS ticket type may carry, as display text.

    Bugs take the full vocabulary; every other type takes only the administrative subset
    (ticket fc20). Kept in ``CLOSE_CLASSES`` order so the enumeration reads the same
    everywhere."""
    from rebar._commands import close_disposition

    if ticket_type == "bug":
        return ", ".join(txn.CLOSE_CLASSES)
    return ", ".join(c for c in txn.CLOSE_CLASSES if c in close_disposition.ADMINISTRATIVE_CLASSES)


def _verification_fail_message(
    ticket_id: str, ticket_type: str, result: dict, items: list, lines: list[str]
) -> str:
    """The refusal message for a completion-verification FAIL (still fail-closed either way).

    A verdict carrying the framework-derived top-level ``evidence_sufficient: false`` marker
    failed on INSUFFICIENT EVIDENCE — the bounded search exhausted without refuting anything —
    so the headline says that honestly instead of reporting fabricated "unmet criteria". The
    verdict's remediation (set by ``reconcile_verdict`` on every FAIL) is appended either way."""
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
    # Bounded auto-resume trail (ticket b5f8): when the gate re-ran the verifier before this
    # failure surfaced, say so honestly — per attempt: the cache-credited PASS count and the
    # remaining unmet count. Empty string for a close that never resumed (branch-free here,
    # so this function stays at its frozen complexity ceiling).
    from rebar._commands import close_autoresume

    message += close_autoresume.attempts_note(result)
    # Discoverability (ticket fc20, incident 9b70): a ticket whose work will deliberately NOT
    # be completed — duplicate/obsolete/superseded/wontfix — can never satisfy the verifier, so
    # the FAIL message must present the truthful exit rather than leaving --force as the only
    # visible door. Enumerated per ticket type so a task is never offered a bug-only class.
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
    """Route a qualifying disposition close to its deterministic sign signal (ticket fc20).

    Returns the verdict dict to sign, ``None`` (a disposition path that yields no signal — the
    close proceeds unsigned, the conservative direction), or the :data:`_NO_DISPOSITION`
    sentinel meaning "not a disposition — keep normal completion verification". A separate
    guard function on purpose: ``_completion_precheck`` sits at its recorded shrink-only
    complexity ceiling, so the decision points live here and its call site stays a single
    branch.

    Two doors, mirroring :func:`close_disposition.verdict`'s mints: a REASON-REQUIRED class
    (obsolete/wontfix always; not_a_bug/escalated unless a live replacement link stands in —
    bug d54b, checked replacement-first inside ``verdict``) is attested from its ``--reason``
    or replacement (validated by the shared class guard before this runs); every other
    disposition class still requires a net-active replacement link to a live counterpart
    (:func:`_has_live_replacement_link`). ATTESTING rather than withholding the signature is
    the 738a fix: an unsigned exempt close made the certification path count it as
    uncertified and withhold its parent's signature, with no honest exit."""
    from rebar._commands import close_disposition

    if close_class in close_disposition.REASON_REQUIRED_CLASSES:
        return close_disposition.verdict(ticket_id, close_class, tracker, close_reason=close_reason)
    if _has_live_replacement_link(ticket_id, ticket_type, close_class, tracker):
        return close_disposition.verdict(ticket_id, close_class, tracker)
    return _NO_DISPOSITION


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
):
    """The completion-verification close gate's PRE-close half (runs outside the write lock).

    Returns the PASS verdict ``result`` (the sign signal, fed to
    :func:`sign_completion_verdict` after a confirmed close), or ``None`` when the gate is off or
    the
    close is a ``--force`` (which closes WITHOUT verifying or signing — withholding the
    signed confirmation, so a closed-without-signature ticket is the durable signal that
    validation did not pass). Raises :class:`CommandError` (block) on a FAIL verdict, or when
    the LLM is unavailable / any verifier error (fail-closed). The ``rebar.llm`` import is LAZY
    so the optionality contract holds: core stays stdlib-only unless the gate is on AND a
    non-force close is attempted.

    THE RETURN CONTRACT HAS A THIRD CASE, and leaving it out of this docstring is part of what let
    bug 738a hide. A **disposition** close (a bug closed ``duplicate`` / ``not_a_bug`` /
    ``escalated`` while naming a live replacement) skips the billable verifier — it never claimed to
    implement its own criteria — but still returns a sign signal, built by
    :mod:`rebar._commands.close_disposition`. It used to return ``None``, so the closure was
    unsigned, and :func:`rebar.llm.completion.child_closure_findings` counts ANY unsigned closure as
    uncertified and withholds the parent's signature. Read as "unsigned implies gate-off or forced",
    which is what this docstring said, that state looked like a force-close and was reopened as one.
    So: ``None`` means off or forced; a disposition returns a verdict whose manifest says
    ``DISPOSITION``, not ``PASS``."""
    # session_log / code_review are lifecycle-exempt — they cannot be transitioned, so
    # transition_core will refuse this close authoritatively. Skip the gate BEFORE the (billable)
    # verifier runs, so a doomed close attempt never fires an LLM call.
    if ticket_type in ("session_log", "code_review", "identity"):
        return None
    from rebar._commands import gates

    # The tracker may be relocated outside the code checkout. Resolve code/config concerns
    # from the caller's explicit root (or REBAR_ROOT / the cwd git toplevel), while tracker
    # reads below continue to use the independently resolved tracker directory.
    code_root = str(config.repo_root(repo_root))

    # Shared resolution + fail-OPEN-on-unreadable-config posture (see _commands/gates.py).
    # The confirmed fail-CLOSED behavior still applies when the gate is readable-ON but the
    # LLM is unavailable (below).
    if not gates.gate_enabled(
        code_root,
        "require_completion_verification_for_close",
        ticket_id=ticket_id,
        gate_label="the completion-verification close gate",
        extra=" (other close gates still apply)",
    ):
        return None
    if force_close:
        return None  # close, but withhold the signed confirmation (no verify, no sign)

    # Cheap precondition BEFORE the billable LLM call: an invalid close-class combination
    # (missing bug class, non-administrative class on a non-bug, missing reason for a
    # reason-required class — tickets ed13 + fc20 + bug d54b). Shared rule
    # (:func:`txn.close_class_refusal`), so it cannot drift from transition_core's
    # authoritative write-side guard, which would reject the close anyway; failing here spares
    # the LLM call. ticket_id + tracker let the rule honor a not_a_bug/escalated close whose
    # live replacement link stands in for its --reason.
    tracker = str(config.tracker_dir(repo_root))
    refusal = txn.close_class_refusal(
        ticket_type, close_class, close_reason=reason, ticket_id=ticket_id, tracker=tracker
    )
    if refusal:
        raise CommandError(
            f"Error: {refusal} (checked before running completion verification).",
            returncode=1,
        )

    # An administrative/disposition close is a statement about where the work lives (or why it
    # will not happen), not a claim that this ticket's acceptance criteria were implemented.
    # Skip the completion-only checks (including file-impact and the billable verifier) when
    # the disposition qualifies: a reason-required class carries its --reason (or, for
    # not_a_bug/escalated, a replacement link instead), and a replacement-bearing class
    # carries a net-active link to a live counterpart. The close-class guard above and all
    # structural/write-time close guards still apply.
    disposition = _administrative_disposition(ticket_id, ticket_type, close_class, reason, tracker)
    if disposition is not _NO_DISPOSITION:
        return disposition

    # NO usable replacement. A `duplicate`/`superseded` close cannot be rescued by the
    # completion verifier — the work it would ask about lives on the replacement ticket — so
    # falling through printed advice that could not be followed. Fail HERE, naming the one
    # command that works. Scoped to the replacement-bearing classes (not the whole
    # non-completion set): `not_a_bug` asserts there is no defect and `escalated` may point
    # outside the tracker, so neither owes a link — since bug d54b they are reason-required
    # instead (a live replacement still short-circuits first) and never reach this fallthrough.
    # Deterministic and pre-LLM, so such a close never buys the wrong advice with a billable
    # request.
    _ensure_duplicate_close_is_linked(ticket_id, ticket_type, close_class, tracker)

    # AC-checkbox completeness precheck (DET, pre-LLM): unchecked items block close (433c).
    txn.ensure_ac_boxes_checked(ticket_id, tracker)

    # Attested-item validity precheck (DET, pre-LLM, bug 2f56-313f-6175-41b1): an
    # [operator-attested] item citing exact repo path/symbol evidence (attestation
    # laundering), or missing its complete `provenance:` continuation line, blocks the
    # close BEFORE the verifier can accept the tag at face value (ADR-0043).
    txn.ensure_attested_items_valid(ticket_id, tracker)

    # Deterministic precheck BEFORE the billable LLM call (alongside the open-children guard):
    # a ticket that records file_impact claims a concrete code change, so there MUST be a commit
    # that references it (a `rebar-ticket: <id>` trailer). If none exists, the implementation has
    # not landed and completion cannot be confirmed — fail fast (no LLM call).
    from rebar._engine_support.descendants import list_descendants
    from rebar._engine_support.resolver import resolve_ticket_id

    resolved_id = resolve_ticket_id(ticket_id, tracker) or ticket_id
    # Credit the ticket's ENTIRE descendant subtree: a parent (epic/story) whose code was
    # delivered by its children carries no commit referencing its OWN id, only the child ids.
    # Accept a referencing commit for the ticket or any of its descendants (transitive).
    accepted_ids = {resolved_id}
    descendants = list_descendants(ticket_id, tracker)
    for bucket in ("epics", "stories", "tasks", "bugs"):
        for desc_id in descendants.get(bucket, []):
            desc_resolved = resolve_ticket_id(desc_id, tracker)
            if desc_resolved is not None:
                accepted_ids.add(desc_resolved)
    _check_work_landed(ticket_id, resolved_id, accepted_ids, tracker, code_root)

    try:
        # The billable verifier run, wrapped in the bounded auto-resume loop (ticket b5f8):
        # an insufficiency-only FAIL (search exhaustion, nothing positively refuted) re-runs
        # `llm.verify_completion` itself — the verdict cache seeds the credited PASSes — up
        # to `verify.auto_resume_max` times while each attempt strictly grows the credited
        # count. The `graph=False` / `source="attested"` / `ref` / `fetch=False` call-site
        # rationale is documented on the helper, which imports `rebar.llm` LAZILY so the
        # optionality contract holds. Extracted along this call seam so this function stays
        # at its frozen complexity ceiling.
        from rebar._commands import close_autoresume

        result = close_autoresume.verify_with_auto_resume(
            ticket_id, ref=ref, repo_root=code_root, cfg_root=code_root
        )
    except Exception as exc:  # noqa: BLE001 — missing extra/key OR any verifier failure -> fail-closed (re-raise CommandError)
        from rebar.llm import failure as _failure

        # Shape B (story blackbear): thread the classifier disposition mamba/preflight attached
        # to the raised LLM error through to the process exit code. A retryable outage (429/5xx)
        # → exit 11 ("transient — retry"), else the existing fail-closed exit 1. CommandError
        # already carries `returncode` and transition.py returns it, so exit 11 propagates with
        # no plumbing change. The sanitized diagnostic is also written to the session log.
        _outcome = _failure.outcome_of(exc)
        _failure.log_degrade(_outcome, gate="completion-verify", ticket_id=ticket_id)
        _rc = 11 if (_outcome and _outcome.retryable) else 1
        _hint = ""
        if _outcome is not None:
            _msg = _failure.message_for(
                _outcome.resolution_class.value,
                finish_reason=(_outcome.diagnostic or {}).get("finish_reason"),
            )
            if _msg:
                _hint = f" [{_outcome.resolution_class.value}: {_msg}]"
        # Neither a bounded-recovery failure nor a mid-run verifier failure (e.g. a step-budget
        # exhaustion after minutes of real model calls) is an unavailable runtime, so the
        # "install the extra / set a key" remedy is reserved for actual unavailability.
        _remedy = _failure.close_gate_remedy(exc, _outcome)
        raise CommandError(
            f"Error: cannot close {ticket_id}: completion verification could not run "
            f"({exc}).{_hint} {_remedy} "
            'Override with --force="<reason>".',
            returncode=_rc,
        ) from None

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
        from rebar.llm import completion_sidecar

        result.setdefault("ticket_id", resolved_id)
        _emit_completion_sidecar(completion_sidecar, result, ticket_id, repo_root, is_pass=False)
        raise CommandError(message, returncode=1)
    # PASS: persist the lossless PASS record to the durable, queryable sidecar (story e7e0)
    # BEFORE any sign/early-return, so EVERY PASS close — including the local (opt-in) and the
    # certifiable=False (force-closed-descendant) unsigned paths below — leaves the positive
    # per-criterion `criteria[]` capture, mirroring the FAIL branch's emit. Best-effort:
    # persistence is observability and must NEVER affect the close outcome, so the emit is
    # wrapped and any exception logged and swallowed.
    from rebar.llm import completion_sidecar

    result.setdefault("ticket_id", resolved_id)
    _emit_completion_sidecar(completion_sidecar, result, ticket_id, repo_root, is_pass=True)
    # local source (opt-in back-out) verified + passed but is NEVER signed (epic
    # raze-vet-ditch S4: an unattested run produces no signature). Only an EXPLICIT local
    # verdict suppresses signing; the default close path is attested and signs (a verdict with
    # no source — e.g. a legacy caller — keeps the prior sign-on-PASS behavior). A local close
    # yields a closed-without-signature ticket (the documented "not attested" signal).
    if result.get("source") == "local":
        return None
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
        return None
    return result
