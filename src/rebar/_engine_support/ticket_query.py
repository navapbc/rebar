"""The ``list_tickets`` filter set as one dataclass (stdlib-only leaf).

``list_tickets`` threads the same ~13 filter parameters through several read
layers. Historically each layer respelled the whole set in its own signature
(the public facade, the ``_reads`` library shim, the ``list_states`` filter core,
the CLI ``list`` arm), so adding a filter meant editing every layer. ``TicketQuery``
collapses the INNER replication: the filter core (``_engine_support.reads.list_states``)
and its callers take one ``TicketQuery`` instead of respelling 13 keyword params.

The PUBLIC scalar signatures stay scalar by design — the library facade
(``rebar.list_tickets``), the MCP tool, and the CLI ``list`` argv contract each
keep their explicit parameters (API / tool-schema / argv stability) and build a
``TicketQuery`` at the boundary via :meth:`TicketQuery.from_library`, which owns the
library→engine normalization (``None`` → ``""`` sentinels and the ``priority``
int→str cast). The engine-form field spelling here matches ``list_states`` exactly.

This is a leaf: it imports only stdlib, so every read layer (and the reducer's
callers) can depend on it downward without a package cycle. In particular the pure
``rebar.reducer`` layer must NOT import this (the layering guard forbids
``reducer`` → ``_engine_support``); ``reducer.apply_ticket_filters`` therefore keeps
its scalar signature and ``list_states`` maps the query onto it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TicketQuery:
    """The filter set for a ticket list, in engine (``list_states``) form.

    Every field is optional and defaults to the "not filtered" value, so
    ``TicketQuery()`` lists everything and ``TicketQuery(status="open")`` narrows a
    single dimension. Strings use ``""`` (not ``None``) as the unset sentinel to
    match the filter core; ``priority`` is a string here (the int→str cast happens
    in :meth:`from_library`).
    """

    status: str = ""
    ticket_type: str = ""
    priority: str = ""
    parent: str = ""
    has_tag: str = ""
    without_tag: str = ""
    include_archived: bool = False
    exclude_deleted: bool = False
    min_children: int | None = None
    blocking_state: str = ""
    with_children_count: bool = False
    sort: str = ""
    include_body: bool = True

    @classmethod
    def from_library(
        cls,
        *,
        status: str | None = None,
        ticket_type: str | None = None,
        priority: int | str | None = None,
        parent: str | None = None,
        has_tag: str | None = None,
        without_tag: str | None = None,
        include_archived: bool = False,
        exclude_deleted: bool = False,
        min_children: int | None = None,
        blocking_state: str = "",
        with_children_count: bool = False,
        sort: str | None = None,
        include_body: bool = True,
    ) -> TicketQuery:
        """Build a query from the library/facade parameters (the ``None``-sentinel,
        ``priority: int|str|None`` form), normalizing to the engine field shape."""
        return cls(
            status=status or "",
            ticket_type=ticket_type or "",
            priority="" if priority is None else str(priority),
            parent=parent or "",
            has_tag=has_tag or "",
            without_tag=without_tag or "",
            include_archived=include_archived,
            exclude_deleted=exclude_deleted,
            min_children=min_children,
            blocking_state=blocking_state,
            with_children_count=with_children_count,
            sort=sort or "",
            include_body=include_body,
        )
