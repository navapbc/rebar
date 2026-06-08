"""rebar MCP server (FastMCP).

Exposes the ticket system as MCP tools, built on the rebar Python library.
Reads (``show``/``list``) use the library's subprocess wrappers (alias-aware);
``reconcile`` defaults to a non-mutating dry-run.

Safety:
  * ``reconcile`` defaults to ``dry-run``; ``live`` additionally requires
    REBAR_MCP_ALLOW_RECONCILE_LIVE=1.
  * Write tools (create/transition/edit/link/unlink/tag/untag/archive/comment)
    are gated by REBAR_MCP_READONLY: set it to 1 to expose a read-only server.

The ``mcp`` dependency is an optional extra and is imported lazily.
"""

from __future__ import annotations

import os

import rebar


def _readonly() -> bool:
    return os.environ.get("REBAR_MCP_READONLY", "").strip() in ("1", "true", "yes")


def build_server():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise SystemExit(
            "The rebar MCP server requires the 'mcp' extra. "
            "Install it with: pip install 'rebar[mcp]'"
        ) from exc

    mcp = FastMCP("rebar")

    # ── Read tools ────────────────────────────────────────────────────────────
    @mcp.tool()
    def show_ticket(ticket_id: str) -> dict:
        """Show compiled ticket state (accepts full id, short id, or alias)."""
        return rebar.show_ticket(ticket_id)

    @mcp.tool()
    def list_tickets(
        status: str | None = None,
        ticket_type: str | None = None,
        has_tag: str | None = None,
        include_archived: bool = False,
    ) -> list[dict]:
        """List tickets as a JSON array, with optional filters."""
        return rebar.list_tickets(
            status=status,
            ticket_type=ticket_type,
            has_tag=has_tag,
            include_archived=include_archived,
        )

    @mcp.tool()
    def ticket_deps(ticket_id: str) -> dict:
        """Show the dependency graph for a ticket."""
        return rebar.deps(ticket_id)

    @mcp.tool()
    def ready_tickets() -> object:
        """List tickets ready to work (all blockers closed)."""
        return rebar.ready()

    @mcp.tool()
    def next_batch(epic_id: str) -> dict:
        """Next parallel batch of unblocked tickets under an epic's hierarchy."""
        return rebar.next_batch(epic_id)

    @mcp.tool()
    def reconcile(mode: str = "dry-run") -> dict:
        """Run the Jira reconciler. Defaults to a non-mutating dry-run.

        'live' mutates Jira and requires REBAR_MCP_ALLOW_RECONCILE_LIVE=1.
        """
        if mode == "live" and os.environ.get(
            "REBAR_MCP_ALLOW_RECONCILE_LIVE", ""
        ).strip() not in ("1", "true", "yes"):
            raise ValueError(
                "live reconcile is disabled; set REBAR_MCP_ALLOW_RECONCILE_LIVE=1 to enable"
            )
        return rebar.reconcile(mode)

    # ── Write tools (gated by REBAR_MCP_READONLY) ──────────────────────────────
    if not _readonly():

        @mcp.tool()
        def create_ticket(
            ticket_type: str,
            title: str,
            parent: str | None = None,
            priority: int | None = None,
            description: str | None = None,
            tags: list[str] | None = None,
        ) -> str:
            """Create a ticket; returns the canonical ticket id."""
            return rebar.create_ticket(
                ticket_type,
                title,
                parent=parent,
                priority=priority,
                description=description,
                tags=tags,
            )

        @mcp.tool()
        def transition_ticket(
            ticket_id: str, current_status: str, target_status: str
        ) -> dict:
            """Transition a ticket's status (optimistic concurrency)."""
            return rebar.transition(ticket_id, current_status, target_status)

        @mcp.tool()
        def comment_ticket(ticket_id: str, body: str) -> str:
            """Append a comment to a ticket."""
            rebar.comment(ticket_id, body)
            return "ok"

        @mcp.tool()
        def edit_ticket(
            ticket_id: str,
            title: str | None = None,
            priority: int | None = None,
            assignee: str | None = None,
            description: str | None = None,
            tags: list[str] | None = None,
        ) -> str:
            """Edit ticket fields."""
            rebar.edit_ticket(
                ticket_id,
                title=title,
                priority=priority,
                assignee=assignee,
                description=description,
                tags=tags,
            )
            return "ok"

        @mcp.tool()
        def link_tickets(id1: str, id2: str, relation: str) -> str:
            """Link two tickets (blocks | depends_on | relates_to)."""
            rebar.link(id1, id2, relation)
            return "ok"

        @mcp.tool()
        def unlink_tickets(id1: str, id2: str) -> str:
            """Remove a link between two tickets."""
            rebar.unlink(id1, id2)
            return "ok"

        @mcp.tool()
        def tag_ticket(ticket_id: str, tag: str) -> str:
            """Add a tag to a ticket."""
            rebar.tag(ticket_id, tag)
            return "ok"

        @mcp.tool()
        def untag_ticket(ticket_id: str, tag: str) -> str:
            """Remove a tag from a ticket."""
            rebar.untag(ticket_id, tag)
            return "ok"

        @mcp.tool()
        def archive_ticket(ticket_id: str) -> str:
            """Archive a ticket (excludes it from the default list)."""
            rebar.archive(ticket_id)
            return "ok"

    return mcp


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
