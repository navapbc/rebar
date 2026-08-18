"""The ``transition`` close-path tail (module-size seam off :mod:`.transition`).

:func:`close_ticket` is the locked-write-and-finalize half that
:func:`rebar._commands.transition.transition_compute` calls once the plan-review
gate and the parent-first cascade have run. It owns the close-path invariants in
their LOAD-BEARING order (verify -> close -> sign): the unresolved-open-children
structural guard, the completion-verification precheck (:func:`_completion_precheck`
/ :func:`_verdict_manifest`, run OUTSIDE the write lock), the locked
``txn.transition_core`` write, post-close signing of the PASS attestation, the
force-close audit comment, and per-ticket scratch cleanup + best-effort push.

This module MUST NOT import :mod:`.transition` (no back-edge): the recursion into
``transition_compute`` lives in ``_cascade_open_parent``, which stays there, so the
close tail here never calls back up.
"""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Mapping
from typing import Any

from rebar import config
from rebar._commands import scratch, txn
from rebar._commands._seam import CommandError

# The completion-precheck cluster lives in close_precheck (ticket 74a3: module-size split
# along the existing call-graph seam). Re-imported here so the historical seam names —
# `transition_close._completion_precheck` (a documented monkeypatch target) and
# `transition_close._referencing_commit_exists` — keep working unchanged.
from rebar._commands.close_precheck import (  # noqa: F401 — re-exported compatibility seam
    _completion_precheck,
    _referencing_commit_exists,
)
from rebar.graph._unblock import batch_close_operations

logger = logging.getLogger(__name__)


_PLAN_REVIEW_CLOSE_TYPES = frozenset({"task", "story", "epic"})


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
    fingerprint plan-review signs (ticket_id/description/file_impact/children, and
    file_impact_scope when it is an explicit ``none``) — so completion
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
    if result.get("disposition"):
        from rebar._commands import close_disposition

        return close_disposition.decorate_manifest(manifest, result)
    return manifest


def sign_completion_verdict(result: dict, ticket_id: str, repo_root=None, *, signer=None) -> dict:
    """The completion-verifier PRODUCER STEP: build the deterministic PASS manifest for
    ``result`` (via :func:`_verdict_manifest`) and mint the ``completion-verifier`` op-cert
    through the signing seam (:func:`rebar.signing.sign_manifest`), appending the SIGNATURE
    event to the store under ``repo_root``. Returns the signed record.

    Extracted from the close gate so BOTH producers mint the completion op-cert the SAME way
    (story ee0b): the close path here and the trusted op-cert gate service's worker on a PASS
    verdict. ``signer`` (story 6f14): an OPTIONAL startup-composed op-cert binding; when the
    service passes one, the SEAM signs from that binding's key + principal instead of the process
    env — the caller never signs bespokely. Omitting it (the developer-local CLI close) keeps the
    exact env/genesis behavior. Raises :class:`rebar.signing.SigningError` on the degrade path
    (OpenSSH < 8.9 / unwritable tracker), which the caller records as a closed-/completed-without
    -signature outcome."""
    from rebar import signing as _signing

    manifest = _verdict_manifest(result, ticket_id, repo_root)
    return _signing.sign_manifest(
        ticket_id, manifest, kind="completion-verifier", repo_root=repo_root, signer=signer
    )


def _apply_caused_by(
    ticket_id: str, caused_by: str, tracker: str, repo_root_str: str, repo_root
) -> None:
    """Best-effort ``caused_by`` link on a BUG close (ticket 555e).

    Only bugs carry the blame-hunt semantics. An explicit ``caused_by`` id wins; otherwise
    :func:`rebar.metrics.blame.derive_caused_by` auto-derives a single dominant culprit from
    git blame. If a culprit is resolved, the edge is written via the lower-level
    :func:`rebar.graph._links._write_link_event` (which bypasses the closed-source + cycle
    guards ``add_dependency`` enforces — the source bug is already ``closed`` here). EVERYTHING
    is wrapped so a resolve/write failure NEVER blocks or fails the close."""
    try:
        from rebar.reducer import reduce_ticket as _reduce

        state = _reduce(os.path.join(tracker, ticket_id)) or {}
        if state.get("ticket_type") != "bug":
            return

        culprit = caused_by.strip() if caused_by else None
        if culprit:
            from rebar._engine_support.resolver import resolve_ticket_id

            culprit = resolve_ticket_id(culprit, tracker) or culprit
        else:
            from rebar.metrics import blame

            culprit = blame.derive_caused_by(ticket_id, repo_root_str, tracker)
        if not culprit or culprit == ticket_id:
            return

        from rebar.graph._links import _write_link_event

        _write_link_event(ticket_id, culprit, "caused_by", str(config.tracker_dir(repo_root)))
    except Exception:
        logger.warning(
            "best-effort caused_by link on close of %s failed; close stands",
            ticket_id,
            exc_info=True,
        )


