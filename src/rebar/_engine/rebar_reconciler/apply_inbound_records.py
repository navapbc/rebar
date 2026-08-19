#!/usr/bin/env python3
"""Inbound rebar-facade mutation: the assignee identity mint and the link graph.

Once the home of every inbound create/update phase helper (extracted from
``apply_inbound.py``, ticket 090a). Those helpers moved on to
``apply_inbound_events.py`` when this module reached one line of headroom under the
800-LOC hard cap (ticket 6f51-f8a4-b4fb-450c); the split followed the seam the call
graph already drew, so what remains here is one concern rather than a remainder.

What stays: the two clusters that mutate rebar's SHARED stores through the public
``rebar`` facade rather than writing ticket event files —

* the inbound assignee identity mint (``rebar.ensure_identity_for``), and
* the inbound link graph (``rebar.link`` / ``rebar.unlink``) with the durable
  impossible-link and peer-confirmation sidecar stores that decide which records are
  worth attempting and which removals are safe to honour.

Both are best-effort and fail-open: every failure is logged through this module's
``logger`` and the inbound apply continues. Their only shared module-level dependency
with the event writers was ``inbound_translate._extract_name`` (the identity mint's
display-name read), which is why it is still imported below; every other dependency
here is a function-local import, as it already was.

``apply_inbound_events`` is imported at module level for ONE documented back-compat
re-export; that module never imports this one at module level, so the direction stays
one-way.
"""

from __future__ import annotations

import logging

# Back-compat: ``_inbound_update_write_edit_event`` moved to ``apply_inbound_events``
# with the rest of the event writers, but it is still driven as
# ``apply_inbound_records._inbound_update_write_edit_event`` (it is the phase that calls
# the identity mint this module owns), so the name keeps resolving here.
from rebar_reconciler.apply_inbound_events import (  # noqa: F401
    _inbound_update_write_edit_event,
)
from rebar_reconciler.inbound_translate import _extract_name

logger = logging.getLogger(__name__)


def _identity_provider_for(backend) -> str:
    """The store-facing identity provider / creation channel for a backend (bug 5f48).

    The Backend port's ``vendor`` is per-DEPLOYMENT (Cloud and Data Center carry
    different vendor strings — see ``adapters/jira*/backend.py``), but the store's
    identity provider and its ``CREATION_CHANNELS`` vocabulary
    (``rebar.reducer._version``) are per-FAMILY: only the family name is a member.
    Using the raw vendor as the creation channel made every DC mint raise
    ``ValueError`` inside the best-effort ``except`` below — a silently swallowed
    no-op. Widening ``CREATION_CHANNELS`` was rejected deliberately: it would fork the
    store vocabulary per deployment and force a migration, and Cloud/DC identities must
    share ONE provider namespace so a human assigned on one deployment still resolves
    on the other.

    The family is DECLARED BY THE BACKEND (``identity_family``), not derived here. Two
    alternatives were rejected. A string transform (family = everything before the
    first ``-``) keeps this module vendor-neutral but fails OPEN: a future backend
    registered as ``import-foo`` would silently collapse onto ``import`` — a real
    member of ``CREATION_CHANNELS`` — and mis-stamp the provenance of every identity it
    minted, with nothing raising. A literal lookup table here fails closed but puts
    vendor names back into a CORE module, which is exactly what
    ``test_backend_neutrality.py`` forbids. Asking the backend keeps this module free of
    vendor literals AND makes each backend state its own family explicitly.

    Falls back to ``vendor`` when a backend declares no family, so an unrecognized
    backend is rejected by :func:`validate_creation_channel` rather than quietly
    mis-attributed — the same fail-closed posture as before.
    """
    from rebar.reducer._version import validate_creation_channel

    return validate_creation_channel(getattr(backend, "identity_family", None) or backend.vendor)


