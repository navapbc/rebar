#!/usr/bin/env python3
"""Non-retrying ``update_one`` apply phases (module-size split of ``dispatch_one.py``).

The transport owns retries. This leaf also owns link probes and capability guards,
so dispatch_one can sequence these phases without a dependency cycle. The parent,
scalar edit, and label phases remain in dispatch_one."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from ._backend import TicketTransport

import logging
import subprocess
import sys
import time
import urllib.error
from pathlib import Path

# Runtime (not TYPE_CHECKING) import: the capability guard needs the Protocol OBJECT for
# isinstance, not just its name for annotations — ADR-0083 prescribes isinstance-guarded
# capability detection and SupportsComments/SupportsLinks are @runtime_checkable for
# exactly that.
from rebar.reconciler._backend import SupportsComments, SupportsLinks
from rebar.reconciler.binding_lifecycle import UNKNOWN_COMMENT_ID
from rebar.reconciler.pass_io import record_capability_gap

logger = logging.getLogger(__name__)

# ACLI edit accepts these fields. Drop unsupported issue-type changes before a
# batch failure can prevent later updates. Status transitions use the REST path.
_OUTBOUND_BATCH_ALLOWLIST = frozenset({"summary", "description", "assignee", "priority", "status"})


def _capability_present(
    client: TicketTransport,
    protocol: type,
    capability: str,
    member: str,
    site: str,
    key: str | None,
) -> bool:
    """Check the requested transport member before a comment or link write.
    Runtime-checkable Protocol membership is primary. Member lookup also admits
    dynamic proxies that Python's static attribute inspection misses. A missing
    capability records a designed skip and returns False without advancing computed
    or applied counters. Failures after a present capability remain failures."""
    if isinstance(client, protocol) or hasattr(client, member):
        return True
    logger.info(
        "%s: %s skipped for %s — transport does not implement %s "
        "(designed capability skip, not a failure)",
        site,
        member,
        key,
        capability,
    )
    record_capability_gap(capability, member, site, key)
    return False


def _jira_account_id_for(local_ref):
    """Resolve a local reporter string (identity id / email) to a Jira accountId via
    rebar core's identity seam (flow layer may import core), or ``None`` on any miss."""
    if not local_ref or not isinstance(local_ref, str):
        return None
    try:
        from rebar._commands import identity as _identity

        return _identity.jira_account_id(local_ref)
    except Exception:  # noqa: BLE001 — best-effort; an unresolvable reporter is a miss
        return None


def _load_alert_store():
    """Lazy-load the sibling alert_store module by file path (the run_differs / fetcher
    pattern) so a file-path-spec-loaded dispatch_one still resolves it."""

    return importlib.import_module("rebar.reconciler.alert_store")


def _record_reporter_alert(kind: str, jira_key, reason: str) -> None:
    """Best-effort soft-fail alert for the reporter REST sub-call (264f). Resolves
    repo_root via ``rebar.config.repo_root()`` and appends a record through the
    lazily-loaded alert_store. Fully fail-open — observability never breaks the sync."""
    try:
        import rebar

        repo_root = Path(rebar.config.repo_root())
    except Exception:  # noqa: BLE001 — no store → nothing to record; never break the pass
        return
    try:
        alert_store = _load_alert_store()
        alert_store.append(
            {
                "kind": kind,
                "jira_key": jira_key,
                "field": "reporter",
                "reason": reason,
                "timestamp_ns": time.time_ns(),
            },
            repo_root=repo_root,
        )
    except Exception:  # noqa: BLE001 — best-effort alert write; non-fatal
        pass