def _sign_completion_and_report(
    verified_result: dict, ticket_id: str, repo_root, ref: str | None
) -> dict:
    """Sign the completion verdict for a just-closed ticket and report what became of it.

    Extracted from :func:`close_ticket` (bug silvern-dewy-damselfly): this tail is a decision
    of its own — drift refusal vs sign vs sign failure — and inlining it pushed the caller past
    the complexity ratchet.

    Returns the ``completion_signature`` block the close payload carries:
    ``{"signed": bool, "cause": str, "error": str}`` with ``cause`` one of ``signed`` /
    ``material_drifted`` / ``sign_failed``. NEVER raises: the close has ALREADY committed by the
    time this runs, so a failure here can only be reported, never undone. Warnings go to stderr
    and the caller's exit code is unaffected.
    """
    import sys

    from rebar import signing as _signing

    # Pre-sign fingerprint recheck (story blackbear): the verifier ran OUTSIDE the write lock,
    # and transport retries + timeouts widen the verify -> close -> sign window. The close gate
    # verifies an attested snapshot of HEAD, so the manifest's `verified-at-sha:` IS the HEAD
    # SHA at verify time; re-read HEAD now and, if it MOVED, the code drifted under us — do NOT
    # attest stale state. The ticket already closed (the transition committed above), so this is
    # the same closed-without-signature outcome as --force: warn on stderr and skip
    # signing (the close still succeeds, exit 0). Re-close to certify against the current tree.
    _manifest = _verdict_manifest(verified_result, ticket_id, repo_root)
    _verified_sha = _signing.verified_at_sha_from_manifest(_manifest)
    # bug 80af: a --ref-targeted close verifies (and must sign against) THAT ref, not HEAD.
    # So the drift guard must resolve the SAME ref for the fresh sha — otherwise a stacked-story
    # close (--ref=<story-sha> while the worktree sits at the epic tip) would compare the story
    # sha against the tip HEAD and be spuriously treated as drifted, landing UNSIGNED. For a
    # concrete commit the tree is immutable, so resolving the same ref makes the check a stable
    # no-op and a legitimately-targeted close lands SIGNED.
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
    """Fire the operation-linked compaction trigger on a close (story gaudy-gangrenous-basilisk).

    Compaction is no longer PERFORMED on the close path — holding the store write lock across
    the fold is what starved every concurrent writer for up to 13m53s. But it still has to
    happen somewhere for an adopter with no CI and no cron, who has no scheduled sweep to fall
    back on: compaction must work without either, which is why the original design was linked
    to an operation. So the close TRIGGERS it instead of doing it. By the time this runs the
    locked write has released the store lock AND the best-effort push has completed, the
    decision costs two O(1) checks, and any real folding goes to a detached worker — the
    session holds nothing and waits for nothing.

    A GUARD FUNCTION, not an inline branch, on purpose: :func:`close_ticket` sits at its
    recorded ceiling in ``.github/complexity-baseline.json``, which is shrink-only, so the
    decision point lives here and the call site stays unconditional (the same reason
    :func:`rebar._commands.close_precheck._ensure_duplicate_close_is_linked` is a function).
    """
    if target_status != "closed":
        return
    from rebar._commands import compact_trigger

    compact_trigger.maybe_compact(tracker, ticket_id, repo_root=repo_root_str)


