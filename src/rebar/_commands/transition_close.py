"""The ``transition`` close-path tail (module-size seam off :mod:`.transition`).

:func:`close_ticket` is the locked-write-and-finalize half that
:func:`rebar._commands.transition.transition_compute` calls once the plan-review
gate and the parent-first cascade have run. It owns the close-path invariants in
their LOAD-BEARING order (verify -> close -> sign): the unresolved-open-children
structural guard, the completion-verification precheck (:func:`_completion_precheck`
/ :func:`_verdict_manifest`, run OUTSIDE the write lock), the locked
``txn.transition_core`` write, post-close signing of the PASS attestation, the
force-close audit comment, and compact-on-close + per-ticket scratch cleanup +
best-effort push.

This module MUST NOT import :mod:`.transition` (no back-edge): the recursion into
``transition_compute`` lives in ``_cascade_open_parent``, which stays there, so the
close tail here never calls back up.
"""

from __future__ import annotations

import logging
import os
import subprocess

from rebar import config
from rebar._commands import scratch, txn
from rebar._commands._seam import CommandError
from rebar.graph._unblock import batch_close_operations

logger = logging.getLogger(__name__)


def _referencing_commit_exists(accepted_ids: set[str], tracker: str, repo_root) -> bool:
    """True if any commit reachable from the code repo's history references ANY of
    ``accepted_ids`` via a ``rebar-ticket:`` trailer (or a leading ``<id>:`` subject token).

    Each extracted candidate is put through the SAME shared resolver the commit-ticket gate
    uses (:func:`resolve_ticket_id`), so every id form — full / short / alias / Jira key /
    prefix — matches any of the accepted ids. Resolves are cached across commits. A git
    failure (not a repo, no commits) yields ``False`` (no referencing commit found)."""
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
                resolved_cache[ref] = resolve_ticket_id(ref, tracker)
            if resolved_cache[ref] in accepted_ids:
                return True
    return False


def _compact_on_close(repo_root: str, ticket_id: str) -> None:
    """Compact-on-close: squash the event log into a SNAPSHOT (non-blocking, output
    silenced). In-process via rebar._commands.compact; --threshold=0 --skip-sync,
    commit kept."""
    import contextlib
    import io

    from rebar._commands import compact as _compact

    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            _compact.compact_cli([ticket_id, "--threshold=0", "--skip-sync"], repo_root=repo_root)
    except Exception:  # noqa: BLE001 — compact-on-close is non-blocking; broad-but-logged, the close still stands
        logger.warning(
            "compact-on-close failed for %s; continuing (close stands)", ticket_id, exc_info=True
        )


