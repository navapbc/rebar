"""``rebar attach-commits <ticket> <sha>...`` — retroactively link commits to a ticket.

The repair surface for a botched ``rebar-ticket:`` trailer: when a commit landed without a
usable trailer, its ticket cannot be linked to the change at close time. Attaching the SHAs
records a ``COMMITS`` event that the close gate's file-impact-vs-diff check consumes.

Intentionally thin. Validation (and its ALL-OR-NOTHING semantics) lives in the shared
``rebar.attach_commits`` seam so this CLI, the Python library, and the MCP tool behave
identically — there is no CLI-only rule to drift out of sync.
"""

from __future__ import annotations

from rebar._commands._seam import CommandError

USAGE = "Usage: rebar attach-commits <ticket_id> <sha> [<sha>...]"


def attach_commits_cli(args: list[str]) -> int:
    """Parse ``<ticket> <sha>...`` and route to the shared seam."""
    if len(args) < 2:
        raise CommandError(USAGE, returncode=2)
    import rebar
    from rebar._errors import RebarError

    ticket_id, shas = args[0], args[1:]
    try:
        result = rebar.attach_commits(ticket_id, shas)
    except RebarError as exc:  # surfaced as a clean CLI error, not a traceback
        raise CommandError(f"Error: {exc}", returncode=1) from None
    # Normalized confirmation (was `ATTACHED: <n> commit(s) to <id>`; both data —
    # the count and the resolved id — are preserved). Ticket 6bda-9d58-8546-4638.
    from rebar._commands import _confirm

    _confirm.emit(
        "commits-attached",
        result["ticket_id"],
        f"{result['attached']} commit(s)",
        f"commits attached to {result['ticket_id']}: {result['attached']}",
    )
    return 0
