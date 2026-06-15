"""Tier B ``unlink`` port (docs/bash-migration.md §4) — the net-effective UNLINK.

``unlink`` is pair-scoped (no relation arg): it removes the most-recently-created
net-active LINK between the ordered pair by writing an UNLINK event carrying that
LINK's uuid. This mirrors ticket-link.sh's unlink path — the net-effective replay
(``_get_link_info``: replay LINK/UNLINK chronologically, with a SNAPSHOT
compiled_state fallback for compacted links) and the reciprocal UNLINK for
relates_to. The UNLINK write routes through the shared seam (same locked write
path), and the active-link check reuses rebar.graph's ``_is_active_link`` so the
two stay in lockstep.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from rebar._commands._seam import CommandError, append_event, tracker_dir
from rebar._engine_support.resolver import resolve_ticket_id

_USAGE = (
    "Usage: ticket link <source_id> <target_id> <relation>   (relation REQUIRED)\n"
    "       ticket unlink <source> <target>   (pair-scoped, NO relation arg;\n"
    "                                          removes the most-recent link between the pair)\n"
    "\n"
    "  relation: blocks | depends_on | relates_to | duplicates | supersedes | discovered_from\n"
    "  relates_to creates bidirectional LINK events in both ticket dirs\n"
    "  duplicates, supersedes, discovered_from are directional (no reciprocal link)"
)

_EVENT_ORDER = {"LINK": 0, "UNLINK": 1}


def _get_link_info(ticket_dir: Path, target_id: str) -> tuple[str, str]:
    """Net-active ``(link_uuid, relation)`` from source→target, or ``("", "")``.

    Replays LINK/UNLINK events chronologically (UNLINK.data.link_uuid cancels the
    LINK with that uuid), returning the most recent net-active link for target;
    falls back to a SNAPSHOT compiled_state.deps[] entry (compacted links), minus
    any cancelled uuids. Mirrors ticket-link.sh's ``_get_link_info``.
    """
    if not ticket_dir.is_dir():
        return "", ""
    events = [("LINK", f) for f in sorted(ticket_dir.glob("*-LINK.json"))]
    events += [("UNLINK", f) for f in sorted(ticket_dir.glob("*-UNLINK.json"))]
    events.sort(key=lambda x: (x[1].name.split("-")[0], _EVENT_ORDER.get(x[0], 99), x[1].name))

    active: dict[str, tuple[str, str]] = {}
    cancelled: set[str] = set()
    for ev_type, f in events:
        try:
            with open(f, encoding="utf-8") as fh:
                ev = json.load(fh)
        except (json.JSONDecodeError, OSError):
            continue
        data = ev.get("data", {})
        uuid = ev.get("uuid", "")
        if ev_type == "LINK":
            if uuid:
                active[uuid] = (
                    data.get("target_id", data.get("target", "")),
                    data.get("relation", ""),
                )
        else:
            link_uuid = data.get("link_uuid", "")
            if link_uuid:
                cancelled.add(link_uuid)
                active.pop(link_uuid, None)

    found = ("", "")
    for uuid, (tid, rel) in active.items():
        if tid == target_id:
            found = (uuid, rel)  # last match wins (insertion order = chronological)
    if found[0]:
        return found

    for snap in sorted(ticket_dir.glob("*-SNAPSHOT.json")):
        try:
            with open(snap, encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            continue
        for dep in data.get("data", {}).get("compiled_state", {}).get("deps", []):
            duuid = dep.get("link_uuid", "")
            if dep.get("target_id", "") == target_id and duuid and duuid not in cancelled:
                return duuid, dep.get("relation", "")
    return "", ""


def _write_unlink(source_id: str, target_id: str, tracker: Path, *, repo_root) -> None:
    """Validate and append one UNLINK event (mirrors ``_write_unlink_event``)."""
    from rebar.graph._links import _is_active_link

    if not (tracker / ".env-id").is_file():
        raise CommandError("Error: ticket system not initialized. Run 'ticket init' first.")
    for tid in (source_id, target_id):
        if not (tracker / tid).is_dir():
            raise CommandError(f"Error: ticket '{tid}' does not exist")

    link_uuid, link_relation = _get_link_info(tracker / source_id, target_id)
    if not link_uuid:
        raise CommandError(f"Error: no LINK event found in '{source_id}' targeting '{target_id}'")
    if not _is_active_link(source_id, target_id, link_relation, str(tracker)):
        raise CommandError(f"Error: no active link found between '{source_id}' and '{target_id}'")

    append_event(
        source_id,
        "UNLINK",
        {"link_uuid": link_uuid, "target_id": target_id},
        tracker,
        repo_root=repo_root,
    )


def unlink_core(id1_raw: str, id2_raw: str, *, repo_root=None) -> None:
    """Remove the net-active link id1→id2 (+ reciprocal for relates_to).

    Mirrors ticket-link.sh's unlink case: resolve both ids, unlink id1→id2, and for
    a relates_to link also unlink the reciprocal id2→id1 (warning if it is an
    orphaned one-sided link). Raises :class:`CommandError`.
    """
    tracker = tracker_dir(repo_root)
    id1 = resolve_ticket_id(id1_raw, str(tracker))
    if id1 is None:
        raise CommandError(f"Error: ticket '{id1_raw}' does not exist")
    id2 = resolve_ticket_id(id2_raw, str(tracker))
    if id2 is None:
        raise CommandError(f"Error: ticket '{id2_raw}' does not exist")

    _, link_relation = _get_link_info(tracker / id1, id2)
    _write_unlink(id1, id2, tracker, repo_root=repo_root)

    if link_relation == "relates_to":
        recip_uuid, _ = _get_link_info(tracker / id2, id1)
        if recip_uuid:
            _write_unlink(id2, id1, tracker, repo_root=repo_root)
        else:
            print(
                f"Warning: no reciprocal LINK found in '{id2}' targeting '{id1}' — "
                f"orphaned link, removed from '{id1}' only",
                file=sys.stderr,
            )


def unlink_cli(argv: list[str], *, repo_root=None) -> int:
    """Dispatcher Python route for ``unlink``."""
    if len(argv) < 2:
        print(_USAGE, file=sys.stderr)
        return 1
    try:
        unlink_core(argv[0], argv[1], repo_root=repo_root)
    except CommandError as exc:
        print(exc.message, file=sys.stderr)
        return exc.returncode
    return 0