def _completion_precheck(
    ticket_id: str,
    ticket_type: str,
    cfg_root: str,
    repo_root,
    *,
    reason: str,
    force_close: str,
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
    non-force close is attempted."""
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

    # Cheap precondition BEFORE the billable LLM call: a bug close needs a valid --reason
    # (transition_core would reject it anyway). Shared predicate, so it can't drift.
    if ticket_type == "bug" and not txn.bug_close_reason_ok(reason):
        raise CommandError(
            'Error: closing a bug requires --reason starting with "Fixed:" or '
            '"Escalated to user:" (checked before running completion verification).',
            returncode=1,
        )

    # Deterministic precheck BEFORE the billable LLM call (alongside the open-children guard):
    # a ticket that records file_impact claims a concrete code change, so there MUST be a commit
    # that references it (a `rebar-ticket: <id>` trailer). If none exists, the implementation has
    # not landed and completion cannot be confirmed — fail fast (no LLM call).
    from rebar._engine_support import field_reads
    from rebar._engine_support.descendants import list_descendants
    from rebar._engine_support.resolver import resolve_ticket_id

    tracker = str(config.tracker_dir(repo_root))
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
            ticket_id, graph=False, source="attested", ref="HEAD", fetch=False, repo_root=repo_root
        )
    except Exception as exc:  # noqa: BLE001 — missing extra/key OR any verifier failure -> fail-closed (re-raise CommandError)
        # Shape B (story blackbear): thread the classifier disposition mamba/preflight attached
        # to the raised LLM error through to the process exit code. A retryable outage (429/5xx)
        # → exit 11 ("transient — retry"), else the existing fail-closed exit 1. CommandError
        # already carries `returncode` and transition.py returns it, so exit 11 propagates with
        # no plumbing change. The sanitized diagnostic is also written to the session log.
        from rebar.llm import failure as _failure

        _outcome = _failure.outcome_of(exc)
        _failure.log_degrade(_outcome, gate="completion-verify", ticket_id=ticket_id)
        _rc = 11 if (_outcome and _outcome.retryable) else 1
        _hint = ""
        if _outcome is not None:
            _msg = _failure.message_for(_outcome.resolution_class.value)
            if _msg:
                _hint = f" [{_outcome.resolution_class.value}: {_msg}]"
        raise CommandError(
            f"Error: cannot close {ticket_id}: completion verification could not run "
            f"({exc}).{_hint} "
            "The completion-verification gate is enabled "
            "(verify.require_completion_verification_for_close); install the 'agents' extra and "
            'set a model API key, or override with --force-close="<reason>".',
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
    # A closed-but-uncertified (force-closed) descendant WITHHOLDS certification: the parent's own
    # criteria PASSED (it may close), but certification propagates — an unattested descendant leaves
    # the subtree unattested, so we close WITHOUT signing (the same unsigned-close path as
    # --force-close). The closed-without-signature ticket is the durable "not fully certified"
    # signal; re-close the uncertified descendant through the gate to certify, then re-close here.
    if result.get("certifiable") is False:
        return None
    return result


def _is_full_sha(s: object) -> bool:
    """True for a full 40-char lowercase-hex git SHA (the shape ``head_sha`` and an attested
    ``verified-at-sha`` both take)."""
    return isinstance(s, str) and len(s) == 40 and all(c in "0123456789abcdef" for c in s.lower())


def _material_drifted(verified_sha: object, fresh_sha: object) -> bool:
    """Whether the code MATERIALLY drifted between verify and sign (story blackbear): true only
    when BOTH are real full SHAs that differ. A non-SHA / absent ``verified-at-sha`` (an
    unattested/local verdict, or a test's synthetic marker) is not comparable → NOT drift, so the
    normal sign-on-PASS path is preserved. Keeps the recheck sound in attested mode, where the
    manifest's verified-at-sha is HEAD-at-verify and equals a fresh HEAD read unless HEAD moved."""
    return _is_full_sha(verified_sha) and _is_full_sha(fresh_sha) and verified_sha != fresh_sha


def _verdict_manifest(result: dict, ticket_id: str, repo_root=None) -> list[str]:
    """Deterministic manifest (non-empty strings) of the verified PASS verdict, for signing.

    The signature binds ``(ticket_id, manifest)``; the key fingerprint + head_sha on the record
    provide attribution + freshness. Findings are failures-only, so a PASS has no per-criterion
    list to itemize — the minimal core IS the attestation. Deterministic (no timestamps) so
    re-signing the same verified state is reproducible.

    On an attested verdict the manifest carries a ``verified-at-sha:<sha>`` step (epic
    raze-vet-ditch S4) binding WHICH immutable commit was verified into the signed bytes —
    via the manifest channel, so no ``_canonical_payload``/``PAYLOAD_VERSION`` change and no
    prior signature is invalidated.

    It also records the ticket's ``material: <fingerprint>`` (epic dark-acme-lumen) — the SAME
    fingerprint plan-review signs (description/AC/file_impact/children) — so completion
    validity-on-read can detect a material edit made after this verdict was signed, symmetric
    with the plan-review claim gate. Omitted if the fingerprint can't be computed (then the
    material check is simply skipped on read)."""
    from rebar import signing as _signing

    manifest = [
        "completion-verifier: PASS",
        f"ticket: {ticket_id}",
        f"model: {result.get('model') or 'n/a'}",
        f"runner: {result.get('runner') or 'n/a'}",
        # Which rebar gate code produced this attestation (audit/provenance, epic
        # jira-reb-596), symmetric with the plan-review manifest. NEVER read on validity.
        _signing.rebar_version_step(_signing.gate_code_version()),
    ]
    try:
        from rebar.llm.plan_review.attest import current_material_fingerprint

        material = current_material_fingerprint(ticket_id, repo_root=repo_root)
    except Exception:  # noqa: BLE001 — fingerprint is best-effort; absence just skips the material check on read
        material = None
    if material:
        manifest.append(f"material: {material}")
    sha = result.get("verified_at_sha")
    if sha:
        manifest.append(_signing.verified_at_sha_step(sha))
    return manifest


def sign_completion_verdict(result: dict, ticket_id: str, repo_root=None) -> dict:
    """The completion-verifier PRODUCER STEP: build the deterministic PASS manifest for
    ``result`` (via :func:`_verdict_manifest`) and mint the ``completion-verifier`` op-cert
    through the signing seam (:func:`rebar.signing.sign_manifest`), appending the SIGNATURE
    event to the store under ``repo_root``. Returns the signed record.

    Extracted from the close gate so BOTH producers mint the completion op-cert the SAME way
    (story ee0b): the close path here and the trusted op-cert gate service's worker on a PASS
    verdict. The service points ``REBAR_OPCERT_KEY_PATH`` / ``REBAR_OPCERT_ENV_ID`` at the
    provisioned environment key before calling this, so the SEAM signs once with that key — the
    caller never signs bespokely. Raises :class:`rebar.signing.SigningError` on the degrade path
    (OpenSSH < 8.9 / unwritable tracker), which the caller records as a closed-/completed-without
    -signature outcome."""
    from rebar import signing as _signing

    manifest = _verdict_manifest(result, ticket_id, repo_root)
    return _signing.sign_manifest(
        ticket_id, manifest, kind="completion-verifier", repo_root=repo_root
    )


def close_ticket(
    ticket_id: str,
    current_status: str,
    target_status: str,
    tracker: str,
    repo_root_str: str,
    repo_root,
    *,
    reason: str,
    force_close: str,
) -> dict:
    """Perform the locked write and its post-processing tail; return the transition
    result ``{ticket_id, from, to, newly_unblocked, noop}``.

    For a CLOSE this owns the close-path invariants in their LOAD-BEARING order
    (verify -> close -> sign): the unresolved-open-children structural guard, the
    completion-verification precheck (runs the verifier OUTSIDE the write lock, blocks
    fail-closed on FAIL / unavailable-LLM, returns the manifest to sign on PASS), the
    locked write, then — only AFTER a confirmed close — signing the PASS attestation,
    the force-close audit comment, and compact-on-close + per-ticket scratch cleanup. A
    non-close transition falls through to just the locked write. Both paths end with a
    best-effort push (transition_core commits inline, not via write_and_push)."""
    # Open-children guard + newly_unblocked (one batch pass), only on close.
    newly_unblocked: list[str] = []
    if target_status == "closed":
        batch = batch_close_operations(ticket_ids=[ticket_id], tracker_dir=tracker)
        open_children = batch["open_children"]
        newly_unblocked = batch["newly_unblocked"]
        if open_children:
            count = len(open_children)
            # The child-closure relationship is a STRUCTURAL INTEGRITY invariant (a parent is
            # not complete while its children are open), NOT a quality gate — so it is enforced
            # UNCONDITIONALLY: neither --force (which bypasses the plan-review gate) nor
            # --force-close (which bypasses the signature/completion-verifier requirement) can
            # close a parent over open children. Resolve/close the children first, or detach
            # (re-home) them, then close the parent.
            raise CommandError(
                f"Error: cannot close ticket '{ticket_id}' while it has {count} unresolved "
                "(non-closed) child ticket(s) — the child-closure invariant cannot be bypassed "
                "(not with --force or --force-close). Close or resolve these children first, or "
                "detach them (re-home), then close:\n" + "\n".join(open_children),
                returncode=1,
            )

    # Completion-verification close gate (opt-in; runs OUTSIDE the write lock since an LLM
    # call must not serialize all writes). Ordering is verify -> close -> sign: the precheck
    # runs the verifier and blocks (fail-closed) on FAIL / unavailable-LLM; on PASS it returns
    # the manifest to sign AFTER a confirmed close (so a failed/raced close never leaves an
    # orphan "certified" signature on an unclosed ticket). force_close skips both.
    #
    # `idea → closed` is a REJECT/DROP, not a completion: closing an undesigned idea
    # means "we won't pursue this," so there is nothing built to verify or attest.
    # Running the completion precheck (verifier + file-impact→referencing-commit +
    # reason-guard copy) would nonsensically BLOCK the rejection, so we skip it entirely
    # when the from-status is `idea`. The open-children structural guard above still
    # ran unconditionally (integrity, not completion), so an idea parent over
    # non-closed children is still refused.
    verified_result = None
    if target_status == "closed" and current_status != "idea":
        from rebar.reducer import reduce_ticket as _reduce

        ticket_type = (_reduce(os.path.join(tracker, ticket_id)) or {}).get("ticket_type", "")
        verified_result = _completion_precheck(
            ticket_id, ticket_type, repo_root_str, repo_root, reason=reason, force_close=force_close
        )

    from rebar._commands import _seam

    env_id = _seam.env_id(config.tracker_dir(repo_root))
    author = _seam.author("Unknown")
    # Locked write (exit 10 on optimistic-concurrency mismatch). transition_core stamps
    # attribution + signs the STATUS event via the shared finalize seam (bug 0ba4), given repo_root.
    txn.transition_core(
        tracker,
        ticket_id,
        current_status,
        target_status,
        env_id=env_id,
        author=author,
        close_reason=reason,
        force_close_reason=force_close,
        repo_root=repo_root,
    )

    # PASS attestation: sign the verified verdict AFTER the close is confirmed. A crash in this
    # (two-local-commit) window leaves closed-without-signature — the conservative direction
    # (reads as "bypassed", never a false "validated"). Errors surface: we WANT a hard signal if
    # the trustworthy record can't be written.
    if target_status == "closed" and verified_result is not None:
        import sys

        from rebar import signing as _signing

        # Pre-sign fingerprint recheck (story blackbear): the verifier ran OUTSIDE the write lock,
        # and transport retries + timeouts widen the verify -> close -> sign window. The close gate
        # verifies an attested snapshot of HEAD, so the manifest's `verified-at-sha:` IS the HEAD
        # SHA at verify time; re-read HEAD now and, if it MOVED, the code drifted under us — do NOT
        # attest stale state. The ticket already closed (the transition committed above), so this is
        # the same closed-without-signature outcome as --force-close: warn on stderr and skip
        # signing (the close still succeeds, exit 0). Re-close to certify against the current tree.
        _manifest = _verdict_manifest(verified_result, ticket_id, repo_root)
        _verified_sha = _signing.verified_at_sha_from_manifest(_manifest)
        _fresh_sha = _signing.head_sha(config.repo_root(repo_root))
        if _material_drifted(_verified_sha, _fresh_sha):
            sys.stderr.write(
                f"Warning: closed {ticket_id} WITHOUT a completion signature — the code drifted "
                f"between verify ({str(_verified_sha)[:12]}) and sign ({str(_fresh_sha)[:12]}); "
                "not attesting stale state. Re-close to certify against the current tree.\n"
            )
        else:
            try:
                # The shared producer step (story ee0b) — same seam call the trusted op-cert gate
                # service uses on a PASS verdict, so both producers mint the cert identically.
                sign_completion_verdict(verified_result, ticket_id, repo_root)
            except _signing.SigningError as exc:
                # DEGRADE, never wedge (story 8d8e): op-cert signing needs ssh-keygen (OpenSSH
                # >= 8.9) and a writable tracker. When neither can produce a key the close ALREADY
                # committed, so this is the same closed-without-signature outcome as --force-close:
                # warn and skip signing (exit 0). Re-close once OpenSSH >= 8.9 is installed.
                sys.stderr.write(
                    f"Warning: closed {ticket_id} WITHOUT a completion signature — {exc.message} "
                    "Re-close once signing is available to certify against the current tree.\n"
                )

    # Reopen invalidation is NO LONGER a write-time mutation (epic dark-acme-lumen): attestation
    # records are immutable, and a reopen is detected on READ via state["last_reopened_at"] +
    # compute_validity (a completion/plan-review attestation signed before the reopen reads as
    # not-valid). This replaces the former retire_attested_pin clear, and — unlike it — does not
    # destroy the kind-keyed attestations a reopened ticket still carries.

    # Force-close audit comment (best-effort, silenced — matches bash || true).
    if target_status == "closed" and force_close:
        session = _resolve_session(tracker)
        body = (
            "FORCE_CLOSE: close gate(s) bypassed by user approval — no completion/signature "
            f'attestation was signed. Reason: "{force_close}". Session: {session}.'
        )
        try:
            from rebar._commands import leaf

            leaf.comment(ticket_id, body, repo_root=repo_root)
        except Exception:  # noqa: BLE001 — best-effort force-close audit comment; broad-but-logged, close proceeds
            logger.warning(
                "could not write FORCE_CLOSE audit comment on %s; continuing",
                ticket_id,
                exc_info=True,
            )

    if target_status == "closed":
        _compact_on_close(repo_root_str, ticket_id)
        scratch.cleanup_for_ticket(repo_root_str, ticket_id)

    # The STATUS (and compact-on-close SNAPSHOT) commits are now in the local
    # tickets branch but unpushed — txn.transition_core commits inline and does not
    # go through write_and_push. Trigger the same best-effort push so a trailing
    # transition (the last write of a session) isn't stranded (bug prone-octet-cheek).
    from rebar._store import push

    push.push_after_commit(tracker)

    return {
        "ticket_id": ticket_id,
        "from": current_status,
        "to": target_status,
        "newly_unblocked": newly_unblocked,
        "noop": False,
    }


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


def _short_head(tracker: str) -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except Exception:  # noqa: BLE001 — short-HEAD is a session-id nicety; fall open to "" if git is unavailable
        return ""
