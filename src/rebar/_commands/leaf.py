"""Tier B leaf-write commands, implemented in Python (history: docs/bash-migration.md §4).

Each function here validates and composes one leaf-write event in Python, then appends
it in-process through ``_seam.append_event`` → ``rebar._store.event_append.write_and_push``
(the single locked write path; the bash seam was retired in Tier D). Behaviour —
validation order, error strings, exit codes, and the event envelope — is pinned by the
interface-contract tests.

This module implements the leaf-write commands: the pure single-event appends
``comment`` (COMMENT), ``set_file_impact`` (FILE_IMPACT), ``set_verify_commands``
(VERIFY_COMMANDS), the state-reading leaf writes ``tag`` / ``untag`` (TAG/UNTAG)
and ``archive`` (ARCHIVE), plus ``declare_no_file_impact``. The larger
event-composers (create/edit/link/unlink/revert) live in sibling modules
(``composer.py`` / ``link_revert.py``).
"""

from __future__ import annotations

import json

from rebar._commands._seam import (
    CommandError,
    append_event,
    current_tags,
    require_id,
    require_not_ghost,
    tracker_dir,
    validate_tag_name,
)


def _jq_type(value) -> str:
    """JSON type name as ``jq 'type'`` reports it (for byte-identical error text)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def comment(
    ticket_id: str,
    body: str,
    *,
    source: dict | None = None,
    repo_root=None,
    allow_secret_pattern: str = "",
) -> str:
    """Append a COMMENT event (mirrors ``ticket_comment``).

    ``source`` (P1.2 import): optional per-comment provenance — recognised keys
    ``source_author`` and ``source_created_at`` are copied onto the COMMENT data
    when non-None, so the reducer can surface the original comment's author/time on
    an imported comment (the event itself records the importer + a fresh timestamp).

    ``allow_secret_pattern``: audited force override for the write-time secret screen
    (bug e7a9) — see :func:`rebar._commands._seam.append_event`.

    Returns the resolved ticket id (the CLI's confirmation subject, ticket
    6bda-9d58-8546-4638); pre-existing callers ignored the ``None`` return, so the
    widening is additive.
    """
    tracker = tracker_dir(repo_root)
    if not ticket_id:
        raise CommandError("Error: ticket_id must be non-empty")
    if not body:
        raise CommandError("Error: comment body must be non-empty")
    resolved = require_id(ticket_id, tracker)
    require_not_ghost(resolved, tracker)
    data: dict = {"body": body}
    if source:
        for _src_key in ("source_author", "source_created_at"):
            _src_val = source.get(_src_key)
            if _src_val is not None:
                data[_src_key] = _src_val
    append_event(
        resolved,
        "COMMENT",
        data,
        tracker,
        repo_root=repo_root,
        allow_secret_pattern=allow_secret_pattern,
    )
    return resolved


def tag(ticket_id: str, tag_value: str, *, repo_root=None) -> dict:
    """Add a tag via a TAG_DELTA event (P2.3; was a whole-field EDIT clobber).

    Idempotent: adding an already-present tag is a no-op (exit 0, no event). No
    ghost check — matches the bash path, which resolves the id then tags. Emits an
    add delta so concurrent adds on other clones converge instead of clobbering.

    Returns the small wrote-vs-noop outcome ``{"wrote", "id", "tag"}`` (ticket
    6bda-9d58-8546-4638); pre-existing callers ignored the ``None`` return, so the
    widening is additive.
    """
    from rebar.reducer._version import TAG_DELTA

    tracker = tracker_dir(repo_root)
    if not ticket_id or not tag_value:
        raise CommandError("Error: ticket_id and tag must be non-empty")
    tag_value = validate_tag_name(tag_value)
    resolved = require_id(ticket_id, tracker)
    tags = current_tags(resolved, tracker)
    if tag_value in tags:
        return {"wrote": False, "id": resolved, "tag": tag_value}
    append_event(
        resolved,
        TAG_DELTA,
        {"added": [tag_value], "removed": []},
        tracker,
        repo_root=repo_root,
        author_fallback="unknown",
    )
    return {"wrote": True, "id": resolved, "tag": tag_value}


def untag(ticket_id: str, tag_value: str, *, repo_root=None) -> dict:
    """Remove a tag via a TAG_DELTA event (P2.3; was a whole-field EDIT clobber).

    Idempotent: removing an absent tag is a no-op (exit 0, no event). Returns the
    ``{"wrote", "id", "tag"}`` outcome, exactly as :func:`tag`.
    """
    from rebar.reducer._version import TAG_DELTA

    tracker = tracker_dir(repo_root)
    if not ticket_id or not tag_value:
        raise CommandError("Error: ticket_id and tag must be non-empty")
    tag_value = validate_tag_name(tag_value)
    resolved = require_id(ticket_id, tracker)
    tags = current_tags(resolved, tracker)
    if tag_value not in tags:
        return {"wrote": False, "id": resolved, "tag": tag_value}
    append_event(
        resolved,
        TAG_DELTA,
        {"added": [], "removed": [tag_value]},
        tracker,
        repo_root=repo_root,
        author_fallback="unknown",
    )
    return {"wrote": True, "id": resolved, "tag": tag_value}


def _terminal_fold(resolved: str, ticket_dir, repo_root) -> None:
    """Fold the ticket's entire live log into a SNAPSHOT before it is archived.

    Reuses the single-ticket fold path with the incremental gates bypassed —
    ``--threshold=0`` (any unfolded event qualifies) and ``--horizon=0`` (a fold horizon of
    *now*: nothing is too young) — because an archive is TERMINAL: the maintenance walks skip
    archived tickets by default, so whatever the fold leaves live would never be folded again.
    ``--skip-sync`` defers the push to the ARCHIVED event's own write_and_push.

    No-op when nothing is unfolded (no empty or duplicate SNAPSHOT — the guard asks the
    fold's own selection question via :func:`compact._foldable_event_count`, at horizon 0).
    A failed fold aborts the archive so an archived ticket NEVER carries an unfolded tail."""
    import contextlib
    import io

    from rebar._commands import compact
    from rebar._store import hlc

    if compact._foldable_event_count(str(ticket_dir), hlc.physical_now(), 0) == 0:
        return
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        rc = compact.compact_cli(
            [resolved, "--threshold=0", "--horizon=0", "--skip-sync"], repo_root=repo_root
        )
    if rc != 0:
        detail = out.getvalue().strip()
        raise CommandError(
            f"Error: terminal fold failed for ticket '{resolved}' (rc {rc}); not archived"
            + (f"\n{detail}" if detail else "")
        )


def archive(ticket_id: str, *, repo_root=None) -> dict:
    """Archive an open ticket (mirrors ``ticket_archive``).

    Idempotent: an existing ``.archived`` marker or ARCHIVED event short-circuits
    to a no-op (writing the marker if only the event was present, e.g. after
    a clone). Status-gated: only ``open`` tickets may be archived. On success folds
    the live log terminally (see :func:`_terminal_fold`), then writes an ARCHIVED
    event and the ``.archived`` marker.

    Returns the ``{"wrote", "id"}`` outcome (ticket 6bda-9d58-8546-4638); the CLI
    prints the confirmation from it (the former in-seam ``Archived ticket`` print
    moved there, so library/MCP callers no longer get a stray stdout line).
    """
    from rebar.reducer import reduce_ticket
    from rebar.reducer.marker import write_marker

    tracker = tracker_dir(repo_root)
    if not ticket_id:
        raise CommandError("Error: ticket_id must be non-empty")
    resolved = require_id(ticket_id, tracker)
    ticket_dir = tracker / resolved

    if (ticket_dir / ".archived").exists():
        return {"wrote": False, "id": resolved}
    if ticket_dir.is_dir() and any(p.name.endswith("-ARCHIVED.json") for p in ticket_dir.iterdir()):
        write_marker(str(ticket_dir))
        return {"wrote": False, "id": resolved}

    status = (reduce_ticket(str(ticket_dir)) or {}).get("status", "")
    if not status:
        raise CommandError(f"Error: could not read status for ticket '{resolved}'")
    if status != "open":
        raise CommandError(
            f"Error: ticket '{resolved}' has status '{status}'; archive only works on open tickets"
        )

    _terminal_fold(resolved, ticket_dir, repo_root)
    append_event(resolved, "ARCHIVED", {}, tracker, repo_root=repo_root)
    write_marker(str(ticket_dir))
    return {"wrote": True, "id": resolved}


def _validate_json_array(payload: str, label: str, required_keys: tuple[str, ...]):
    """Parse + validate a JSON-array payload of objects with string keys.

    Reproduces the bash ``jq``-based validation order and error strings used by
    ``ticket_set_file_impact`` / ``ticket_set_verify_commands``: valid JSON →
    array type → per-element object-with-string-keys, returning the parsed list.
    """
    try:
        parsed = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        raise CommandError(f"Error: {label} argument is not valid JSON") from None
    if not isinstance(parsed, list):
        raise CommandError(
            f"Error: {label} argument must be a JSON array, got '{_jq_type(parsed)}'"
        )
    keylist = '", "'.join(required_keys)
    for idx, elem in enumerate(parsed):
        if not isinstance(elem, dict) or any(
            not isinstance(elem.get(k), str) for k in required_keys
        ):
            raise CommandError(
                f"Error: {label}[{idx}] is invalid — every element must be an "
                f'object with string keys "{keylist}"'
            )
    return parsed


def _validate_no_file_impact_reason(reason: str) -> None:
    """Require an auditable explanation for an explicit no-impact declaration."""
    if not isinstance(reason, str) or len("".join(reason.split())) < 10:
        raise CommandError(
            "Error: no-file-impact reason must contain at least 10 non-whitespace characters",
            returncode=2,
        )


def set_file_impact(
    ticket_id: str, json_array: str, *, no_file_impact_reason: str | None = None, repo_root=None
) -> dict:
    """Append a FILE_IMPACT event, retaining legacy bytes unless declaring none.

    Returns ``{"id", "count", "none_declared"}`` — the confirmation subject +
    path count (ticket 6bda-9d58-8546-4638); the former ``None`` return had no
    consumers, so the widening is additive.
    """
    tracker = tracker_dir(repo_root)
    if not ticket_id:
        raise CommandError("Error: ticket_id must be non-empty")
    file_impact = _validate_json_array(json_array, "file_impact", ("path", "reason"))
    if no_file_impact_reason is not None:
        _validate_no_file_impact_reason(no_file_impact_reason)
    resolved = require_id(ticket_id, tracker)
    require_not_ghost(resolved, tracker)
    data: dict = {"file_impact": file_impact}
    if no_file_impact_reason is not None:
        data.update(
            {
                "file_impact_scope": "none",
                "no_file_impact_reason": no_file_impact_reason,
            }
        )
    append_event(resolved, "FILE_IMPACT", data, tracker, repo_root=repo_root)
    return {
        "id": resolved,
        "count": len(file_impact),
        "none_declared": no_file_impact_reason is not None,
    }


def declare_no_file_impact(ticket_id: str, reason: str, *, repo_root=None) -> dict:
    """Explicitly declare that a ticket touches no repository file."""
    return set_file_impact(ticket_id, "[]", no_file_impact_reason=reason, repo_root=repo_root)


def set_verify_commands(ticket_id: str, json_array: str, *, repo_root=None) -> tuple[str, int]:
    """Append a VERIFY_COMMANDS event (mirrors ``ticket_set_verify_commands``).

    Returns ``(resolved_id, command_count)`` for the CLI confirmation (ticket
    6bda-9d58-8546-4638); the former ``None`` return had no consumers."""
    tracker = tracker_dir(repo_root)
    if not ticket_id:
        raise CommandError("Error: ticket_id must be non-empty")
    verify_commands = _validate_json_array(
        json_array, "verify_commands", ("dd_id", "dd_text", "command")
    )
    resolved = require_id(ticket_id, tracker)
    require_not_ghost(resolved, tracker)
    append_event(
        resolved,
        "VERIFY_COMMANDS",
        {"verify_commands": verify_commands},
        tracker,
        repo_root=repo_root,
    )
    return resolved, len(verify_commands)