def _update_one_apply_reporter(fields, issue_key, client: TicketTransport) -> None:
    """Phase: apply the reporter via a dedicated REST sub-call (264f).

    Pops ``reporter`` off ``fields`` BEFORE the allowlist filter (so it never reaches
    the scalar edit and need not be allowlisted), resolves the local reporter string to
    a Jira accountId via the identity seam, and on success routes it through
    ``client.set_reporter(issue_key, account_id)`` (REST PUT reporter.accountId).

    Soft degradation (the sync never hard-fails on reporter, and other fields still
    apply): an unresolvable reporter is SKIPPED with an ``outbound-reporter-unresolved``
    alert; an ``HTTPError`` from ``set_reporter`` (a 4xx = Modify-Reporter not granted)
    is caught and recorded as ``outbound-reporter-not-permitted``, then execution
    continues."""
    if "reporter" not in fields:
        return
    reporter = fields.pop("reporter", None)
    if not reporter:
        return
    account_id = _jira_account_id_for(reporter)
    if account_id is None:
        _record_reporter_alert(
            "outbound-reporter-unresolved",
            issue_key,
            f"reporter {reporter!r} maps to no identity/accountId; skipped",
        )
        return
    try:
        client.set_reporter(issue_key, account_id)
    except urllib.error.HTTPError as exc:
        # 4xx = Modify-Reporter permission not granted (the common case); any HTTP
        # failure on the reporter sub-call degrades softly so the rest of the update
        # (and the pass) still succeeds.
        _record_reporter_alert(
            "outbound-reporter-not-permitted",
            issue_key,
            f"set_reporter HTTP {exc.code}: {exc.reason}",
        )


def _update_one_filter_fields(fields, mutation) -> dict:
    """Phase: log + strip fields ACLI's edit endpoint rejects, return the allowlisted set."""
    _stripped = {k: v for k, v in fields.items() if k not in _OUTBOUND_BATCH_ALLOWLIST}
    if _stripped:
        print(  # noqa: T201
            f"update_one: dropping fields not accepted by ACLI edit "
            f"for {mutation.get('key')}: {sorted(_stripped.keys())}",
            file=sys.stderr,
        )
    return {k: v for k, v in fields.items() if k in _OUTBOUND_BATCH_ALLOWLIST}


def _record_comment_id(binding_store, entry, add_comment_result) -> None:
    """Record a successful post by local comment key even when Jira returns no comment ID.
    Persist an ID when supplied, or UNKNOWN_COMMENT_ID when the transport cannot
    supply one. Optional legacy stores and entries without a key leave recording
    unchanged. Count the post before those guards so the pass invariant sees gaps."""
    # Count the landed post before recording guards. A skipped ID write must remain
    # visible to the end-of-pass invariant.
    noter = getattr(binding_store, "note_comment_posted", None)
    if noter is not None:
        noter()
    if binding_store is None or not isinstance(entry, dict):
        return
    key = entry.get("local_comment_key")
    if not key:
        return
    comment_id = add_comment_result.get("id") if isinstance(add_comment_result, dict) else None
    recorder = getattr(binding_store, "record_comment_id", None)
    if recorder is not None:
        recorder(key, comment_id or UNKNOWN_COMMENT_ID)


def _update_one_dispatch_comments(
    mutation, client: TicketTransport, issue_key, comment_errors, binding_store=None
) -> tuple[int, int]:
    """Dispatch comment additions once and return computed and applied counts. Capture
    failures in comment_errors. For keyed comments and stores with a recorder,
    save Jira's ID or UNKNOWN_COMMENT_ID to prevent reposting."""
    _comments_computed = _comments_applied = 0

    comments = mutation.get("comments", []) or []
    # Check comment capability before incrementing counts. A designed skip must not
    # trigger the silent-no-op failure canary.
    if (
        isinstance(comments, list)
        and comments
        and _capability_present(
            client,
            SupportsComments,
            "comments",
            "add_comment",
            "update_one.dispatch_comments",
            issue_key,
        )
    ):
        for entry in comments:
            if not isinstance(entry, dict):
                continue
            # Story 4cee: send the bytes the dedup key was taken from (see the
            # create path's note); ``body`` is the pre-cutover fallback.
            body = entry.get("wire_body") or entry.get("body", "")
            if not body:
                continue
            _comments_computed += 1
            try:
                # Story 9622 (D2): single-attempt, no retry (see create-path note).
                _comment_result = cast("SupportsComments", client).add_comment(issue_key, body)
                _comments_applied += 1
                # emersed-specific-mutt: persist the returned Jira comment ID against
                # the entry's local_comment_key so a re-sync does not re-post it.
                _record_comment_id(binding_store, entry, _comment_result)
            except Exception as exc:  # noqa: BLE001 — in-band capture into comment_errors; non-fatal
                # Capture a nonfatal comment failure in the batch outcome.
                if comment_errors is not None:
                    comment_errors.append(f"add_comment failed: {exc!s}")
                print(  # noqa: T201
                    f"update_one: add_comment failed for {issue_key}: {exc!r}",
                    file=sys.stderr,
                )
    return _comments_computed, _comments_applied


