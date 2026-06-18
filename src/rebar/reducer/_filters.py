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

# Query-field name → reduced-state key. Fields not listed map to themselves.
_FIELD_KEY = {"type": "ticket_type", "parent": "parent_id"}


def match_predicate(state: dict, field: str, op: str, value) -> bool:
    """Evaluate ONE field predicate against a reduced ticket-state dict.

    The single comparison vocabulary shared by the structured-query parser
    (``reducer/_query.py``) and :func:`apply_ticket_filters`, so both speak the
    same semantics (P1.1). Never raises — an unknown field, an uncomparable
    value, or an unset state field yields ``False``.

    ``field`` is a query-field name (``status``/``type``/``priority``/
    ``assignee``/``tag``/``parent``). ``op`` is one of ``eq``/``in``/``lt``/
    ``le``/``gt``/``ge``/``range``. ``value`` is a scalar for the scalar ops, a
    set/iterable for ``in``, and a ``(lo, hi)`` pair (either bound ``None``) for
    ``range``. ``priority`` values are coerced to ``int``; a ticket with no
    explicit priority never matches a priority predicate.
    """
    if field == "tag":
        tags = state.get("tags") or []
        if op == "in":
            return any(v in tags for v in value)
        return value in tags

    if field == "priority":
        pv = state.get("priority")
        if pv is None:
            return False
        try:
            if op == "in":
                return pv in {int(v) for v in value}
            if op == "range":
                lo, hi = value
                if lo is not None and pv < int(lo):
                    return False
                if hi is not None and pv > int(hi):
                    return False
                return True
            ival = int(value)
        except (TypeError, ValueError):
            return False
        if op == "eq":
            return pv == ival
        if op == "lt":
            return pv < ival
        if op == "le":
            return pv <= ival
        if op == "gt":
            return pv > ival
        if op == "ge":
            return pv >= ival
        return False

    # String-valued fields: status, ticket_type, assignee, parent_id.
    actual = state.get(_FIELD_KEY.get(field, field))
    if op == "in":
        return actual in set(value)
    if op == "eq":
        return actual == value
    return False


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
        results = [t for t in results if match_predicate(t, "type", "eq", type_filter)]
    if status_filter:
        status_values = {s.strip() for s in status_filter.split(",")}
        results = [t for t in results if match_predicate(t, "status", "in", status_values)]
    if parent_filter:
        results = [t for t in results if match_predicate(t, "parent", "eq", parent_filter)]
    if tag_filter:
        tag_values = {s.strip() for s in tag_filter.split(",") if s.strip()}
        results = [t for t in results if match_predicate(t, "tag", "in", tag_values)]
    if priority_filter:
        priority_values = {p.strip() for p in priority_filter.split(",") if p.strip()}
        results = [t for t in results if match_predicate(t, "priority", "in", priority_values)]
    if without_tag_filter:
        without_values = {s.strip() for s in without_tag_filter.split(",") if s.strip()}
        results = [t for t in results if not match_predicate(t, "tag", "in", without_values)]
    return results
