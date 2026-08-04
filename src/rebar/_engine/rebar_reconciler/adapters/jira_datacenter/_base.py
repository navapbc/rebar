"""Shared plumbing for the Jira Data Center transport's capability mixins
(ticket 465d, epic e369) — construction, the unwrap boundary, the logged-retry
choke point, and the one shared pager.

RELOCATED VERBATIM out of ``transport.py``; this module changes no behaviour.
Every capability mixin (``_issues.py``, ``_hierarchy.py``, ``_links.py``,
``_comments.py``, ``_people.py``, ``_properties.py``) inherits from
:class:`_TransportBase` so mypy sees ``self._client`` / ``self.project`` /
``self._epic_link_field_id`` / ``self._resolved_statuses`` without each mixin
re-declaring them, and so ``__init__`` exists exactly once regardless of how
many mixins ``JiraDataCenterTransport`` composes.
"""

from __future__ import annotations

import logging
from typing import Any

from rebar_reconciler._backend import BackendPaginationStallError
from rebar_reconciler.adapters.jira_datacenter.retry import _with_connection_retry

logger = logging.getLogger(__name__)

#: Distinguishes "not yet looked up" from "looked up, found nothing" for the
#: Epic Link field-id cache (ticket 39c1) — see ``_hierarchy.py``.
_MISSING = object()


def _unwrap(obj: Any) -> Any:
    """Unwrap a ``pycontribs`` library object (``Issue``/``Comment``/…) to rebar's
    raw payload dict via its ``.raw`` attribute — the parsed JSON the REST API
    actually returned. An object with no ``.raw`` (e.g. an already-plain dict)
    passes through unchanged. This is THE unwrapping boundary the whole story
    exists to enforce: nothing downstream of this function ever sees a
    ``jira.Issue`` (or any other library object)."""
    raw = getattr(obj, "raw", None)
    return raw if raw is not None else obj


def _call_logged(member: str, remote_id: Any, fn: Any, *, rate_limit_retry: bool = False) -> Any:
    """Run ``fn()`` through :func:`_with_connection_retry`, logging a WARNING that
    names the transport MEMBER and the REMOTE ID before any failure propagates.

    Seven of the twelve members added by story J9 are invoked from core call sites
    that swallow ``Exception`` at EVERY site (comments, links, parents, issue
    properties, assignee validation), and three more swallow it at some sites. A
    failure there produces no crash and no record — which is precisely how a DC
    deployment can "converge" while syncing nothing. This log is the only signal
    those paths emit, so it is written HERE, at the single choke point every
    member routes through, rather than per method (where it would be forgotten by
    the thirteenth member). The exception is re-raised untouched: this observes,
    it never handles. Follows ``adapters/jira/acli_subprocess.py``'s module-level
    ``logger = logging.getLogger(__name__)`` convention.

    ``rate_limit_retry`` is FORWARDED, defaulting to False. This forward is load-bearing
    rather than cosmetic: before story S2 this function called ``_with_connection_retry(fn)``
    with no keyword at all, so a flag threaded only as far as here would have been a no-op
    that still type-checked — every ``_paged_search`` read would have looked opted-in and
    retried nothing.
    """
    try:
        return _with_connection_retry(fn, rate_limit_retry=rate_limit_retry)
    except Exception as exc:
        logger.warning(
            "jira-datacenter transport: %s failed for remote id %r: %r", member, remote_id, exc
        )
        raise


def _user_attr(user: Any, key: str) -> Any:
    """Read ``key`` off a ``jira.resources.User`` (attribute) or an already-raw
    dict (item) — the two shapes ``search_users`` yields against a real client and
    against an injected fake respectively."""
    raw = _unwrap(user)
    if isinstance(raw, dict):
        return raw.get(key)
    return getattr(user, key, None)