def _hint_disposition_alternative(close_class: str) -> None:
    """On a force close shaped like an administrative disposition, point at the truthful exit.

    Store mining behind ticket fc20 found the single largest FORCE_CLOSE class to be
    administrative closes (duplicate/obsolete/superseded/wontfix) forced only because the
    completion verifier can never PASS them — a sanctioned, attested path now exists, so the
    force path SAYS SO. "Administrative-shaped" = the close carries an administrative
    ``--class`` already (the force was likely unnecessary) or no class at all (the operator may
    not know the vocabulary). A bug-only class (e.g. ``regression``) earns no hint — that force
    is about the verifier's verdict, not a missing disposition path. Best-effort stderr,
    never affecting the close; a side-effecting guard function so ``close_ticket`` stays at its
    shrink-only complexity ceiling."""
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
    """Perform the locked write and its post-processing tail; return the transition
    result ``{ticket_id, from, to, newly_unblocked, noop}``.

    For a CLOSE this owns the close-path invariants in their LOAD-BEARING order
    (verify -> close -> sign): the unresolved-open-children structural guard, the
    completion-verification precheck (runs the verifier OUTSIDE the write lock, blocks
    fail-closed on FAIL / unavailable-LLM, returns the manifest to sign on PASS), the
    locked write, then — only AFTER a confirmed close — signing the PASS attestation,
    the force-close audit comment, and per-ticket scratch cleanup. A
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
            # UNCONDITIONALLY: --force (which bypasses any enabled start-work gate AND the
            # signature/completion-verifier requirement) cannot
            # close a parent over open children. Resolve/close the children first, or detach
            # (re-home) them, then close the parent.
            raise CommandError(
                f"Error: cannot close ticket '{ticket_id}' while it has {count} unresolved "
                "(non-closed) child ticket(s) — the child-closure invariant cannot be bypassed "
                "(not even with --force). Close or resolve these children first, or "
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
    # Machine-readable record of what became of the completion signature (bug
    # silvern-dewy-damselfly). The close COMMITS before signing is attempted, so a signing
    # failure leaves a ticket closed-without-signature while the command still succeeds. Left
    # unreported, the only evidence was a stderr line whose text described the CLOSE as having
    # failed. Consumers branch on `cause` without parsing English; see the vocabulary below.
    # Stays None on any path where no completion signature is in play (a non-close transition,
    # and `idea -> closed`), and is omitted from the payload entirely in that case.
    completion_signature: dict[str, object] | None = None
    verified_result = None
    plan_review_recheck = None
    if target_status == "closed" and current_status != "idea":
        if ref is None:
            # 4de6: pin the DEFAULT completion target to ONE immutable sha for the whole
            # verify+sign unit. Resolving HEAD lazily at verify time AND again at the pre-sign
            # drift guard let a benign concurrent commit in that window split them (verify saw A,
            # sign saw B), refusing the signature and closing unsigned. Resolve HEAD once here and
            # thread the sha through _completion_precheck (verify) AND the drift guard (which
            # already resolves a non-"HEAD" ref), so both bind the same commit. Best-effort: if
            # HEAD can't resolve, fall back to the prior lazy "HEAD" behavior (ref stays None) —
            # never worse than before.
            try:
                from rebar._snapshot.repo_snapshot import resolve_ref

                ref = resolve_ref("HEAD", str(config.repo_root(repo_root)), fetch=False)
            except Exception:  # noqa: BLE001 — resolution is best-effort; lazy HEAD is the fallback
                ref = None
        from rebar.reducer import reduce_ticket as _reduce

        ticket_state = _reduce(os.path.join(tracker, ticket_id)) or {}
        ticket_type = ticket_state.get("ticket_type", "")
        if not force_close and ticket_type in _PLAN_REVIEW_CLOSE_TYPES:
            from rebar._commands import gates

            check = gates.close_plan_review_gate_check(ticket_id, ticket_state, repo_root=repo_root)
            if not check.get("ok"):
                _raise_plan_review_close_gate_error(ticket_id, check)
            if check.get("verdict") != "disabled":

                def plan_review_recheck(locked_state: Mapping[str, Any]) -> None:
                    locked_check = gates.close_plan_review_gate_check(
                        ticket_id, locked_state, repo_root=repo_root
                    )
                    if not locked_check.get("ok"):
                        _raise_plan_review_close_gate_error(ticket_id, locked_check)

        verified_result = _completion_precheck(
            ticket_id,
            ticket_type,
            repo_root_str,
            repo_root,
            reason=reason,
            force_close=force_close,
            close_class=close_class,
            ref=ref,
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
        close_class=close_class,
        close_reason=close_reason,
        force_reason=force_close,
        repo_root=repo_root,
        pre_status_check=plan_review_recheck,
    )

    # PASS attestation: sign the verified verdict AFTER the close is confirmed. A crash in this
    # (two-local-commit) window leaves closed-without-signature — the conservative direction
    # (reads as "bypassed", never a false "validated"). Errors surface: we WANT a hard signal if
    # the trustworthy record can't be written.
    if target_status == "closed" and verified_result is not None:
        completion_signature = _sign_completion_and_report(
            verified_result, ticket_id, repo_root, ref
        )

    # Blame-Hunt Advisory (ticket 555e): on a BUG close, draw a best-effort caused_by link
    # from the (now-closed) bug to the culprit change/ticket. An explicit --caused-by <id>
    # override wins; otherwise git-blame auto-derives a single dominant culprit. Runs AFTER the
    # close committed, so the link SOURCE (the bug) is `closed` — add_dependency REJECTS a closed
    # source, so we write the edge via the lower-level _write_link_event, which bypasses the
    # closed-source + cycle guards (a non-blocking, non-cycle relation on a closed source needs
    # neither). Best-effort: any resolve/write failure is swallowed and NEVER blocks the close.
    if target_status == "closed":
        _apply_caused_by(ticket_id, caused_by, tracker, repo_root_str, repo_root)

    # Reopen invalidation is NO LONGER a write-time mutation (epic dark-acme-lumen): attestation
    # records are immutable, and a reopen is detected on READ via state["last_reopened_at"] +
    # compute_validity (a completion/plan-review attestation signed before the reopen reads as
    # not-valid). This replaces the former retire_attested_pin clear, and — unlike it — does not
    # destroy the kind-keyed attestations a reopened ticket still carries.

    # Force-close audit comment (best-effort, silenced — matches bash || true).
    if target_status == "closed" and force_close:
        # A deliberate bypass, not a failure — but still closed-without-signature, so it gets
        # its own cause rather than being reported as if a signature had been attempted.
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

    # Compaction is NOT run here (bug choosy-arthrodic-barbet). It used to be, and it was the
    # store's longest lock holder BY FAR: `compact_txn._compact_locked` takes the ONE store
    # write lock and holds it for the whole fold — read, reduce, authorship ledger, snapshot
    # write, retire renames, and the git add/commit, whose nested `_store_git_op_lock` wait and
    # index-lock retry budget stack INSIDE that hold with no aggregate ceiling. Measured on the
    # rebar store: a single close held the lock for 13m53s, and three others the same hour held
    # ~2.5min each, starving every concurrent writer. The 7084 stand-aside probe could not help,
    # because the closing process had released the lock seconds earlier so the store always read
    # free to its own probe.
    #
    # Moving it is safe because compaction is OPTIONAL housekeeping, never a correctness step:
    # an unfolded event log is completely valid and the reducer replays it. `rebar compact
    # <id>` still folds on demand; a scheduled sweep folds the store where CI exists; and this
    # close still TRIGGERS a fold — see `_trigger_compaction` below — it just hands the work to
    # a detached worker after the lock is released instead of doing it inline. What changed is
    # WHO holds the lock and WHEN, not whether a close leads to compaction.
    if target_status == "closed":
        scratch.cleanup_for_ticket(repo_root_str, ticket_id)

    # The STATUS commit is now in the local tickets branch but unpushed —
    # txn.transition_core commits inline and does not go through write_and_push. Trigger
    # the same best-effort push so a trailing transition (the last write of a session)
    # isn't stranded (bug prone-octet-cheek).
    from rebar._store import push

    push.push_after_commit(tracker)

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
        ).stdout.strip()
    except Exception:  # noqa: BLE001 — short-HEAD is a session-id nicety; fall open to "" if git is unavailable
        return ""
