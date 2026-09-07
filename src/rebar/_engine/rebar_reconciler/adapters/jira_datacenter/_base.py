"""Provide shared construction, unwrapping, logging, retry, and paging.

Every Jira Data Center capability mixin inherits ``_TransportBase``, which owns
the injected client, project state, Epic Link field ID, and sole initializer.
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
    """Run ``fn`` through connection retry and log failures before re-raising.

    Warnings name the public transport member and remote ID, including at call
    sites that swallow exceptions. ``rate_limit_retry`` is forwarded explicitly
    so paged reads can opt in.
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
    """Hold the shared client, project state, Epic Link field ID, and pager."""

    # Declared at class level (type-only) so every capability mixin that inherits
    # ``_TransportBase`` sees a resolvable type for these attributes regardless of
    # which mixin's method reads/writes them first — without this, mypy cannot
    # always determine the type of an attribute only ever assigned inside
    # ``__init__`` when it is read from a sibling mixin's method.
    _client: Any
    project: str
    _epic_link_field_id: Any

    def __init__(self, *, client: Any, project: str) -> None:
        self._client = client
        self.project = project
        # Ticket 39c1 (follow-up): cache the discovered "Epic Link" field id across calls —
        # `_MISSING` (not yet looked up) is distinguished from `None` (looked up, this instance
        # has no such field), so a fieldless instance is not re-probed on every `set_parent`.
        self._epic_link_field_id = _MISSING

    def _paged_search(
        self,
        jql: str,
        *,
        fields: str | None = None,
        page_size: int = 100,
        rate_limit_retry: bool = False,
    ) -> list[dict[str, Any]]:
        """Return every issue matching ``jql`` through offset pagination.

        Advance ``startAt`` by the count returned because Data Center can cap
        pages below ``maxResults``. An empty page ends iteration. Repeating the
        previous page's first issue key raises ``BackendPaginationStallError`` for
        both short and full pages, preventing truncated maps and unbounded loops.
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