class _TransportBase:
    """Construction + the shared pager every capability mixin is built on.

    ``resolved_statuses`` defaults to Cloud/DIG's ``{Resolved, Done, Cancelled}``
    (``settings.DEFAULT_RESOLVED_STATUSES``) so a transport built directly with a
    fake client (as the unit tests do) never needs a loaded config; production
    construction threads the configured set through from
    ``resolve_jira_datacenter_settings`` (``reconciler.resolved_statuses``).
    """

    # Declared at class level (type-only) so every capability mixin that inherits
    # ``_TransportBase`` sees a resolvable type for these attributes regardless of
    # which mixin's method reads/writes them first — without this, mypy cannot
    # always determine the type of an attribute only ever assigned inside
    # ``__init__`` when it is read from a sibling mixin's method.
    _client: Any
    project: str
    _epic_link_field_id: Any
    _resolved_statuses: frozenset[str]

    def __init__(
        self,
        *,
        client: Any,
        project: str,
        resolved_statuses: frozenset[str] | None = None,
    ) -> None:
        self._client = client
        self.project = project
        # Ticket 39c1 (follow-up): cache the discovered "Epic Link" field id across calls —
        # `_MISSING` (not yet looked up) is distinguished from `None` (looked up, this instance
        # has no such field), so a fieldless instance is not re-probed on every `set_parent`.
        self._epic_link_field_id = _MISSING
        if resolved_statuses is None:
            from rebar_reconciler.adapters.jira_datacenter.settings import (
                DEFAULT_RESOLVED_STATUSES,
            )

            resolved_statuses = DEFAULT_RESOLVED_STATUSES
        self._resolved_statuses = resolved_statuses

    def _paged_search(
        self,
        jql: str,
        *,
        fields: str | None = None,
        page_size: int = 100,
        rate_limit_retry: bool = False,
    ) -> list[dict[str, Any]]:
        """Every issue matching ``jql``, paged to exhaustion — the ONE pager the
        whole-project readers share.

        Advances by what the server ACTUALLY returned and stops only on an EMPTY page.
        Jira DC silently truncates ``maxResults`` above ``jira.search.views.default.max``,
        so a SHORT page is not proof of exhaustion: advancing by the REQUESTED size reads
        a truncated FIRST page as the final one (measured: 20 of 250 recovered).

        SHARED rather than repeated per method because that defect was fixed once, in
        ``get_parent_map`` alone, leaving the two siblings here plus a third in
        ``fetcher._iter_pages``. A structural test now fails the build if any caller takes
        the ``search_issues`` default again (bug 9263).

        **Termination (ticket 18a4).** Two conditions stop the walk, and only two:

        * an EMPTY page — the ordinary exhaustion exit; or
        * an OFFSET STALL — a page repeating the previous page's first issue key,
          which proves the server is not honouring ``startAt``. That raises
          :class:`~rebar_reconciler._backend.BackendPaginationStallError` naming
          ``startAt`` and the stalled offset. Without it the empty-page exit is
          unreachable against a ``startAt``-blind instance and this loop runs
          forever, ``out`` growing without bound.

          The stall aborts on ANY repeated page, SHORT **or** FULL — deliberately
          UNLIKE ``fetcher._iter_pages``, which returns cleanly on a repeated SHORT
          page. That reasoning ("fewer than asked for, so nothing further to give")
          does not transfer to DC: this pager exists *because* a hardened DC caps
          EVERY page below the requested size, so on DC a short page is the normal
          case WITH more to give. Treating it as exhaustion would silently return
          20 of 250 — the exact bug-9263 loss this pager was built to refuse. It
          cannot false-positive either: a ``startAt``-honouring server serves
          DIFFERENT issues at each offset.

        Raises:
            BackendPaginationStallError: the server stopped honouring ``startAt``.
        """
        out: list[dict[str, Any]] = []
        start_at = 0
        prev_first_key: Any = None
        while True:
            results = _call_logged(
                "_paged_search",
                jql,
                lambda offset=start_at: self._client.search_issues(
                    jql, startAt=offset, maxResults=page_size, fields=fields
                ),
                rate_limit_retry=rate_limit_retry,
            )
            batch = [_unwrap(issue) for issue in results]
            if not batch:
                break
            # A missing/unusable key yields ``None``, which never compares equal to a
            # previous ``None`` here — two consecutive keyless pages must not be read as
            # a stall, and a non-dict item (the readers already tolerate junk) must not
            # raise an AttributeError from the guard itself.
            head = batch[0]
            first_key = head.get("key") if isinstance(head, dict) else None
            if first_key is not None and first_key == prev_first_key:
                raise BackendPaginationStallError(
                    f"jira-datacenter _paged_search: the search endpoint returned the "
                    f"same first issue ({first_key!r}) again at startAt={start_at} — it "
                    f"is not honouring `startAt`, so paging can never advance and this "
                    f"whole-project read is truncated (jql={jql!r})"
                )
            prev_first_key = first_key
            out.extend(batch)
            start_at += len(batch)
        return out
