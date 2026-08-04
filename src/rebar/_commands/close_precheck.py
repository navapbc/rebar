"""The completion-verification close gate's PRE-close cluster (extracted from
transition_close.py at the existing call-graph seam — ticket 74a3; the file had crossed the
locked 800-LOC module-size cap).

:func:`_completion_precheck` is the gate's pre-lock half — the deterministic prechecks
(bug-class, replacement-link disposition, AC-checkbox completeness, file-impact →
referencing-commit) followed by the billable ``llm.verify_completion`` run and its
fail-closed error shaping. Its three private helpers (:func:`_is_live_ticket`,
:func:`_has_live_replacement_link`, :func:`_referencing_commit_exists`) are called only from
here. ``transition_close`` re-imports the public seam names so existing monkeypatch targets
(``transition_close._completion_precheck``) and test imports keep working unchanged.
"""

from __future__ import annotations

import logging
import os
import subprocess

from rebar import config
from rebar._commands import txn
from rebar._commands._seam import CommandError

logger = logging.getLogger(__name__)

_NON_COMPLETION_BUG_CLASSES = frozenset({"duplicate", "not_a_bug", "escalated"})


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
    if ticket_type != "bug" or close_class not in _NON_COMPLETION_BUG_CLASSES:
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


def _referencing_commit_exists(accepted_ids: set[str], tracker: str, repo_root) -> bool:
    """True if any commit reachable from the code repo's history references ANY of
    ``accepted_ids`` via a ``rebar-ticket:`` trailer (or a leading ``<id>:`` subject token).

    Each extracted candidate is put through the SAME shared resolver the commit-ticket gate
    uses (:func:`resolve_ticket_id`), so every id form — full / short / alias / Jira key /
    prefix — matches any of the accepted ids. Resolves run ``quiet`` — these are historical
    candidates the user never supplied, so an unrelated ambiguity is noise, not a diagnostic
    (bug af11); ambiguous candidates still resolve to ``None`` either way, so the decision is
    unchanged. Resolves are cached across commits. A git failure (not a repo, no commits)
    yields ``False`` (no referencing commit found)."""
    from rebar._commands.verify_commit import extract_ticket_refs
    from rebar._engine_support.resolver import resolve_ticket_id

    proc = subprocess.run(
        ["git", "-C", str(repo_root), "log", "--format=%B%x00"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return False
    resolved_cache: dict[str, str | None] = {}
    for message in proc.stdout.split("\0"):
        for ref in extract_ticket_refs(message):
            if ref not in resolved_cache:
                resolved_cache[ref] = resolve_ticket_id(ref, tracker, quiet=True)
            if resolved_cache[ref] in accepted_ids:
                return True
    return False


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
    close is a ``--force-close`` (which closes WITHOUT verifying or signing — withholding the
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

    # Shared resolution + fail-OPEN-on-unreadable-config posture (see _commands/gates.py).
    # The confirmed fail-CLOSED behavior still applies when the gate is readable-ON but the
    # LLM is unavailable (below).
    if not gates.gate_enabled(
        cfg_root,
        "require_completion_verification_for_close",
        ticket_id=ticket_id,
        gate_label="the completion-verification close gate",
        extra=" (other close gates still apply)",
    ):
        return None
    if force_close:
        return None  # close, but withhold the signed confirmation (no verify, no sign)

    # Cheap precondition BEFORE the billable LLM call: a bug close needs a valid --class
    # (ticket ed13 — transition_core would reject it anyway). Shared predicate, so it can't
    # drift. The old free-text --reason requirement is REMOVED; a valid --class satisfies the
    # precheck.
    if ticket_type == "bug" and not txn.bug_close_class_ok(close_class):
        allowed = ", ".join(txn.CLOSE_CLASSES)
        raise CommandError(
            "Error: closing a bug requires --class <value> — one of: "
            f"{allowed} (checked before running completion verification).",
            returncode=1,
        )

    # A duplicate / not-a-bug / escalated bug with a durable replacement relation is a
    # disposition, not a claim that this ticket's acceptance criteria were implemented.
    # Skip the completion-only checks (including file-impact and the billable verifier), but
    # only when the directional link is net-active and its counterpart is a live ticket.
    # The bug-class guard above and all structural/write-time close guards still apply.
    tracker = str(config.tracker_dir(repo_root))
    if _has_live_replacement_link(ticket_id, ticket_type, close_class, tracker):
        # ATTEST the disposition rather than withholding a signature (bug 738a): skipping the
        # verifier is right, but leaving the close unsigned made the certification path count an
        # exempt child as uncertified and withhold its parent's signature, with no honest exit.
        from rebar._commands import close_disposition

        return close_disposition.verdict(ticket_id, close_class, tracker)

    # AC-checkbox completeness precheck (DET, pre-LLM): unchecked items block close (433c).
    txn.ensure_ac_boxes_checked(ticket_id, tracker)

    # Deterministic precheck BEFORE the billable LLM call (alongside the open-children guard):
    # a ticket that records file_impact claims a concrete code change, so there MUST be a commit
    # that references it (a `rebar-ticket: <id>` trailer). If none exists, the implementation has
    # not landed and completion cannot be confirmed — fail fast (no LLM call).
    from rebar._engine_support import field_reads
    from rebar._engine_support.descendants import list_descendants
    from rebar._engine_support.resolver import resolve_ticket_id

    # Derive the code repo root from the (always-resolved) tracker rather than the raw
    # ``repo_root`` param — the CLI passes ``repo_root=None``, which would make ``git -C None``
    # fail and the check spuriously report "no referencing commit". ``os.path.dirname(tracker)``
    # is the same resolution ``transition_compute`` uses for ``repo_root_str``.
    code_root = os.path.dirname(tracker)
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
    if field_reads.file_impact(ticket_id, tracker) and not _referencing_commit_exists(
        accepted_ids, tracker, code_root
    ):
        raise CommandError(
            f"Error: cannot close {ticket_id}: it records file_impact (a code change) but no "
            f"commit references it (nor any of its descendants). Add a "
            f"'rebar-ticket: {resolved_id}' trailer to the commit "
            'that implements it, then retry (or override with --force-close="<reason>"). '
            "Completion verification cannot confirm the work landed without a referencing commit.",
            returncode=1,
        )

    try:
        from rebar import llm  # LAZY — preserves the optionality contract

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
            _msg = _failure.message_for(_outcome.resolution_class.value)
            if _msg:
                _hint = f" [{_outcome.resolution_class.value}: {_msg}]"
        # Neither a bounded-recovery failure nor a mid-run verifier failure (e.g. a step-budget
        # exhaustion after minutes of real model calls) is an unavailable runtime, so the
        # "install the extra / set a key" remedy is reserved for actual unavailability.
        _remedy = _failure.close_gate_remedy(exc, _outcome)
        raise CommandError(
            f"Error: cannot close {ticket_id}: completion verification could not run "
            f"({exc}).{_hint} {_remedy} "
            'Override with --force-close="<reason>".',
            returncode=_rc,
        ) from None

    if str(result.get("verdict", "")).upper() != "PASS":
        items = result.get("findings", []) or []
        lines = [
            f"  - {(f.get('criterion') or f.get('dimension') or '?')}: {f.get('detail', '')}"
            for f in items[:20]
        ]
        # Surface the verdict's remediation guidance (set by reconcile_verdict on every FAIL) so
        # the caller is pointed at the evidence channel — documenting proof that a requirement is
        # met as a comment on the ticket — rather than left with only the bare list of criteria.
        guidance = result.get("remediation")
        message = (
            f"Error: completion verification FAILED for {ticket_id} — {len(items)} unmet "
            "criteria; not closing.\n" + "\n".join(lines)
        )
        if guidance:
            message += "\n\n  " + guidance
        # Persist the FAIL verdict to a durable, queryable sidecar (ticket 24ec) BEFORE the
        # raise, so a completion FAIL leaves an artifact (mirroring the plan-review
        # REVIEW_RESULT sidecar) instead of vanishing. Best-effort: emit swallows its own
        # errors and returns False; it never changes the close outcome or masks the FAIL —
        # the raise below still fires unconditionally. Supply the canonical id so the record
        # lands in the resolved ticket dir, and material=None (no fingerprint is computed on
        # the FAIL path).
        from rebar.llm import completion_sidecar

        result.setdefault("ticket_id", resolved_id)
        try:
            completion_sidecar.emit(result, material=None, repo_root=repo_root)
        except Exception:  # noqa: BLE001 — defense-in-depth: persistence is best-effort observability and must NEVER mask the FAIL, even if emit itself raises
            logger.warning("completion FAIL sidecar emit raised; still blocking", exc_info=True)
        raise CommandError(message, returncode=1)
    # PASS: persist the lossless PASS record to the durable, queryable sidecar (story e7e0)
    # BEFORE any sign/early-return, so EVERY PASS close — including the local (opt-in) and the
    # certifiable=False (force-closed-descendant) unsigned paths below — leaves the positive
    # per-criterion `criteria[]` capture, mirroring the FAIL branch's emit. Best-effort:
    # persistence is observability and must NEVER affect the close outcome, so the emit is
    # wrapped and any exception logged and swallowed.
    from rebar.llm import completion_sidecar

    result.setdefault("ticket_id", resolved_id)
    try:
        completion_sidecar.emit(result, material=None, repo_root=repo_root)
    except Exception:  # noqa: BLE001 — best-effort observability sidecar; must never affect the close
        logger.warning("completion PASS sidecar emit raised; close proceeds", exc_info=True)
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
