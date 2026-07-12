"""rebar library — read path (queries, export/import, fsck).

The in-process read wrappers (``show_ticket`` / ``list_tickets`` / ``deps`` /
``ready`` / ``next_batch`` / ``search`` / ``recent_session_logs``), the NDJSON
``export_tickets`` / ``import_tickets`` interop, and
``fsck`` — split out of the ``rebar`` package facade (``__init__.py``, ticket
S3 / 4532) so it stays a thin re-export namespace. Every function is re-exported
as ``rebar.<name>``. The private ``_json_or`` helper lives here too and is
re-exported as ``rebar._json_or`` (see tests/unit/test_json_or_narrowing.py).

Reads compute from the in-process rebar.reducer / rebar.graph packages — no
subprocess (the bash orchestrator was retired in the bash→Python migration).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from rebar._errors import RebarError

if TYPE_CHECKING:
    # Schema-derived return types (story 3a10). Import-only under TYPE_CHECKING.
    from rebar.types import DepsGraph, NextBatch, TicketState


def _json_or(out: str, default):
    import json as _json

    try:
        return _json.loads(out)
    except _json.JSONDecodeError:
        # Opportunistic narrowing (epic ring-gun-jot): the only expected failure here is
        # malformed/empty JSON from a quality-gate subprocess; narrowed (with a regression
        # test) from a blind `except` so a genuine bug (e.g. a TypeError) is no longer
        # masked by the sentinel default. In the `import rebar` hot path.
        return default


# ── Read path (in-process via rebar._reads; alias-aware, returns parsed JSON) ──
def show_ticket(ticket_id: str, *, repo_root=None) -> TicketState:
    """Compiled ticket state as a dict (alias/short-id aware)."""
    from rebar import _reads

    return cast("TicketState", _reads.show_ticket(ticket_id, repo_root=repo_root))


def export_tickets(
    *,
    out=None,
    status: str | None = None,
    ticket_type: str | None = None,
    parent: str | None = None,
    strip_external: bool = False,
    include_session_logs: bool = False,
    exclude_archived: bool = False,
    include_deleted: bool = False,
    repo_root=None,
) -> dict:
    """Export the store as NDJSON (one full ticket per line) to ``out``.

    ``out`` is a writable text file object, a path (str/os.PathLike), or None for a
    metadata-only run (no write). A lossy interop projection — reporting/data-mining
    and clean rebar→rebar migration — NOT a backup. Streams via ``reduce_ticket``
    (bounded memory). ``strip_external`` removes all external-tracker linkage
    (provider-neutral). Scope defaults: all work types/statuses incl. closed;
    session_log excluded; archived included (marked); deleted excluded. Returns run
    metadata ``{exported, schema_version, source_env, exported_at}``. See
    :mod:`rebar._io.export_ndjson`.
    """
    from rebar._io import export_ndjson

    return export_ndjson.export_tickets(
        out=out,
        status=status,
        ticket_type=ticket_type,
        parent=parent,
        strip_external=strip_external,
        include_session_logs=include_session_logs,
        exclude_archived=exclude_archived,
        include_deleted=include_deleted,
        repo_root=repo_root,
    )


def import_tickets(source, *, dry_run: bool = False, repo_root=None) -> dict:
    """Import tickets from rebar export NDJSON ``source`` into this repo.

    ``source`` is an NDJSON file path, a file object, or an iterable of lines/dicts.
    A provenance import for clean rebar→rebar migration: each ticket gets a fresh
    local id + fresh HLC timestamps, with the source identity preserved as
    ``source_*``. Events are composed through the normal locked write path (CREATE +
    EDIT-parent + LINK + COMMENT + STATUS, two-pass), reproducing parents, links,
    tags, comments, file-impact, verify-commands, and non-open statuses. A dangling
    parent / link target is skipped with a warning, never a hard failure.
    ``dry_run`` reports create counts without writing. Returns run metadata
    ``{created, skipped, links, comments, warnings, dry_run}``. See
    :mod:`rebar._io.import_ndjson`.
    """
    from rebar._io import import_ndjson

    return import_ndjson.import_tickets(source, dry_run=dry_run, repo_root=repo_root)


def list_tickets(
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
    full: bool = True,
    repo_root=None,
) -> list[TicketState]:
    """List tickets as a list of dicts, with optional filters.

    ``exclude_deleted`` drops tickets whose reduced status is ``deleted``. Note
    delete writes STATUS(deleted)+ARCHIVED, so the default list already hides
    tombstones via archived-exclusion; ``exclude_deleted`` only changes results
    when combined with ``include_archived=True``. ``min_children`` keeps tickets
    with ≥ N direct children and ``blocking_state`` ("unblocked"/"blocked") filters
    by readiness. ``with_children_count`` adds a ``children_count`` field (opt-in,
    so the default shape matches show/search — the single-reducer invariant).
    ``sort`` orders the result by ``priority|created|updated|id|status`` (prefix
    ``-`` for descending; unset values sort last); the default keeps store order.
    ``full`` (default ``True``) emits the bulky ``description``/``comments`` fields;
    pass ``full=False`` for a lean summary (the default for the ``list`` CLI and the
    MCP ``list_tickets`` tool).
    """
    from rebar import _reads
    from rebar._engine_support.ticket_query import TicketQuery

    # Build the TicketQuery at this public boundary (``full`` is the library
    # spelling of the engine's ``include_body``), then funnel through the single
    # query-accepting read entry. The scalar filter shape lives ONCE — in
    # TicketQuery.from_library — so this facade no longer re-forwards it field by
    # field; a new filter is added to the dataclass, not respelled here.
    query = TicketQuery.from_library(
        status=status,
        ticket_type=ticket_type,
        priority=priority,
        parent=parent,
        has_tag=has_tag,
        without_tag=without_tag,
        include_archived=include_archived,
        exclude_deleted=exclude_deleted,
        min_children=min_children,
        blocking_state=blocking_state,
        with_children_count=with_children_count,
        sort=sort,
        include_body=full,
    )
    return cast(
        "list[TicketState]",
        _reads.list_by_query(query, repo_root=repo_root),
    )


def deps(ticket_id: str, *, repo_root=None) -> DepsGraph:
    """Dependency graph for a ticket (JSON)."""
    from rebar import _reads

    return cast("DepsGraph", _reads.deps(ticket_id, repo_root=repo_root))


def ready(*, sort: str | None = None, repo_root=None) -> list[TicketState]:
    """Tickets ready to work (all blockers closed).

    ``sort`` orders by ``priority|created|updated|id|status`` (``-`` prefix =
    descending; unset values last); the default keeps ready-order."""
    from rebar import _reads

    return _reads.ready(sort=sort, repo_root=repo_root)


def next_batch(epic_id: str, *, repo_root=None) -> NextBatch:
    """Next parallel batch of unblocked tickets under an epic's hierarchy (JSON).

    Runs in-process via the shared read plumbing, like every other read (Tier C
    retired the bash orchestrator)."""
    from rebar import _reads

    return cast("NextBatch", _reads.next_batch(epic_id, repo_root=repo_root))


def search(
    query: str,
    *,
    status: str | None = None,
    ticket_type: str | None = None,
    has_tag: str | None = None,
    include_archived: bool = False,
    sort: str | None = None,
    repo_root=None,
) -> list[TicketState]:
    """Full-text search over titles/descriptions/comments/tags (replay-derived).

    Returns a JSON list of matching ticket states (same element shape as
    :func:`list_tickets`). Plain whitespace-split terms match case-insensitively
    (AND). The query also accepts field predicates — ``status:``/``type:``/
    ``priority:``/``assignee:``/``tag:``/``parent:`` (comma = OR within a field,
    ``priority`` accepts ``<``/``<=``/``>``/``>=`` and ``n..m`` ranges), with
    ``-``/``not:`` negation; an unknown ``field:`` degrades to a literal
    substring. ``sort`` orders results by ``priority|created|updated|id|status``
    (``-`` prefix = descending; unset values last)."""
    from rebar import _reads

    return _reads.search(
        query,
        status=status,
        ticket_type=ticket_type,
        has_tag=has_tag,
        include_archived=include_archived,
        sort=sort,
        repo_root=repo_root,
    )


def recent_session_logs(*, limit: int = 5, repo_root=None) -> list[TicketState]:
    """The ``limit`` newest ``session_log`` tickets, newest first (by created_at).

    session_log tickets are hidden from :func:`list_tickets`; this is the
    type-specific read that surfaces them (same element shape as
    :func:`list_tickets`). ``limit`` defaults to 5; a non-positive ``limit``
    returns an empty list."""
    from rebar import _reads

    return cast("list[TicketState]", _reads.recent_session_logs(limit=limit, repo_root=repo_root))


# ── identity provider-neutral resolution seam (264f; read path, never raises) ──
def resolve_mapping(provider: str, external_id: str, *, repo_root=None) -> str | None:
    """Id of the identity whose ``mappings`` contains ``{provider, external_id}`` (exact
    match on the provider's opaque external id, NEVER email), else ``None``. Never raises."""
    from rebar._commands import identity as _identity

    return _identity.resolve_mapping(provider, external_id, repo_root=repo_root)


def is_placeholder(identity_id: str, *, repo_root=None) -> bool:
    """True iff ``identity_id`` is an identity whose compiled-state ``tags`` carries the
    ``placeholder`` marker (a ghost minted for an unmapped inbound user). An unknown id or
    non-identity ticket is ``False``. Never raises."""
    from rebar._commands import identity as _identity

    return _identity.is_placeholder(identity_id, repo_root=repo_root)


def jira_account_id(local_assignee: str, *, repo_root=None) -> str | None:
    """Resolve a LOCAL assignee/reporter string (identity ticket id or case-insensitive
    email) to its Jira accountId (``{provider:"jira"}`` external_id), else ``None``.
    Never raises."""
    from rebar._commands import identity as _identity

    return _identity.jira_account_id(local_assignee, repo_root=repo_root)


def identity_email(local_assignee: str, *, repo_root=None) -> str | None:
    """The matched identity's ``email`` (same id/email matching as
    :func:`jira_account_id`), else ``None``. Never raises."""
    from rebar._commands import identity as _identity

    return _identity.identity_email(local_assignee, repo_root=repo_root)


def fsck(*, recover: bool = False, report_only: bool = False, repo_root=None) -> str:
    """Run store integrity checks. ``recover=True`` runs the destructive recovery
    path. ``report_only=True`` suppresses fsck's only mutation — removing a stale
    ``.git/index.lock`` — so a read-only surface (MCP under REBAR_MCP_READONLY)
    can run plain fsck without any git-state write (the stale lock is reported,
    not removed)."""
    if recover:
        # In-process fsck-recover (Tier E E4). report_only has no effect on the
        # recover path (it has no index.lock mutation toggle); preserved for API
        # compatibility. Output captured; exit!=0 raises (prior _ok contract).
        import contextlib as _ctx
        import io as _io

        from rebar._commands import fsck_recover as _fr

        _out, _err = _io.StringIO(), _io.StringIO()
        with _ctx.redirect_stdout(_out), _ctx.redirect_stderr(_err):
            _rc = _fr.fsck_recover_cli([], repo_root=repo_root)
        if _rc != 0:
            raise RebarError(
                f"rebar fsck failed (exit {_rc}): {(_err.getvalue() or _out.getvalue()).strip()}",
                returncode=_rc,
                stderr=_err.getvalue(),
            )
        return _out.getvalue()

    # In-process fsck (Tier E E4). Output is captured; exit!=0 (issues found) raises,
    # preserving the prior _ok(_run(...)) contract.
    import contextlib
    import io

    from rebar._commands import fsck as _fsck_mod

    # Read-only surfaces (report_only, e.g. list/show) pass no_mutate=True directly,
    # so the scan never deletes the stale .git/index.lock — no os.environ round-trip.
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = _fsck_mod.fsck_cli([], repo_root=repo_root, no_mutate=report_only)
    if rc != 0:
        raise RebarError(
            f"rebar fsck failed (exit {rc}): {(err.getvalue() or out.getvalue()).strip()}",
            returncode=rc,
            stderr=err.getvalue(),
        )
    return out.getvalue()


# NOTE: the deprecated ``rebar.list_epics()`` library function (DE7), the CLI
# ``list-epics`` command, and the MCP ``list_epics`` tool were all removed pre-1.0
# (the last two in ticket 5899). Compose the primitives directly instead::
#
#     rebar.list_tickets(ticket_type="epic", status="open,in_progress",
#                        blocking_state="unblocked", min_children=N)
#     rebar.list_tickets(ticket_type="bug", priority=0)
