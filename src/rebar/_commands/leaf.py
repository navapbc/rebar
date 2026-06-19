"""Tier B leaf-write commands ported to Python (docs/bash-migration.md §4).

Each function here is the Python implementation of one ``ticket-lib-api.sh``
leaf-write command, reached behind ``REBAR_LEAF_WRITES=python``. They validate and
compose in Python, then append through the bash write seam (``_seam.append_event``
→ ``ticket-append-event.sh`` → ``write_commit_event``) so the locked write path is
unchanged until Tier D. Behaviour — validation order, error strings, exit codes,
and the event envelope — mirrors the bash functions byte-for-byte so the per-command
bash suite passes against either implementation.

Ported so far: ``comment`` (COMMENT), ``set_file_impact`` (FILE_IMPACT),
``set_verify_commands`` (VERIFY_COMMANDS) — the pure single-event appends. The
state-reading leaf writes (tag/untag, archive) and the larger event-composers
(create/edit/link/unlink/revert) are tracked as child tickets of the Tier B story.
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


def comment(ticket_id: str, body: str, *, source: dict | None = None, repo_root=None) -> None:
    """Append a COMMENT event (mirrors ``ticket_comment``).

    ``source`` (P1.2 import): optional per-comment provenance — recognised keys
    ``source_author`` and ``source_created_at`` are copied onto the COMMENT data
    when non-None, so the reducer can surface the original comment's author/time on
    an imported comment (the event itself records the importer + a fresh timestamp).
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
    append_event(resolved, "COMMENT", data, tracker, repo_root=repo_root)


def tag(ticket_id: str, tag_value: str, *, repo_root=None) -> None:
    """Add a tag via a TAG_DELTA event (P2.3; was a whole-field EDIT clobber).

    Idempotent: adding an already-present tag is a no-op (exit 0, no event). No
    ghost check — matches the bash path, which resolves the id then tags. Emits an
    add delta so concurrent adds on other clones converge instead of clobbering.
    """
    from rebar.reducer._version import TAG_DELTA

    tracker = tracker_dir(repo_root)
    if not ticket_id or not tag_value:
        raise CommandError("Error: ticket_id and tag must be non-empty")
    resolved = require_id(ticket_id, tracker)
    tags = current_tags(resolved, tracker)
    if tag_value in tags:
        return
    append_event(
        resolved,
        TAG_DELTA,
        {"added": [tag_value], "removed": []},
        tracker,
        repo_root=repo_root,
        author_fallback="unknown",
    )


def untag(ticket_id: str, tag_value: str, *, repo_root=None) -> None:
    """Remove a tag via a TAG_DELTA event (P2.3; was a whole-field EDIT clobber).

    Idempotent: removing an absent tag is a no-op (exit 0, no event).
    """
    from rebar.reducer._version import TAG_DELTA

    tracker = tracker_dir(repo_root)
    if not ticket_id or not tag_value:
        raise CommandError("Error: ticket_id and tag must be non-empty")
    resolved = require_id(ticket_id, tracker)
    tags = current_tags(resolved, tracker)
    if tag_value not in tags:
        return
    append_event(
        resolved,
        TAG_DELTA,
        {"added": [], "removed": [tag_value]},
        tracker,
        repo_root=repo_root,
        author_fallback="unknown",
    )


def archive(ticket_id: str, *, repo_root=None) -> None:
    """Archive an open ticket (mirrors ``ticket_archive``).

    Idempotent: an existing ``.archived`` marker or ARCHIVED event short-circuits
    to a silent no-op (writing the marker if only the event was present, e.g. after
    a clone). Status-gated: only ``open`` tickets may be archived. On success writes
    an ARCHIVED event, the ``.archived`` marker, and prints the confirmation line.
    """
    from rebar.reducer import reduce_ticket
    from rebar.reducer.marker import write_marker

    tracker = tracker_dir(repo_root)
    if not ticket_id:
        raise CommandError("Error: ticket_id must be non-empty")
    resolved = require_id(ticket_id, tracker)
    ticket_dir = tracker / resolved

    if (ticket_dir / ".archived").exists():
        return
    if ticket_dir.is_dir() and any(p.name.endswith("-ARCHIVED.json") for p in ticket_dir.iterdir()):
        write_marker(str(ticket_dir))
        return

    status = (reduce_ticket(str(ticket_dir)) or {}).get("status", "")
    if not status:
        raise CommandError(f"Error: could not read status for ticket '{resolved}'")
    if status != "open":
        raise CommandError(
            f"Error: ticket '{resolved}' has status '{status}'; archive only works on open tickets"
        )

    append_event(resolved, "ARCHIVED", {}, tracker, repo_root=repo_root)
    write_marker(str(ticket_dir))
    print(f"Archived ticket '{resolved}'")


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


def set_file_impact(ticket_id: str, json_array: str, *, repo_root=None) -> None:
    """Append a FILE_IMPACT event (mirrors ``ticket_set_file_impact``)."""
    tracker = tracker_dir(repo_root)
    if not ticket_id:
        raise CommandError("Error: ticket_id must be non-empty")
    file_impact = _validate_json_array(json_array, "file_impact", ("path", "reason"))
    resolved = require_id(ticket_id, tracker)
    require_not_ghost(resolved, tracker)
    append_event(
        resolved, "FILE_IMPACT", {"file_impact": file_impact}, tracker, repo_root=repo_root
    )


def set_verify_commands(ticket_id: str, json_array: str, *, repo_root=None) -> None:
    """Append a VERIFY_COMMANDS event (mirrors ``ticket_set_verify_commands``)."""
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