def _ensure_inbound_assignee_identity(assignee, repo_root) -> None:
    """Best-effort: mint/reuse a placeholder identity for an inbound Jira assignee
    (2f13). When the assignee field carries an external id, resolve it through
    :func:`rebar.ensure_identity_for` (provider = the backend vendor's Jira-family
    name, keyed on that external id) so an unmapped inbound user gets a ghost
    identity that a later outbound pass can key on.

    The external id is deployment-shaped (story b4e3's ``UserIdentityModel``, see
    ``adapters/jira_family/identity_model.py``): Jira Cloud identifies users by the
    opaque ``accountId`` (username/userkey were removed for GDPR), while Data Center
    has NO accountId at all and identifies users by ``name``. This function is
    handed the RAW Jira user object, so it accepts either — ``accountId`` FIRST so
    Cloud's behavior is byte-for-byte unchanged, falling back to DC's ``name``
    (bug 5f48: the accountId-only guard meant DC never minted an identity).

    ADDITIVE + best-effort: this NEVER changes the human-readable name extraction and
    NEVER fails the inbound apply — an assignee carrying neither key returns early
    without minting, and any mint failure is swallowed so the apply continues with
    the name-only behavior."""
    if not isinstance(assignee, dict):
        return
    external_id = ""
    # Cloud's key first, then DC's, then ``account_id`` — the CANONICAL
    # ``assignee_identity`` shape the inbound differ forwards on an UPDATE (bug 8d68).
    # ``_identity_of:154-179`` already resolved accountId-or-DC-username into that one key,
    # with accountId winning, so reading it here inherits 5f48's precedence rather than
    # restating it. The dict guard above is deliberately NOT relaxed to accept a bare string:
    # on DC the scalar assignee happens to be the username, but on Cloud it is the DISPLAY
    # NAME, and minting on it would register every Cloud user under the wrong external id.
    for key in ("accountId", "name", "account_id"):
        candidate = assignee.get(key)
        if isinstance(candidate, str) and candidate.strip():
            external_id = candidate
            break
    if not external_id:
        return
    display_name = _extract_name(assignee) or assignee.get("display") or ""
    try:
        import rebar
        from rebar.config import compose_config
        from rebar_reconciler._backend_registry import select_backend

        # S4: provider identity + creation channel come from the configured backend
        # (routes through the Backend port), not a hard-coded provider literal — asked
        # for its FAMILY, since the store vocabulary is per-family, not per-deployment
        # (see _identity_provider_for, bug 5f48).
        provider = _identity_provider_for(select_backend(compose_config()))

        rebar.ensure_identity_for(
            provider,
            external_id,
            display_name or external_id,
            repo_root=repo_root,
            creation_channel=provider,
        )
    except Exception:
        logger.debug(
            "inbound: could not ensure identity for jira user %r", external_id, exc_info=True
        )


