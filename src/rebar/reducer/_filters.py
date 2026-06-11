"""Shared in-memory filter predicates for ``ticket list`` / ``ticket_list``.

Both the standalone script (``ticket-list.sh``) and the in-process library
(``ticket-lib-api.sh:ticket_list``) reduce the corpus and then narrow it by the
same criteria, in both their default-JSON and ``--output llm`` output branches —
four call sites in total. Keeping the predicate chain here, instead of
copy-pasting it into each inline ``python3 -c`` block, makes cross-implementation
and cross-format equivalence *structural* rather than something the tests must
police: a semantics change lands in one place for all four sites.

Semantics (mirrored in ``ticket list --help`` and ticket-cli-reference.md):

  * Filters AND across dimensions; within a single dimension comma-separated
    values are OR.
  * ``priority`` is matched as an exact integer — a ticket with no explicit
    priority never matches a ``--priority`` filter.
  * ``tag`` keeps tickets carrying ANY of the listed tags; ``without_tag``
    excludes tickets carrying ANY of the listed tags.
  * ``error``/``fsck_needed`` tickets are dropped unless explicitly requested via
    ``--status`` (d145-e1a9).

Validation of filter *values* (e.g. ``--priority`` range) stays in the calling
shell scripts so a bad flag fails fast with a clear message before reduction;
this function assumes already-validated inputs and treats an empty string as
"dimension not filtered".
"""

from __future__ import annotations


def apply_ticket_filters(
    results: list,
    *,
    type_filter: str = "",
    status_filter: str = "",
    parent_filter: str = "",
    tag_filter: str = "",
    priority_filter: str = "",
    without_tag_filter: str = "",
) -> list:
    """Return ``results`` narrowed by the supplied filters.

    ``results`` is a list of compiled ticket-state dicts (as produced by
    ``reduce_all_tickets``). Each filter is a raw CLI string; an empty string
    means the dimension is not filtered. The input list is not mutated.
    """
    # error/fsck_needed are excluded unless explicitly requested via --status.
    if status_filter not in ("error", "fsck_needed"):
        results = [t for t in results if t.get("status") not in ("error", "fsck_needed")]
    if type_filter:
        results = [t for t in results if t.get("ticket_type") == type_filter]
    if status_filter:
        status_values = {s.strip() for s in status_filter.split(",")}
        results = [t for t in results if t.get("status") in status_values]
    if parent_filter:
        results = [t for t in results if t.get("parent_id") == parent_filter]
    if tag_filter:
        tag_values = {s.strip() for s in tag_filter.split(",") if s.strip()}
        results = [t for t in results if tag_values & set(t.get("tags") or [])]
    if priority_filter:
        priority_values = {int(p.strip()) for p in priority_filter.split(",") if p.strip()}
        results = [t for t in results if t.get("priority") in priority_values]
    if without_tag_filter:
        without_values = {s.strip() for s in without_tag_filter.split(",") if s.strip()}
        results = [t for t in results if not (without_values & set(t.get("tags") or []))]
    return results
