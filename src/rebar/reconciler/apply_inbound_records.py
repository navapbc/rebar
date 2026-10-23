#!/usr/bin/env python3
"""Apply inbound assignee identity and link-graph changes through rebar facades.

Identity minting and link/unlink operations use shared stores, including
impossible-link and peer-confirmation records. Failures log and leave inbound
apply running. Ticket event-file writes live in ``apply_inbound_events``.
"""

from __future__ import annotations

import logging

from rebar.reconciler.inbound_translate import _extract_name

logger = logging.getLogger(__name__)


def _identity_provider_for(backend) -> str:
    """Return the backend's validated store-facing identity family.

    The backend declares its family across Cloud/DC deployments. Fall back to
    ``vendor`` only when absent so unknown values fail validation rather than
    silently acquiring another provider's identity namespace.
    """
    from rebar.reducer._version import validate_creation_channel

    return validate_creation_channel(getattr(backend, "identity_family", None) or backend.vendor)


def _ensure_inbound_assignee_identity(assignee, repo_root) -> None:
    """Best-effort mint or reuse an identity for a Jira assignee.

    Prefer Cloud ``accountId``, then Data Center ``name`` or canonical
    ``account_id``. Missing IDs or mint failures leave inbound apply running.
    """
    if not isinstance(assignee, dict):
        return
    external_id = ""
    # Prefer Cloud ID, then DC name, then canonical differ ID. A bare string
    # could be a Cloud display name, so it cannot establish external identity.
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
        from rebar.reconciler._backend_registry import select_backend

        # Use the configured backend's family for provider and creation channel.
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
    """Mirror a peer deletion with relation-scoped ``rebar.unlink``.

    Only the named active relation is removed; absent ones log a no-op. Check
    through the same relation-aware lookup used by unlink. Return True only
    when an UNLINK was written, for accurate applied counts.
    """
    import rebar
    from rebar._commands._seam import tracker_dir
    from rebar._commands.unlink import _get_link_info

    # Require peer confirmation before treating a missing managed link as deleted.
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
        from rebar.reconciler.impossible_links import ImpossibleLinkStore

        return ImpossibleLinkStore(str(tracker_dir(repo_root)))
    except Exception as exc:  # noqa: BLE001 — fail-open: no memory is worse, not fatal
        logger.debug("_apply_inbound_update: impossible-link store unavailable: %r", exc)
        return None


def _open_peer_confirmation_store(repo_root):
    """The peer-confirmation store, or None if unopenable — which disables the
    removal-decline for the pass, restoring pre-a4bd behaviour (epic a4bd)."""
    from rebar.reconciler.peer_confirmations import open_store_or_none

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
        from rebar.reconciler.impossible_links import classify

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
    # Apply Jira link adds/removals through rebar facades, which own graph
    # validation and events. Log failures and unknown actions.
    inbound_links = payload.get("links") or []
    links_applied: int = 0
    if isinstance(inbound_links, list):
        import rebar

        # Cache deterministic local-graph rejections so later passes skip them.
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
                    # force: an inbound edge is peer-authored, and its evidence (e.g. the
                    # introducing commit behind a caused_by) may live only in the peer's
                    # clone. Applying it unforced would let the caused_by referencing-commit
                    # check drop a synced edge and record it as permanently impossible.
                    rebar.link(
                        local_id,
                        target_local_id,
                        relation,
                        force="inbound reconcile: peer-authored edge",
                        repo_root=repo_root,
                    )
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
