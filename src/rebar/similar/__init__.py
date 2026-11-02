"""Similar-ticket retrieval.

ADR 0122 fixed the retrieval architecture; this package builds its default tier -- a
lexical FTS5 index that needs no model provider, no credentials and no network, so
retrieval works in every installation. An opt-in embedding tier layers over the same
index.

The whole surface is one function, :func:`find_similar`, and its defining property is
that it CANNOT fail its caller. Every failure path returns an empty list after
logging at warning level. It is consulted on the write path of ticket creation, where
an advisory that raised would turn a successful create into a failed command.

Catch-up is synchronous and happens here, in the read path, before the query is
served. There is no daemon and no background thread.
"""

from __future__ import annotations

import logging
from typing import Any

from .catchup import catch_up, live_entries
from .index import index_dir, index_path, open_index
from .lexical import RESULT_LIMIT, match_expression, query_text, search

logger = logging.getLogger(__name__)

__all__ = [
    "RESULT_LIMIT",
    "catch_up",
    "find_similar",
    "index_dir",
    "index_path",
    "live_entries",
    "match_expression",
    "open_index",
    "query_text",
    "search",
]


def find_similar(
    tracker: str,
    *,
    title: str = "",
    description: str = "",
    text: str | None = None,
    exclude_id: str | None = None,
    limit: int = RESULT_LIMIT,
) -> list[dict[str, Any]]:
    """Ranked similar tickets, or ``[]``. Never raises.

    Pass *title* and *description* to have the measured query text built, or *text*
    to supply it directly. *exclude_id* keeps a ticket out of its own results.
    """
    conn = None
    try:
        conn = open_index(tracker)
        if conn is None:
            return []
        try:
            catch_up(tracker, conn)
        except Exception:
            # Serving stale results would be worse than serving none: the caller
            # cannot tell the difference, and a silently frozen index looks exactly
            # like a healthy one that matched nothing.
            logger.warning("similar-ticket catch-up failed; returning no results", exc_info=True)
            return []
        return search(
            conn,
            text if text is not None else query_text(title, description),
            exclude_id=exclude_id,
            limit=limit,
        )
    except Exception:
        logger.warning("similar-ticket retrieval failed; returning no results", exc_info=True)
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                logger.warning("similar-ticket index close failed", exc_info=True)
