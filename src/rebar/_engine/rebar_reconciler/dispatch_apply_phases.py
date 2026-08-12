#!/usr/bin/env python3
"""Non-retrying ``update_one`` apply phases (module-size split of ``dispatch_one.py``).

``update_one`` is a thin sequencer over per-phase helpers. This leaf owns the phases
that do NOT funnel their Jira writes through ``dispatch_one._call_with_retry``: the
allowlist filter, the reporter-by-accountId REST sub-call (264f) and its identity /
alert-store degradation helpers, and the single-attempt comment dispatch. The retrying
phases (parent reparent, the scalar edit, label + link dispatch) STAY in ``dispatch_one``
next to the shared ``_call_with_retry`` backoff hub.

This module is a strict LEAF: it imports nothing from ``dispatch_one`` (its only
cross-module reach is the lazy in-function imports of ``rebar``,
``rebar._commands.identity`` and ``rebar_reconciler._loader``), so ``dispatch_one`` can
re-import the three phase functions back without a cycle. ``dispatch_one`` re-exports
``_update_one_apply_reporter`` / ``_update_one_filter_fields`` /
``_update_one_dispatch_comments`` so ``update_one``'s bare-name calls and
``dispatch_one.<phase>`` attribute access are unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from ._backend import TicketTransport

import logging
import random
import sys
import time
import urllib.error
from pathlib import Path

# Runtime (not TYPE_CHECKING) import: the capability guard needs the Protocol OBJECT for
# isinstance, not just its name for annotations — ADR-0083 prescribes isinstance-guarded
# capability detection and SupportsComments is @runtime_checkable for exactly that.
from rebar_reconciler._backend import SupportsComments
from rebar_reconciler._errors import (
    MAX_BACKOFF_S,
    JiraAPIError,
    RetryExhaustedError,
    parse_retry_after,
)
from rebar_reconciler.pass_io import record_capability_gap

logger = logging.getLogger(__name__)

# Bug 85a1: strip fields ACLI does not accept on `jira workitem edit`.
# The legacy batch path here was unfiltered, so a local issuetype change
# (e.g., probe Phase 2 ticket_type=task→bug) flowed through as
# ``--issuetype Bug`` which ACLI rejects with non-zero exit, aborting the
# ENTIRE batch loop and silently losing every subsequent outbound update.
# The typed leaf ``_apply_outbound_update`` already filters via
# ``_OUTBOUND_UPDATE_ALLOWLIST`` — apply the same allowlist here. Stripped
# fields (issuetype, type-change in general) are intentional drops mirroring
# the typed-leaf contract; outbound issuetype changes are BY_DESIGN
# unsupported on the edit endpoint (Atlassian JRASERVER-71292).
# status is included: bug 85a1 (Gap 8) removed the BY_DESIGN drop —
# outbound status push now uses REST POST /transitions via
# ``transition_issue`` (bypasses ACLI's silent-exit-0 failure mode).
# The typed leaf's REBAR_RECONCILER_STATUS_GATING gate is also gone.
_OUTBOUND_BATCH_ALLOWLIST = frozenset({"summary", "description", "assignee", "priority", "status"})


def _call_with_retry(fn, *args, max_retries: int = 3, **kwargs):
    """Call fn(*args, **kwargs) with exponential backoff on retryable failures.

    Retryable: TimeoutError; JiraAPIError 5xx/429; and (story 9622)
    urllib.error.HTTPError 5xx/429 — the REST floor (acli_rest) raises raw
    HTTPError, previously uncaught here, so the idempotent REST writes routed
    through it got zero retry. 429 honors a present integer ``Retry-After``, else
    ADR-0036 jittered backoff; 5xx uses that backoff.
    Non-retryable: JiraAPIError / HTTPError 4xx (except 429) — re-raised raw
    immediately (preserving the 404 / hierarchy-400 semantics). On exhaustion a
    retried HTTPError re-raises raw; TimeoutError/JiraAPIError raise RetryExhaustedError.

    Args:
        fn:          Callable to invoke.
        *args:       Positional arguments forwarded to fn.
        max_retries: Maximum number of retry attempts after the first failure.
        **kwargs:    Keyword arguments forwarded to fn.

    Returns:
        The return value of fn on success.

    Raises:
        RetryExhaustedError: When all retry attempts are exhausted.
        JiraAPIError:        Immediately, for non-retryable 4xx (except 429) errors.
    """
    delays = [1, 2, 4]
    last_exc: Exception | None = None

    for attempt in range(max_retries + 1):
        retry_after: float | None = None
        try:
            return fn(*args, **kwargs)
        except JiraAPIError as exc:
            # 429 and 5xx are retryable; all other 4xx fail fast
            if exc.status_code != 429 and 400 <= exc.status_code < 500:
                raise
            last_exc = exc
        except urllib.error.HTTPError as exc:
            # REST-transport floor (acli_rest raises raw HTTPError): 429/5xx
            # retryable, other 4xx fail fast (raw). HTTPError.code is the status.
            if exc.code != 429 and 400 <= exc.code < 500:
                raise
            last_exc = exc
            if exc.code == 429:
                retry_after = parse_retry_after(
                    exc.headers.get("Retry-After") if exc.headers else None
                )
        except TimeoutError as exc:
            last_exc = exc

        if attempt < max_retries:
            if retry_after is not None:
                delay: float = min(MAX_BACKOFF_S, retry_after)
            elif isinstance(last_exc, urllib.error.HTTPError):
                # ADR 0036: 2**(attempt+1) + jitter, capped.
                delay = min(MAX_BACKOFF_S, 2 ** (attempt + 1) + random.random())
            else:
                delay = delays[min(attempt, len(delays) - 1)]
            time.sleep(delay)

    # On exhaustion of a retried HTTPError, re-raise the ORIGINAL raw HTTPError
    # (story 9622): downstream catchers switch on raw HTTPError (e.g.
    # apply_handlers.handle_update softens 404 but re-raises non-404 5xx as
    # pass-fatal), so wrapping it would silently defeat them. TimeoutError /
    # JiraAPIError keep the RetryExhaustedError contract.
    if isinstance(last_exc, urllib.error.HTTPError):
        raise last_exc
    # CHAIN the cause (PEP 3134) — the prior `raise RetryExhaustedError(str(last_exc))` dropped
    # __cause__, losing the underlying failure (epic romp-swath-wince). Populate last_exception /
    # attempts for post-hoc inspection.
    raise RetryExhaustedError(
        str(last_exc), last_exception=last_exc, attempts=max_retries + 1
    ) from last_exc


def _capability_present(
    client: TicketTransport,
    protocol: type,
    capability: str,
    member: str,
    site: str,
    key: str | None,
) -> bool:
    """Runtime capability guard for an OPT-IN capability member (ticket a3fa).

    The four ``cast("SupportsComments"/"SupportsLinks", client)`` call sites are STATIC
    assertions with no runtime effect, and every one of them sits inside a broad
    ``except Exception``. So a transport that does not implement the capability raised an
    ``AttributeError`` that was swallowed: the sub-op silently never applied and the pass
    reported success — conflating "capability absent (designed, fine to skip)" with
    "capability present but threw (a real failure)". This splits the two.

    RELATION TO ``fetcher.py`` (AC4). ADR-0083 ("Reconciler vendor adapter seam") prescribes
    ``isinstance``-guarded capability detection — "callers detect a capability by an
    ``isinstance``-guarded check against the backend" — and ``SupportsComments``/
    ``SupportsLinks`` are ``@runtime_checkable`` for exactly that. So ``isinstance`` is the
    PRIMARY check here. It is backed by the ``hasattr(client, member)`` shape that
    ``fetcher.py:597/647/688`` already uses, which makes the two subsystems consistent rather
    than divergent, and is REQUIRED for correctness rather than merely permitted:

    Since Python 3.12 (gh-102433) a ``@runtime_checkable`` ``isinstance`` resolves members
    with ``inspect.getattr_static``, which deliberately does NOT see attributes served by
    ``__getattr__``. Any dynamically-proxying transport — a decorator/wrapper that forwards
    to an inner client, and every ``MagicMock`` test double — therefore HAS ``add_comment``
    yet fails ``isinstance``. Trusting ``isinstance`` alone would declare such a transport
    "capability absent" and skip the write **by design**, which is the exact silent-skip
    defect this ticket exists to remove, only now blessed. The fallback closes that hole.

    Checking the specific ``member`` (not the whole Protocol) is also the right granularity
    for a WRITE dispatch site: the question here is "can this transport perform the call I am
    about to make", not "does it also implement the read side" (``get_comment_map`` /
    ``get_issuelinks_map``), which this code path never touches.

    Returns True when the call may proceed. When absent, logs an INFO line naming the missing
    capability and the site, records the designed skip on the EXISTING ``bridge_alerts``
    channel under ``outbound-<capability>-capability-absent`` (deduped per process per
    (capability, site) — see ``pass_io.record_capability_gap``), and returns False. Callers
    must skip WITHOUT incrementing their ``*_computed``/``*_applied`` counters: those feed
    ``apply_handlers``' silent-no-op canary, which is a FAILURE detector (it logs the
    "bug-3f04 failure mode" warning and, under ``RECONCILER_FAIL_SILENT_NOOP=1``, records a
    per-mutation failure), and routing a DESIGNED skip through it would re-conflate exactly
    what this guard separates.
    """
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
    from rebar_reconciler._loader import lazy_load

    return lazy_load("rebar_reconciler.alert_store", "alert_store.py")


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
        print(
            f"update_one: dropping fields not accepted by ACLI edit "
            f"for {mutation.get('key')}: {sorted(_stripped.keys())}",
            file=sys.stderr,
        )
    return {k: v for k, v in fields.items() if k in _OUTBOUND_BATCH_ALLOWLIST}


def _update_one_dispatch_comments(
    mutation, client: TicketTransport, issue_key, comment_errors
) -> tuple[int, int]:
    """Phase: dispatch comment-add sub-ops (in-band capture into comment_errors).
    Returns (computed, applied) counts."""
    _comments_computed = _comments_applied = 0

    comments = mutation.get("comments", []) or []
    # Ticket a3fa: capability guard folded INTO the condition (same shape as create_one and
    # _update_one_dispatch_links). It sits AHEAD of _comments_computed deliberately: a
    # DESIGNED skip must increment neither _computed nor _applied, because apply_handlers'
    # silent-no-op canary is a FAILURE detector — it logs the "bug-3f04 failure mode" warning
    # and, under RECONCILER_FAIL_SILENT_NOOP=1, records a per-mutation failure. Routing a
    # designed skip through it would re-conflate exactly what the guard separates.
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
            body = entry.get("body", "")
            if not body:
                continue
            _comments_computed += 1
            try:
                # Story 9622 (D2): single-attempt, no retry (see create-path note).
                cast("SupportsComments", client).add_comment(issue_key, body)
                _comments_applied += 1
            except Exception as exc:  # noqa: BLE001 — in-band capture into comment_errors; non-fatal
                # Bug 6afc-20ee-84e5-4dd5: non-fatal, but surface it so the batch
                # outcome no longer reports error=None for a mutation whose
                # comment sub-mutation failed.
                if comment_errors is not None:
                    comment_errors.append(f"add_comment failed: {exc!s}")
                print(
                    f"update_one: add_comment failed for {issue_key}: {exc!r}",
                    file=sys.stderr,
                )
    return _comments_computed, _comments_applied


def _index_existing_links(issuelinks) -> set[tuple[str, str]]:
    """Index a ``get_issue_links`` result as a ``{(type_name, other_key)}`` set.

    Bug 3f04: local copy of ``apply_outbound._index_existing_links`` — this module
    deliberately never imports the applier (cycle avoidance), so the helper is
    duplicated. Records ``type.name`` plus the OTHER issue's key on EITHER side
    (``inwardIssue``/``outwardIssue``), so the membership test is direction-agnostic
    (a ``Blocks`` link to B is "present" whether B is the inward or outward side).
    """
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
