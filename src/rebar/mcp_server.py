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

# Typed output for show_ticket so FastMCP advertises an outputSchema to MCP
# clients (agents get a documented, validated shape instead of an opaque dict).
# Mirrors src/rebar/schemas/ticket_state.schema.json — kept permissive
# (extra="allow", non-core fields optional) so the evolving event-sourced shape
# never breaks the tool. A cross-interface test pins this and the CLI/library
# output to the canonical schema.
#
# Defined at module level (not inside build_server) because FastMCP resolves
# return annotations via eval against the function's module globals; a local
# class can't be resolved under `from __future__ import annotations`. pydantic is
# guaranteed by the `mcp` extra; guarded so a bare `import rebar.mcp_server`
# without the extra still reaches build_server's friendly install message.
try:
    from pydantic import BaseModel, ConfigDict

    class TicketStateOut(BaseModel):
        model_config = ConfigDict(extra="allow")

        ticket_id: str
        ticket_type: str
        title: str
        status: str
        priority: int
        tags: list[str] = []
        assignee: str | None = None
        parent_id: str | None = None
        alias: str | None = None
        description: str | None = None
        comments: list[dict] = []
        deps: list[dict] = []
        file_impact: list[dict] = []
except ImportError:  # pragma: no cover - pydantic ships with the mcp extra
    TicketStateOut = None  # type: ignore[assignment,misc]


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
    def show_ticket(ticket_id: str) -> TicketStateOut:
        """Show compiled ticket state (accepts full id, short id, or alias)."""
        return TicketStateOut.model_validate(rebar.show_ticket(ticket_id))

    @mcp.tool()
    def list_tickets(
        status: str | None = None,
        ticket_type: str | None = None,
        priority: int | None = None,
        parent: str | None = None,
        has_tag: str | None = None,
        without_tag: str | None = None,
        include_archived: bool = False,
    ) -> list[dict]:
        """List tickets as a JSON array, with optional filters."""
        return rebar.list_tickets(
            status=status,
            ticket_type=ticket_type,
            priority=priority,
            parent=parent,
            has_tag=has_tag,
            without_tag=without_tag,
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
    def search(
        query: str,
        status: str | None = None,
        ticket_type: str | None = None,
        has_tag: str | None = None,
        include_archived: bool = False,
    ) -> list[dict]:
        """Full-text search over titles/descriptions/comments/tags (replay-derived)."""
        return rebar.search(
            query,
            status=status,
            ticket_type=ticket_type,
            has_tag=has_tag,
            include_archived=include_archived,
        )

    @mcp.tool()
    def fsck(recover: bool = False) -> str:
        """Check ticket-store integrity (JSON validity, CREATE presence, lock
        cleanup). Set recover=True to run the recovery path."""
        return rebar.fsck(recover=recover)

    # ── Quality gates + file-impact reads (WS5d) ───────────────────────────────
    @mcp.tool()
    def clarity_check(ticket_id: str) -> dict:
        """Score ticket clarity (score / verdict / threshold / passed)."""
        return rebar.clarity_check(ticket_id)

    @mcp.tool()
    def check_ac(ticket_id: str) -> dict:
        """Check the ticket has an Acceptance Criteria block (passed / output)."""
        return rebar.check_ac(ticket_id)

    @mcp.tool()
    def quality_check(ticket_id: str) -> dict:
        """Check ticket dispatch readiness (passed / output)."""
        return rebar.quality_check(ticket_id)

    @mcp.tool()
    def validate() -> dict:
        """Repo-wide quality health check (JSON report: score, critical/major/
        minor issues, warnings, suggestions). Takes no ticket id."""
        return rebar.validate()

    @mcp.tool()
    def get_file_impact(ticket_id: str) -> list[dict]:
        """Get the file-impact array (consumed by next-batch conflict scheduling)."""
        return rebar.get_file_impact(ticket_id)

    @mcp.tool()
    def get_verify_commands(ticket_id: str) -> list[dict]:
        """Get the DD-level verify-commands array for a ticket."""
        return rebar.get_verify_commands(ticket_id)

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
            assignee: str | None = None,
            description: str | None = None,
            tags: list[str] | None = None,
        ) -> str:
            """Create a ticket; returns the canonical ticket id."""
            return rebar.create_ticket(
                ticket_type,
                title,
                parent=parent,
                priority=priority,
                assignee=assignee,
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
        def claim_ticket(ticket_id: str, assignee: str | None = None) -> dict:
            """Atomically claim an OPEN ticket (-> in_progress + assignee).

            Raises a tool error (ConcurrencyError) if the ticket is not open —
            i.e. another agent already claimed it.
            """
            return rebar.claim(ticket_id, assignee=assignee)

        @mcp.tool()
        def reopen_ticket(ticket_id: str) -> dict:
            """Reopen a closed ticket (closed -> open). Optimistic-concurrency:
            raises a tool error if the ticket is not currently closed."""
            return rebar.reopen(ticket_id)

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

        @mcp.tool()
        def compact_ticket(ticket_id: str | None = None) -> str:
            """Compact a ticket's event log (or all tickets if id omitted)."""
            rebar.compact(ticket_id)
            return "ok"

        # ── File-impact / verify-commands writes (WS5d; feed next-batch) ───────
        @mcp.tool()
        def set_file_impact(ticket_id: str, impact: list) -> str:
            """Record file impact (list of {path, reason}) for conflict-aware
            next-batch scheduling."""
            rebar.set_file_impact(ticket_id, impact)
            return "ok"

        @mcp.tool()
        def set_verify_commands(ticket_id: str, commands: list) -> str:
            """Record DD-level verify commands (list of {dd_id, dd_text, command})."""
            rebar.set_verify_commands(ticket_id, commands)
            return "ok"

    return mcp


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
