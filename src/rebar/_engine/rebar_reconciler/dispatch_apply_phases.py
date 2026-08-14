#!/usr/bin/env python3
"""Non-retrying ``update_one`` apply phases (module-size split of ``dispatch_one.py``).

``update_one`` is a thin sequencer over per-phase helpers. This leaf owns the allowlist
filter, the reporter-by-accountId REST sub-call (264f) and its identity / alert-store
degradation helpers, the single-attempt comment dispatch, the shared ``_call_with_retry``
backoff hub, and the link-probe helpers.

The LINK dispatch phase joined them here (ticket 5528): ``dispatch_one`` crossed the
800-LOC cap once the delete arm learned to tell a proven-gone link from a failure. Links
were the natural cluster to move rather than an arbitrary slice — the phase already calls
four helpers that live here (``_call_with_retry``, ``_capability_present``,
``_find_link_id``, ``_index_existing_links``), so the move REMOVED cross-module reach
instead of adding it, and it sits beside its sibling ``_update_one_dispatch_comments``.
The parent reparent, scalar edit and label phases stay in ``dispatch_one``.

This module is a strict LEAF: it imports nothing from ``dispatch_one`` (its only
cross-module reach is the lazy in-function imports of ``rebar``,
``rebar._commands.identity`` and ``rebar_reconciler._loader``), so ``dispatch_one`` can
re-import the three phase functions back without a cycle. ``dispatch_one`` re-exports
``_update_one_apply_reporter`` / ``_update_one_filter_fields`` /
``_update_one_dispatch_comments`` so ``update_one``'s bare-name calls and
``dispatch_one.<phase>`` attribute access are unchanged.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from ._backend import TicketTransport

import logging
import random
import subprocess
import sys
import time
import urllib.error
from pathlib import Path

# Runtime (not TYPE_CHECKING) import: the capability guard needs the Protocol OBJECT for
# isinstance, not just its name for annotations — ADR-0083 prescribes isinstance-guarded
# capability detection and SupportsComments/SupportsLinks are @runtime_checkable for
# exactly that.
from rebar_reconciler._backend import SupportsComments, SupportsLinks
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


def _record_comment_id(binding_store, entry, add_comment_result) -> None:
    """Persist the returned Jira comment ID against ``entry``'s local_comment_key.

    emersed-specific-mutt (append-only comment sync). ``add_comment`` returns
    ``{"id": ...}``; this captures that ID and records it via the binding_store's
    write-ahead ``record_comment_id`` map, keyed on the COMMENT event's HLC
    (``entry["local_comment_key"]``), so a re-sync never re-posts the comment.

    Every dependency is optional and guarded, so this is a no-op — never an error —
    when the store is absent (legacy caller / stub), the entry carries no key, the
    result carries no id, or the store predates ``record_comment_id``. Shared by
    both enactment sites (``create_one`` and ``_update_one_dispatch_comments``) so
    they cannot drift.
    """
    if binding_store is None or not isinstance(entry, dict):
        return
    key = entry.get("local_comment_key")
    if not key:
        return
    comment_id = add_comment_result.get("id") if isinstance(add_comment_result, dict) else None
    if not comment_id:
        return
    recorder = getattr(binding_store, "record_comment_id", None)
    if recorder is not None:
        recorder(key, comment_id)


def _update_one_dispatch_comments(
    mutation, client: TicketTransport, issue_key, comment_errors, binding_store=None
) -> tuple[int, int]:
    """Phase: dispatch comment-add sub-ops (in-band capture into comment_errors).
    Returns (computed, applied) counts. emersed-specific-mutt: when ``binding_store``
    is provided, the returned Jira comment ID is persisted against the entry's
    ``local_comment_key`` so a re-sync does not re-post the comment (append-only)."""
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
                _comment_result = cast("SupportsComments", client).add_comment(issue_key, body)
                _comments_applied += 1
                # emersed-specific-mutt: persist the returned Jira comment ID against
                # the entry's local_comment_key so a re-sync does not re-post it.
                _record_comment_id(binding_store, entry, _comment_result)
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
    """True when *exc* shows a link delete failed because the link was already gone.

    Matches BOTH streams, following the ``delete_issue`` precedent in ``adapters/jira/acli.py``
    (``err_text = (exc.stderr or "") + (exc.stdout or "")``) rather than stderr alone. That
    matters here specifically: ``workitem link delete`` runs WITHOUT ``--json`` (story 25ae —
    ACLI rejects the flag on this subcommand), so its failure output is human-readable text on
    the exit-code path and the "404 / not found" line can land on STDOUT. Reading stderr only
    would misclassify a GENUINE concurrent-removal race as a failure — a false alarm in the
    honest-counter direction, which would erode trust in the very counter this narrowing
    exists to make trustworthy.

    See :data:`_IDEMPOTENT_LINK_REMOVAL_MARKERS` for why this is a narrow allow-list and why
    output carrying no marker at all returns False (unproven end-state, counted failed).
    """
    text = ((exc.stderr or "") + (exc.stdout or "")).lower()
    if not text:
        return False
    return any(marker in text for marker in _IDEMPOTENT_LINK_REMOVAL_MARKERS)


def _dispatch_link_removes(links, client: TicketTransport, issue_key) -> tuple[int, int, int]:
    """Sub-phase: the symmetric link REMOVE dispatch. Returns (computed, applied, failed).

    Split out of :func:`_update_one_dispatch_links` along the ADD/REMOVE seam that phase
    already had. The two halves share only the counters, so this is an extraction of an
    existing cluster rather than a mechanical carve — and it keeps the parent function under
    its locked complexity ceiling now that the delete arm distinguishes a proven-gone link
    from a failure.

    Bug wake-inn-parse: a managed link the differ marked for removal (a deliberate local
    unlink) is deleted on Jira so the inbound differ stops re-adding it. The differ emits only
    (type, to_key); resolve the link id here by probing the issue's current links (mirrors the
    ADD dedup probe). Best-effort + logged — a link op must not unwind the batch.
    """
    _computed = _applied = _failed = 0
    if not (
        isinstance(links, list)
        and any(isinstance(e, dict) and e.get("action") == "remove" for e in links)
    ):
        return _computed, _applied, _failed

    try:
        link_objs = client.get_issue_links(issue_key)
    except Exception as exc:  # noqa: BLE001 — probe is best-effort; skip removals this pass
        print(
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
            _call_with_retry(client.delete_issue_link, link_id)
            _applied += 1
        except subprocess.CalledProcessError as exc:
            # delete_issue_link shells out via ACLI (raises CalledProcessError, NOT an
            # HTTPError). We only reach here after _find_link_id confirmed the link exists in
            # a fresh probe, so a failure carrying a 404/409/"does not exist" signature is a
            # concurrent removal or change — idempotent, the desired end-state (link gone) is
            # reached, so it counts as applied.
            #
            # Any OTHER failure has NOT reached that end-state and is counted failed. Counting
            # it applied is what made ticket 5528 invisible: the missing confirmation flag made
            # every delete fail identically and permanently, while the pass reported
            # links_applied=N. Non-fatal either way — the batch is not unwound — but no longer
            # scored as success.
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
    print(
        f"update_one: delete_issue_link failed for {issue_key} -> {to_key} ({link_type}): {exc!r}",
        file=sys.stderr,
    )


def _confirm_link_add(link_confirm, entry, to_key, result) -> None:
    """Hand an ACCEPTED link ADD to the peer-confirmation sink (epic a4bd).

    Every failure here is swallowed. The link already landed on the vendor, so
    losing the evidence costs one un-declined removal (strictly the pre-a4bd
    behaviour), whereas raising would fail an outbound write that actually
    succeeded and would corrupt the applied/computed/failed counts — the very
    conflation between "did not land" and "reported fine" that ticket 5528 closed.
    """
    if link_confirm is None:
        return
    try:
        # The vendor's stable link id when it supplies one. ``set_relationship`` is
        # typed ``-> dict[str, Any]``, but a backend may legitimately return no id
        # (or a non-mapping); ``None`` is recorded and is NOT an error.
        link_id = result.get("id") if isinstance(result, dict) else None
        link_confirm(to_key=to_key, relation=entry.get("relation"), link_id=link_id)
    except Exception as exc:  # noqa: BLE001 — evidence is best-effort; never fail a landed write
        print(f"update_one: peer-confirmation record failed for {to_key}: {exc!r}", file=sys.stderr)


def _update_one_dispatch_links(
    mutation,
    client: TicketTransport,
    issue_key,
    *,
    link_confirm: Callable[..., None] | None = None,
) -> tuple[int, int, int]:
    """Phase: dispatch link ADD (deduped) + link REMOVE sub-ops. ``links_computed`` is
    counted POST-DEDUP so an idempotent re-sync reports 0 (no false canary). Returns
    (computed, applied, failed) counts.

    ``link_confirm`` (epic a4bd) is an optional sink invoked after a link ADD is
    ACCEPTED by the vendor, so the peer-confirmation store can record the evidence
    that the peer now carries this link. It is a callback rather than a store handle
    because this function has neither a binding store nor a pass id and must not grow
    knowledge of either — the production caller (``apply_handlers.handle_update``)
    closes over both. ``None`` (the default) preserves the pre-a4bd behaviour exactly.

    ``failed`` counts link ops that did NOT reach their desired end-state. It exists because
    a failure that is neither counted nor surfaced is indistinguishable from success: the
    delete arm used to increment ``applied`` on ANY ``CalledProcessError``, so a Jira pass
    could report ``links_applied=N`` having removed nothing (ticket 5528 — 15 cancelled ACLI
    deletes, 15 reported applied, 0 links actually removed). Failures stay NON-FATAL — the
    scalar update already succeeded and one bad link must not unwind the batch — but they
    are now counted and surfaced instead of silently absorbed.
    """
    _links_computed = _links_applied = _links_failed = 0

    # Bug 3f04: dispatch link adds (blocks/relates) via client.set_relationship.
    # The outbound differ emits these alongside changed scalar fields, but the
    # batch path never applied them (the link entry was dropped + no dispatch
    # here) — so outbound link sync was a silent no-op. Mirror the typed leaf
    # ``_apply_outbound_update``: probe the issue's existing links ONCE and skip
    # any add already present (either direction) so a re-issued POST after a
    # timed-out-but-committed create does not duplicate the link. Failures are
    # best-effort + logged (non-fatal — the scalar update already succeeded).
    links = mutation.get("links", []) or []
    # Ticket a3fa: the capability guard is folded INTO this condition rather than placed
    # inside the loop, for the same two reasons as create_one's comment guard — the
    # capability is invariant across the loop, and a boolean operand costs zero McCabe
    # complexity, so the guard does not raise this function's LOCKED complexity baseline.
    # Placing it here also keeps it AHEAD of _links_computed, which matters: a designed skip
    # must increment neither _computed nor _applied, or apply_handlers' silent-no-op canary
    # (a FAILURE detector) would report it as the bug-3f04 failure mode.
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
            print(
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
                _link_result = _call_with_retry(
                    cast("SupportsLinks", client).set_relationship, frm, to, link_type
                )
                _links_applied += 1
                _confirm_link_add(link_confirm, entry, to_key, _link_result)
            except Exception as exc:  # noqa: BLE001 — best-effort link op; non-fatal, counted
                _links_failed += 1
                print(
                    f"update_one: set_relationship failed for {frm} -> {to} ({link_type}): {exc!r}",
                    file=sys.stderr,
                )

    _rm_computed, _rm_applied, _rm_failed = _dispatch_link_removes(links, client, issue_key)
    return (
        _links_computed + _rm_computed,
        _links_applied + _rm_applied,
        _links_failed + _rm_failed,
    )