def _index_existing_links(issuelinks) -> set[tuple[str, str]]:
    """Index link type and peer key from either issue side. This local helper avoids an
    applier import cycle and makes duplicate checks direction-independent."""
    existing: set[tuple[str, str]] = set()
    for link in issuelinks or []:
        if not isinstance(link, dict):
            continue
        link_type = link.get("type") or {}
        type_name = link_type.get("name") if isinstance(link_type, dict) else None
        if not type_name:
            continue
        for side_key in ("inwardIssue", "outwardIssue"):
            side = link.get(side_key)
            if isinstance(side, dict):
                side_key_val = side.get("key")
                if side_key_val:
                    existing.add((type_name, side_key_val))
    return existing


def _find_link_id(issuelinks, link_type: str, to_key: str) -> str | None:
    """Return the id of the issuelink of ``link_type`` to ``to_key`` (either direction).

    The REMOVE counterpart of :func:`_index_existing_links` (wake-inn-parse): the differ
    emits only (type, to_key) for a managed link to delete; the applier resolves the
    concrete link id from a fresh ``get_issue_links`` probe. Direction-agnostic (matches
    whether ``to_key`` is the inward or outward side). Returns None when no such link
    exists (already removed — idempotent success)."""
    for link in issuelinks or []:
        if not isinstance(link, dict):
            continue
        link_t = link.get("type") or {}
        type_name = link_t.get("name") if isinstance(link_t, dict) else None
        if type_name != link_type:
            continue
        for side_key in ("inwardIssue", "outwardIssue"):
            side = link.get(side_key)
            if isinstance(side, dict) and side.get("key") == to_key:
                link_id = link.get("id")
                return str(link_id) if link_id is not None else None
    return None


# A link DELETE that fails because the link is already gone (or was concurrently changed)
# still reaches the desired end-state — link absent — so it counts as applied. These are the
# only signatures that justify that, matched case-insensitively.
#
# Deliberately NARROW, and output that carries NO marker does NOT qualify. The previous code
# treated EVERY CalledProcessError as an idempotent race, which is how a deterministic,
# permanent failure (an unanswered confirmation prompt reading EOF from stdin=DEVNULL,
# reported by ACLI as "command cancelled") was scored as a successful removal on every pass,
# forever. The "self-healing next pass" rationale only holds for a TRANSIENT race; it is false
# for a deterministic one, so the benefit of the doubt has to be earned by an explicit
# signature.
_IDEMPOTENT_LINK_REMOVAL_MARKERS: tuple[str, ...] = (
    "404",
    "409",
    "does not exist",
    "not found",
)


def _is_idempotent_link_removal(exc: subprocess.CalledProcessError) -> bool:
    """Recognize an already absent link only from explicit 404, 409, or missing-link
    markers in either stdout or stderr. ACLI link delete reports text without JSON.
    An unmarked failure has not proven the desired end state."""
    text = ((exc.stderr or "") + (exc.stdout or "")).lower()
    if not text:
        return False
    return any(marker in text for marker in _IDEMPOTENT_LINK_REMOVAL_MARKERS)


def _dispatch_link_removes(links, client: TicketTransport, issue_key) -> tuple[int, int, int]:
    """Delete managed links by resolving their ID from a fresh issue-link probe.
    Return computed, applied, and failed counts. An already absent link succeeds.
    Other delete failures remain nonfatal and count as failed."""
    _computed = _applied = _failed = 0
    if not (
        isinstance(links, list)
        and any(isinstance(e, dict) and e.get("action") == "remove" for e in links)
    ):
        return _computed, _applied, _failed

    try:
        link_objs = client.get_issue_links(issue_key)
    except Exception as exc:  # noqa: BLE001 — probe is best-effort; skip removals this pass
        print(  # noqa: T201
            f"update_one: get_issue_links probe (remove) failed for {issue_key}: {exc!r}",
            file=sys.stderr,
        )
        return _computed, _applied, _failed

    for entry in links:
        if not isinstance(entry, dict) or entry.get("action") != "remove":
            continue
        link_type = entry.get("type")
        to_key = entry.get("to_key")
        if not link_type or not to_key:
            continue
        link_id = _find_link_id(link_objs, link_type, to_key)
        if link_id is None:
            continue  # already absent in Jira — idempotent success, nothing to do
        _computed += 1
        try:
            client.delete_issue_link(link_id)
            _applied += 1
        except subprocess.CalledProcessError as exc:
            # Only a recognized concurrent removal counts as applied. Other ACLI failures
            # remain failed, even when the batch continues.
            if _is_idempotent_link_removal(exc):
                _applied += 1
            else:
                _failed += 1
                _log_link_delete_failure(issue_key, to_key, link_type, exc)
        except Exception as exc:  # noqa: BLE001 — best-effort link op; non-fatal, counted
            # Also the Jira Data Center path, whose _links.py raises HTTPError rather than
            # CalledProcessError — counted failed, not ignored.
            _failed += 1
            _log_link_delete_failure(issue_key, to_key, link_type, exc)
    return _computed, _applied, _failed