def _inbound_unlink_one(local_id, target_local_id, relation, repo_root, confirm_store=None) -> bool:
    """Mirror a peer link DELETION locally via a RELATION-SCOPED ``rebar.unlink``.

    **G5, relation-scoped (tickets 2b16 → e39f).** Links are written keyed on
    ``(target_id, relation)`` (``graph/_links.add_dependency``), so a pair can hold more than
    one active relation. The original G5 guard could only DECLINE when the pair's most-recent
    net-active relation was not the one whose peer link vanished — ``rebar.unlink`` was
    pair-scoped, so unlinking would have removed a link the peer still carries. That decline
    was permanent: the removal re-emitted and re-declined every pass, so a double-related
    pair never converged (bug e39f). ``rebar.unlink`` now accepts the relation, so we remove
    exactly the mirrored ``(target, relation)`` link and the pair converges. The G5 safety
    invariant is unchanged and now structural: a removal can never touch a relation the
    record did not name, and when the named relation has NO net-active local link the
    removal is a logged no-op. We still ask ``_get_link_info`` (the very function
    ``unlink_core`` will consult, with the same relation narrowing) before writing, so the
    no-op case is detected without an exception and the prediction and the subsequent write
    can never disagree.

    Returns True only when an UNLINK was actually written, so the caller's count feeds the
    silent-no-op canary (``apply_handlers.py``) truthfully. Every skip is LOGGED: this defect
    class has been silent every time, so a skip that reports nothing is not acceptable.
    """
    import rebar
    from rebar._commands._seam import tracker_dir
    from rebar._commands.unlink import _get_link_info

    # G3 DISCRIMINATOR (epic a4bd): "managed" proves ownership, NOT that the peer ever saw the
    # link, and G4 misses the outbound-ADD-deduped case. Require positive evidence instead, so
    # absence is never read as deletion (full rationale in ``peer_confirmations``).
    if confirm_store is not None and not confirm_store.is_confirmed(
        local_id, target_local_id, relation
    ):
        logger.warning(
            "_apply_inbound_update: declining the inbound removal of %s link %s -> %s: no "
            "peer-confirmation record, so this link was never proven to reach the peer and "
            "its absence there is not evidence of a deletion",
            relation,
            local_id,
            target_local_id,
        )
        return False

    try:
        link_uuid, _ = _get_link_info(tracker_dir(repo_root) / local_id, target_local_id, relation)
    except Exception as exc:  # noqa: BLE001 — fail-open: decline the removal, never guess
        logger.warning(
            "_apply_inbound_update: cannot resolve the net-active link %s -> %s, "
            "declining the inbound removal of relation %s: %r",
            local_id,
            target_local_id,
            relation,
            exc,
        )
        return False

    if not link_uuid:
        # Already gone, or the pair holds only OTHER relations (a re-applied record, or a
        # local unlink beat us). Removing nothing is exactly right — never remove a link
        # the peer still carries (G5) — but never silently.
        logger.info(
            "_apply_inbound_update: no active %s link %s -> %s; the inbound removal of "
            "relation %s is a no-op (any other relation the pair holds is untouched)",
            relation,
            local_id,
            target_local_id,
            relation,
        )
        return False

    try:
        rebar.unlink(local_id, target_local_id, relation, repo_root=repo_root)
        return True
    except Exception as exc:  # noqa: BLE001 — fail-open: skip this link, continue applying others
        logger.warning(
            "_apply_inbound_update: rebar.unlink failed for %s -> %s (%s): %r",
            local_id,
            target_local_id,
            relation,
            exc,
        )
        return False


def _open_impossible_link_store(repo_root):
    """Open the durable impossible-link record, or None if it cannot be opened.

    None disables the whole feature for this pass and restores the previous
    attempt-every-time behaviour — the memory is an optimisation, never a
    precondition for applying links.
    """
    try:
        from rebar._commands._seam import tracker_dir
        from rebar_reconciler.impossible_links import ImpossibleLinkStore

        return ImpossibleLinkStore(str(tracker_dir(repo_root)))
    except Exception as exc:  # noqa: BLE001 — fail-open: no memory is worse, not fatal
        logger.debug("_apply_inbound_update: impossible-link store unavailable: %r", exc)
        return None


def _open_peer_confirmation_store(repo_root):
    """The peer-confirmation store, or None if unopenable — which disables the
    removal-decline for the pass, restoring pre-a4bd behaviour (epic a4bd)."""
    from rebar_reconciler.peer_confirmations import open_store_or_none

    return open_store_or_none(repo_root)


def _skip_impossible_link(skip_store, local_id, target_local_id, relation) -> bool:
    """True when this link is already known impossible and the world has not moved."""
    if skip_store is None:
        return False
    try:
        reason = skip_store.should_skip(local_id, target_local_id, relation)
    except Exception as exc:  # noqa: BLE001 — an unusable record must never block a write
        logger.debug("_apply_inbound_update: impossible-link lookup failed: %r", exc)
        return False
    if reason is None:
        return False
    logger.debug(
        "_apply_inbound_update: skipping %s -> %s (%s): recorded as impossible (%s)",
        local_id,
        target_local_id,
        relation,
        reason,
    )
    return True


def _note_impossible_link(skip_store, local_id, target_local_id, relation, exc) -> str:
    """Record a permanently-impossible link; return a suffix for the WARNING line.

    An empty suffix means the failure was NOT classified as permanent, so it is
    not recorded and will be retried next pass exactly as before.
    """
    if skip_store is None:
        return ""
    try:
        from rebar_reconciler.impossible_links import classify

        reason = classify(exc)
        if reason is None:
            return ""
        skip_store.record(local_id, target_local_id, relation, reason)
    except Exception as note_exc:  # noqa: BLE001 — recording is best-effort
        logger.debug("_apply_inbound_update: could not record impossible link: %r", note_exc)
        return ""
    return (
        f" — structurally impossible ({reason}); recorded, and not retried until the "
        "deciding local state changes"
    )


