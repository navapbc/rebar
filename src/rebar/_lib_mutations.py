"""rebar library — post-genesis mutation surface (leaf writes, session logs, store).

Split out of ``rebar._lib_writes`` by concern (ticket 4631-5598-7127-4a56), which
had grown to seven concerns against the 800-line module cap. This module holds the
three that mutate a ticket AFTER it exists:

* the Tier B leaf-write adapter ``_python_leaf`` and its seven callers (``comment``,
  ``edit_ticket``, ``link``, ``unlink``, ``tag``, ``untag``, ``archive``) — one
  adapter, seven call sites, the whole call graph inside this module;
* the ``session_log`` convenience surface, which maps ``CommandError`` itself rather
  than routing through ``_python_leaf``;
* store maintenance (``compact``, ``attach_commits``).

Every name here is re-exported from ``rebar._lib_writes`` and from the ``rebar``
package facade, so ``rebar.<name>``, ``rebar._lib_writes.<name>`` and
``rebar._python_leaf`` all keep resolving.
"""

from __future__ import annotations

from typing import Any

from rebar._commands.gates import log_description_cap_warning as _warn_description_cap
from rebar._errors import RebarError


def _python_leaf(fn, *args, repo_root, what: str, **kwargs) -> Any:
    """Run a Tier B leaf write in-process — the sole path since the cutover.

    Tier B retired its kill-switch after the soak (docs/bash-migration.md §4); the
    library/MCP write surface now calls ``rebar._commands`` directly. A command
    failure is mapped onto RebarError so the exit-code contract is unchanged.
    Extra keyword arguments are forwarded verbatim to ``fn`` (e.g. ``source=`` for
    comment provenance).

    Returns whatever ``fn`` returns. Most leaf writes return None and their callers
    ignore this, so widening it is additive; ``link`` relies on it to carry the
    hierarchy-escalation record back out (bug fec5-d8bb-86cd-453e). Forking a
    separate helper for that one caller would have duplicated the
    ``CommandError -> RebarError`` mapping below, which is the single thing keeping
    the library's exit-code contract uniform.
    """
    from rebar._commands._seam import CommandError

    try:
        return fn(*args, repo_root=repo_root, **kwargs)
    except CommandError as exc:
        raise RebarError(
            f"rebar {what} failed (exit {exc.returncode}): {exc.message}",
            returncode=exc.returncode,
            stderr=exc.message,
        ) from None


def comment(
    ticket_id: str,
    body: str,
    *,
    source: dict | None = None,
    repo_root=None,
    allow_secret_pattern: str = "",
) -> None:
    """Append a comment. ``source`` (P1.2 import): optional per-comment provenance
    (``source_author``/``source_created_at``) preserved on the imported comment.

    ``allow_secret_pattern``: audited force override for the write-time secret screen
    (bug e7a9) — a non-empty reason lets a refused body through and is recorded on the
    event. Deliberately not exposed over MCP."""
    from rebar._commands import leaf

    _python_leaf(
        leaf.comment,
        ticket_id,
        body,
        source=source,
        repo_root=repo_root,
        allow_secret_pattern=allow_secret_pattern,
        what="comment",
    )


def append_session_log(
    entry: str,
    *,
    summary=None,
    relates_to=None,
    discovered_from=None,
    repo_root=None,
    _creation_channel: str = "python",
) -> dict:
    """Append ``entry`` to the current session_log, creating one on first use.

    A convenience over ``create`` + ``comment``: the first call creates a
    ``session_log`` (titled ``summary`` or a default) and records it as the
    current log via a local pointer; subsequent calls append to that same log.
    Optional ``relates_to`` / ``discovered_from`` link the log to the work it
    documents (blocking links remain refused). Returns
    ``{"id", "alias", "created"}``.

    ``_creation_channel`` is INTERNAL (see :func:`create_ticket`): defaults to
    ``"python"``; the MCP adapter passes ``"mcp"`` — it stamps the session_log's genesis
    CREATE when this call creates one."""
    from rebar._commands import session_log
    from rebar._commands._seam import CommandError

    try:
        return session_log.append(
            entry,
            summary=summary,
            relates_to=relates_to,
            discovered_from=discovered_from,
            repo_root=repo_root,
            creation_channel=_creation_channel,
        )
    except CommandError as exc:
        raise RebarError(exc.message, returncode=exc.returncode, stderr=exc.message) from None


def start_session_log(
    *,
    summary=None,
    relates_to=None,
    discovered_from=None,
    repo_root=None,
    _creation_channel: str = "python",
) -> dict:
    """Explicitly create a NEW session_log and make it the current one (rotating
    away from any prior log). Returns ``{"id", "alias"}``.

    ``_creation_channel`` is INTERNAL (see :func:`create_ticket`): defaults to
    ``"python"``; the MCP adapter passes ``"mcp"``."""
    from rebar._commands import session_log
    from rebar._commands._seam import CommandError

    try:
        return session_log.start(
            summary=summary,
            relates_to=relates_to,
            discovered_from=discovered_from,
            repo_root=repo_root,
            creation_channel=_creation_channel,
        )
    except CommandError as exc:
        raise RebarError(exc.message, returncode=exc.returncode, stderr=exc.message) from None