def _log_link_delete_failure(issue_key, to_key, link_type, exc: BaseException) -> None:
    """Emit the operator-facing line for a link delete that did not reach its end-state."""
    print(  # noqa: T201
        f"update_one: delete_issue_link failed for {issue_key} -> {to_key} ({link_type}): {exc!r}",
        file=sys.stderr,
    )


def _confirm_link_add(link_confirm, entry, to_key, result) -> None:
    """Send a landed link addition to the optional peer-confirmation sink. Recording
    errors remain nonfatal because the vendor write has already succeeded."""
    if link_confirm is None:
        return
    try:
        # Record the vendor link ID when supplied. A missing ID is valid evidence.
        link_id = result.get("id") if isinstance(result, dict) else None
        link_confirm(to_key=to_key, relation=entry.get("relation"), link_id=link_id)
    except Exception as exc:  # noqa: BLE001 — evidence is best-effort; never fail a landed write
        print(f"update_one: peer-confirmation record failed for {to_key}: {exc!r}", file=sys.stderr)  # noqa: T201


def _update_one_dispatch_links(
    mutation,
    client: TicketTransport,
    issue_key,
    *,
    link_confirm: Callable[..., None] | None = None,
) -> tuple[int, int, int]:
    """Dispatch deduplicated link adds and managed removals. Return computed, applied,
    and failed counts after add deduplication. A landed add may call link_confirm with
    peer evidence. Link failures are reported without unwinding successful scalar
    updates."""
    _links_computed = _links_applied = _links_failed = 0

    # Probe existing links once before adding. Either direction counts as present.
    # A timed-out but committed prior write must not create a duplicate.
    links = mutation.get("links", []) or []
    # Check link capability before counting an add. A designed skip must not appear
    # as a silent-no-op failure.
    if (
        isinstance(links, list)
        and any(isinstance(e, dict) and e.get("action") == "add" for e in links)
        and _capability_present(
            client,
            SupportsLinks,
            "links",
            "set_relationship",
            "update_one.dispatch_links",
            issue_key,
        )
    ):
        existing_links: set[tuple[str, str]] | None = None
        try:
            existing_links = _index_existing_links(client.get_issue_links(issue_key))
        except Exception as exc:  # noqa: BLE001 — dedup probe is best-effort; proceed without it
            existing_links = None
            print(  # noqa: T201
                f"update_one: get_issue_links probe failed for {issue_key}: {exc!r}",
                file=sys.stderr,
            )
        for entry in links:
            if not isinstance(entry, dict) or entry.get("action") != "add":
                continue
            link_type = entry.get("type")
            to_key = entry.get("to_key")
            if not link_type or not to_key:
                continue
            if existing_links is not None and (link_type, to_key) in existing_links:
                continue  # already present (either direction) — no duplicate add
            # Counted AFTER the dedup skip so a fully-deduped mutation is computed==0 (no canary).
            _links_computed += 1
            frm, to = (to_key, issue_key) if entry.get("swap") else (issue_key, to_key)
            try:
                _link_result = cast("SupportsLinks", client).set_relationship(frm, to, link_type)
                _links_applied += 1
                _confirm_link_add(link_confirm, entry, to_key, _link_result)
            except Exception as exc:  # noqa: BLE001 — best-effort link op; non-fatal, counted
                _links_failed += 1
                print(  # noqa: T201
                    f"update_one: set_relationship failed for {frm} -> {to} ({link_type}): {exc!r}",
                    file=sys.stderr,
                )

    _rm_computed, _rm_applied, _rm_failed = _dispatch_link_removes(links, client, issue_key)
    return (
        _links_computed + _rm_computed,
        _links_applied + _rm_applied,
        _links_failed + _rm_failed,
    )