def _inbound_update_apply_links(payload, local_id, repo_root) -> int:
    """Phase: write each Jira-sourced relation change into rebar via the rebar facades."""
    # Cycle 3: inbound links — write each Jira-sourced relation into rebar via
    # the rebar.link library facade. rebar.link owns relation validation,
    # hierarchy promotion, cycle/redundant-link guards, and the LINK event
    # write — so we do NOT hand-write LINK events here. The redundant-link
    # guard inside add_dependency makes re-apply idempotent. Failures are
    # non-fatal and logged.
    #
    # Ticket 2b16: REMOVE records are now honoured too. This loop previously read
    # ``if entry.get("action") != "add": continue``, so a removal emitted by
    # ``inbound_differ._diff_link_removals_inbound`` was SILENTLY DISCARDED — the pass
    # reported OK, the dep stayed put, and nothing raised. An unrecognised action is still
    # skipped, but loudly, so the next new record type cannot fail the same way.
    inbound_links = payload.get("links") or []
    links_applied: int = 0
    if isinstance(inbound_links, list):
        import rebar

        # Bug b8b1: three of the failures below are deterministic verdicts about the
        # LOCAL graph (closed source / redundant with the hierarchy / cycle-forming),
        # not faults. Without memory the differ re-emits the identical record every
        # pass and we re-spend the write — measured at 19 doomed writes per live pass,
        # a byte-identical set each time. Consult the durable record first, and record
        # a permanent verdict after the fact so the next pass can skip it.
        skip_store = _open_impossible_link_store(repo_root)
        skipped: int = 0
        # Epic a4bd: opened ONCE per pass (never per record) beside the skip store, and
        # None-when-unopenable so the whole decline degrades to pre-a4bd behaviour.
        confirm_store = _open_peer_confirmation_store(repo_root)
        declined: int = 0

        for entry in inbound_links:
            if not isinstance(entry, dict):
                continue
            action = entry.get("action")
            target_local_id = entry.get("target_id")
            relation = entry.get("relation")
            if not target_local_id or not relation:
                continue
            if action == "add":
                if _skip_impossible_link(skip_store, local_id, target_local_id, relation):
                    skipped += 1
                    continue
                try:
                    rebar.link(local_id, target_local_id, relation, repo_root=repo_root)
                    links_applied += 1
                except Exception as exc:  # noqa: BLE001 — fail-open: skip this link, continue applying others
                    logger.warning(
                        "_apply_inbound_update: rebar.link failed for %s -> %s (%s): %r%s",
                        local_id,
                        target_local_id,
                        relation,
                        exc,
                        _note_impossible_link(skip_store, local_id, target_local_id, relation, exc),
                    )
            elif action == "remove":
                if _inbound_unlink_one(
                    local_id, target_local_id, relation, repo_root, confirm_store
                ):
                    links_applied += 1
                elif confirm_store is not None and not confirm_store.is_confirmed(
                    local_id, target_local_id, relation
                ):
                    declined += 1
            else:
                logger.warning(
                    "_apply_inbound_update: ignoring inbound link record with unknown "
                    "action %r for %s -> %s (%s)",
                    action,
                    local_id,
                    target_local_id,
                    relation,
                )

        if skip_store is not None:
            if skipped:
                # INFO, not WARNING, and once per pass rather than once per link: the
                # whole point is to drain the permanent error floor from the log while
                # keeping the fact of the suppression visible. The individual records
                # (and why each is impossible) live in the store file.
                logger.info(
                    "_apply_inbound_update: skipped %d structurally-impossible inbound "
                    "link record(s) for %s; details in %s",
                    skipped,
                    local_id,
                    skip_store.path,
                )
            skip_store.save()
        if declined:
            # INFO once per pass, like the impossible-link summary: a decline is SAFE, not
            # an error, but must stay visible — this defect class was silent.
            logger.info(
                "_apply_inbound_update: declined %d inbound link removal(s) for %s with no "
                "peer-confirmation record (never proven to reach the peer)",
                declined,
                local_id,
            )
    return links_applied