def edit_ticket(ticket_id: str, *, repo_root=None, **fields) -> str | None:
    """Edit ticket fields: title, priority, assignee, ticket_type, description.

    Tags (P2.3): use ``add_tags``/``remove_tags``/``set_tags`` (lists or CSV) to
    mutate via convergent TAG_DELTA deltas. (The ``tags=`` set-alias was removed
    pre-1.0 — DE7; it is now rejected as an unknown field.) Returns the save-time
    description-cap warning (``None`` when silent; ALSO logged), for MCP to surface.
    """
    tag_add = fields.pop("add_tags", None)
    tag_remove = fields.pop("remove_tags", None)
    tag_set = fields.pop("set_tags", None)
    normalized = {}
    for key, value in fields.items():
        if value is None:
            continue
        # Scalars are str-coerced (edit_core parses them from strings); list-valued
        # fields (repos) must pass through intact — str()-ing a list corrupts it into
        # "['a', 'b']", which the EDIT repos normaliser then comma-splits into garbage.
        normalized[key] = value if isinstance(value, list) else str(value)
    from rebar._commands import composer

    warning = _python_leaf(
        composer.edit_core,
        ticket_id,
        normalized,
        repo_root=repo_root,
        what="edit",
        tag_add=tag_add,
        tag_remove=tag_remove,
        tag_set=tag_set,
    )
    return _warn_description_cap(warning)


def link(id1: str, id2: str, relation: str, *, repo_root=None) -> dict | None:
    """Link two tickets.

    ``relation`` must be one of the seven canonical relations: blocks, depends_on,
    relates_to, duplicates, supersedes, discovered_from, caused_by.

    Returns the REDIRECT record when hierarchy escalation recorded a DIFFERENT pair
    than the one asked for, else None. The CLI prints that record; this path cannot
    (stdout is suppressed so rebar-mcp's stdio JSON-RPC stream stays intact), so
    returning it is how a library or MCP caller learns the substitution happened
    instead of believing the requested edge was written (bug 1803-df54-18bb-4881).
    """
    from rebar._commands import composer

    def _link(i, j, rel, *, repo_root):
        return composer.link_core(i, j, rel, repo_root=repo_root, quiet=True)

    return _python_leaf(_link, id1, id2, relation, repo_root=repo_root, what="link")


def unlink(id1: str, id2: str, relation: str | None = None, *, repo_root=None) -> None:
    from rebar._commands import unlink as _unlink_cmd

    _python_leaf(_unlink_cmd.unlink_core, id1, id2, relation, repo_root=repo_root, what="unlink")


def tag(ticket_id: str, tag: str, *, repo_root=None) -> None:
    from rebar._commands import leaf

    _python_leaf(leaf.tag, ticket_id, tag, repo_root=repo_root, what="tag")


def untag(ticket_id: str, tag: str, *, repo_root=None) -> None:
    from rebar._commands import leaf

    _python_leaf(leaf.untag, ticket_id, tag, repo_root=repo_root, what="untag")


def archive(ticket_id: str, *, repo_root=None) -> None:
    from rebar._commands import leaf

    _python_leaf(leaf.archive, ticket_id, repo_root=repo_root, what="archive")


def compact(ticket_id: str | None = None, *, repo_root=None) -> None:
    # In-process (Tier E E3): compact-on-id via the shared compaction core
    # (ticket-compact.sh retired from this path). Output is captured (the bash
    # library wrapper captured it too); failures raise RebarError.
    import contextlib
    import io

    from rebar._commands import compact as _compact

    out, err = io.StringIO(), io.StringIO()
    argv = [ticket_id] if ticket_id else []
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = _compact.compact_cli(argv, repo_root=repo_root)
    if rc != 0:
        raise RebarError(
            f"rebar compact failed (exit {rc}): {err.getvalue().strip()}",
            returncode=rc,
            stderr=err.getvalue(),
        )


def attach_commits(ticket_id: str, commits, *, repo_root=None) -> dict:
    """Attach commit SHAs to a ticket as a durable, union-merged ``commits`` list
    (epic a88f / WS-H). ``commits`` is a list of SHA strings or {sha, message?,
    author?, …} records. Convergent (union by sha) and NOT synced to Jira. Returns
    ``{ticket_id, attached}``."""
    from rebar._commands import _seam
    from rebar._commands._seam import CommandError
    from rebar._engine_support import commit_impact

    tracker = _seam.tracker_dir(repo_root)
    tid = _seam.require_id(ticket_id, tracker)
    _seam.require_not_ghost(tid, tracker)
    records = []
    for c in commits:
        if isinstance(c, str) and c:
            records.append({"sha": c})
        elif isinstance(c, dict) and c.get("sha"):
            records.append(c)
        else:
            raise RebarError(f"invalid commit entry {c!r}: need a sha string or {{sha, …}} dict")
    # ALL-OR-NOTHING: the WHOLE batch is validated before anything is appended, so one bad
    # SHA never leaves a half-recorded attachment. The CLI and the MCP tool inherit this by
    # construction — they all route through THIS seam.
    unresolvable = commit_impact.unresolvable_shas([r["sha"] for r in records], tracker)
    if unresolvable:
        raise RebarError(
            f"cannot attach commits: {', '.join(unresolvable)} did not resolve to a commit "
            "in this repository; nothing was recorded"
        )
    try:
        _seam.append_event(tid, "COMMITS", {"commits": records}, tracker, repo_root=repo_root)
    except CommandError as exc:
        raise RebarError(
            f"rebar attach-commits failed (exit {exc.returncode}): {exc.message}",
            returncode=exc.returncode,
            stderr=exc.message,
        ) from None
    return {"ticket_id": tid, "attached": len(records)}
