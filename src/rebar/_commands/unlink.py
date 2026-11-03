"""Tier B ``unlink`` port (docs/bash-migration.md §4) — the net-effective UNLINK.

``unlink`` removes a net-active LINK between the ordered pair by writing an UNLINK
event carrying that LINK's uuid. With no relation it is pair-scoped — it removes
the most-recently-created net-active LINK between the pair. With an explicit
relation (bug e39f) removal is
RELATION-SCOPED: exactly the named relation's net-active link is removed, leaving
any other relation the pair holds untouched — symmetric with the write side,
where ``graph/_links.add_dependency`` keys idempotency on ``(target_id,
relation)``. Both forms share the net-effective replay (``_get_link_info``:
replay LINK/UNLINK chronologically, with a SNAPSHOT compiled_state fallback for
compacted links) and the reciprocal UNLINK for relates_to. The UNLINK write
routes through the shared seam (same locked write path), and the active-link
check reuses rebar.graph's ``_is_active_link`` so the two stay in lockstep.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from rebar._commands._seam import CommandError, append_event, tracker_dir
from rebar._ids import resolve_ticket_id
from rebar.reducer._sort import prefix_ts as _prefix_ts

_USAGE = (
    "Usage: ticket link <source_id> <target_id> <relation>   (relation REQUIRED)\n"
    "       ticket unlink <source> <target> [relation]   (no relation: pair-scoped,\n"
    "                                          removes the most-recent link between the pair;\n"
    "                                          with relation: removes that relation's link)\n"
    "\n"
    "  relation: blocks | depends_on | relates_to | duplicates | supersedes | "
    "discovered_from | caused_by\n"
    "  relates_to creates bidirectional LINK events in both ticket dirs\n"
    "  duplicates, supersedes, discovered_from, caused_by are directional (no reciprocal link)"
)

_EVENT_ORDER = {"LINK": 0, "UNLINK": 1}


def _replay_links(ticket_dir: Path) -> tuple[dict[str, tuple[str, str]], set[str]]:
    """Replay LINK/UNLINK events chronologically: net-active ``{uuid: (target, relation)}``.

    ``UNLINK.data.link_uuid`` cancels the LINK with that uuid. The stored ``target_id`` is
    returned raw, exactly as written; the dict's insertion order is chronological. Also
    returns every cancelled uuid, for the SNAPSHOT fallback.
    """
    events = [("LINK", f) for f in sorted(ticket_dir.glob("*-LINK.json"))]
    events += [("UNLINK", f) for f in sorted(ticket_dir.glob("*-UNLINK.json"))]
    events.sort(key=lambda x: (_prefix_ts(x[1].name), _EVENT_ORDER.get(x[0], 99), x[1].name))

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
    return active, cancelled


def _snapshot_links(ticket_dir: Path, cancelled: set[str]) -> list[tuple[str, str, str]]:
    """``(uuid, target, relation)`` for SNAPSHOT compiled_state deps not later cancelled."""
    out: list[tuple[str, str, str]] = []
    for snap in sorted(ticket_dir.glob("*-SNAPSHOT.json")):
        try:
            with open(snap, encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            continue
        for dep in data.get("data", {}).get("compiled_state", {}).get("deps", []):
            duuid = dep.get("link_uuid", "")
            if duuid and duuid not in cancelled:
                out.append((duuid, dep.get("target_id", ""), dep.get("relation", "")))
    return out


def _stored_target_matches(stored: str, target_id: str, tracker: Path) -> bool:
    """True when a LINK's stored ``target_id`` names the ticket ``target_id``.

    ``process_link`` resolves a stored target at replay, so a link written with a short form
    (for example a unique 4-hex quad) is shown under the full id (jovial-subcranial-conch).
    The stored string matches when it equals ``target_id`` or resolves to it now. A stored
    target that is ambiguous, unknown, or resolves to a different ticket never matches.
    """
    if stored == target_id:
        return True
    if not stored:
        return False
    return resolve_ticket_id(stored, str(tracker), quiet=True) == target_id


def _find_link(
    ticket_dir: Path, target_id: str, relation: str | None = None
) -> tuple[str, str, str]:
    """Net-active ``(link_uuid, relation, stored_target)`` from source→target, or empties.

    Returns the most recent net-active LINK for target (``_replay_links``); falls back to a
    SNAPSHOT compiled_state.deps[] entry (compacted links), minus any cancelled uuids. A
    stored target matches by ``_stored_target_matches``; ``stored_target`` is the raw string.
    """
    if not ticket_dir.is_dir():
        return "", "", ""
    tracker = ticket_dir.parent
    active, cancelled = _replay_links(ticket_dir)
    found = ("", "", "")
    for uuid, (tid, rel) in active.items():
        if (relation is None or rel == relation) and _stored_target_matches(
            tid, target_id, tracker
        ):
            found = (uuid, rel, tid)  # last match wins (insertion order = chronological)
    if found[0]:
        return found
    for duuid, tid, drel in _snapshot_links(ticket_dir, cancelled):
        if (relation is None or drel == relation) and _stored_target_matches(
            tid, target_id, tracker
        ):
            return duuid, drel, tid
    return "", "", ""


def _get_link_info(
    ticket_dir: Path, target_id: str, relation: str | None = None
) -> tuple[str, str]:
    """Net-active ``(link_uuid, relation)`` from source→target, or ``("", "")``.

    ``relation`` narrows the search to exactly that relation (bug e39f): links are
    written keyed on ``(target_id, relation)``, so a pair can hold several
    relations; ``relation=None`` keeps the pair-scoped most-recent behavior. The match
    rules are ``_find_link``'s.
    """
    uuid, rel, _stored = _find_link(ticket_dir, target_id, relation)
    return uuid, rel


def _dangling_links(
    ticket_dir: Path, raw_target: str, relation: str | None
) -> list[tuple[str, str]]:
    """Net-active ``(link_uuid, relation)`` whose STORED target equals ``raw_target`` exactly.

    Chronological, live LINK events before compacted SNAPSHOT entries. Exact string equality
    is the safety property (lionly-squarish-xantus): a link stored with a full ticket id never
    equals a short or unknown input, so this cannot select a link to a real ticket.
    """
    if not ticket_dir.is_dir():
        return []
    active, cancelled = _replay_links(ticket_dir)
    links = [(u, t, r) for u, (t, r) in active.items()]
    seen = set(active)
    links += [link for link in _snapshot_links(ticket_dir, cancelled) if link[0] not in seen]
    return [
        (uuid, rel)
        for uuid, tid, rel in links
        if tid == raw_target and (relation is None or rel == relation)
    ]


def _write_unlink(
    source_id: str,
    target_id: str,
    tracker: Path,
    *,
    repo_root,
    relation: str | None = None,
) -> None:
    """Validate and append one UNLINK event (mirrors ``_write_unlink_event``)."""
    from rebar.graph._links import _is_active_link

    if not (tracker / ".env-id").is_file():
        raise CommandError("Error: ticket system not initialized. Run 'ticket init' first.")
    for tid in (source_id, target_id):
        if not (tracker / tid).is_dir():
            raise CommandError(f"Error: ticket '{tid}' does not exist")

    link_uuid, link_relation, stored_target = _find_link(tracker / source_id, target_id, relation)
    if not link_uuid:
        scope = f" with relation '{relation}'" if relation else ""
        raise CommandError(
            f"Error: no LINK event found in '{source_id}' targeting '{target_id}'{scope}"
        )
    if not _is_active_link(source_id, stored_target, link_relation, str(tracker)):
        raise CommandError(f"Error: no active link found between '{source_id}' and '{target_id}'")

    append_event(
        source_id,
        "UNLINK",
        {"link_uuid": link_uuid, "target_id": target_id},
        tracker,
        repo_root=repo_root,
    )


def unlink_core(id1_raw: str, id2_raw: str, relation: str | None = None, *, repo_root=None) -> dict:
    """Remove the net-active link id1→id2 (+ reciprocal for relates_to).

    With ``relation=None``, the pair-scoped unlink case: resolve both ids,
    unlink the most-recent id1→id2 link, and for a relates_to link also unlink the
    reciprocal id2→id1 (warning if it is an orphaned one-sided link). With an
    explicit ``relation`` (bug e39f), removes exactly that relation's net-active
    link — a pair holding several relations keeps the others. When ``id2_raw`` does
    not resolve to a ticket, a link whose stored target equals it exactly is removed
    instead (``_unlink_dangling``). Raises :class:`CommandError`.

    Returns ``{"source", "target", "relation"}`` — the resolved edge actually
    removed (ticket 6bda-9d58-8546-4638); the former ``None`` return was unconsumed
    (the library facade discards it), so the widening is additive.
    """
    if relation is not None:
        from rebar.graph._links import CANONICAL_RELATIONS

        if relation not in CANONICAL_RELATIONS:
            canonical_list = " | ".join(sorted(CANONICAL_RELATIONS))
            raise CommandError(
                f"Error: invalid relation '{relation}': must be one of {canonical_list}"
            )
    tracker = tracker_dir(repo_root)
    id1 = resolve_ticket_id(id1_raw, str(tracker))
    if id1 is None:
        raise CommandError(f"Error: ticket '{id1_raw}' does not exist")
    id2 = resolve_ticket_id(id2_raw, str(tracker), quiet=True)
    if id2 is None:
        return _unlink_dangling(id1, id2_raw, relation, tracker, repo_root=repo_root)

    _, link_relation = _get_link_info(tracker / id1, id2, relation)
    _write_unlink(id1, id2, tracker, repo_root=repo_root, relation=relation)

    if link_relation == "relates_to":
        recip_uuid, _ = _get_link_info(tracker / id2, id1, relation)
        if recip_uuid:
            _write_unlink(id2, id1, tracker, repo_root=repo_root, relation=relation)
        else:
            print(
                f"Warning: no reciprocal LINK found in '{id2}' targeting '{id1}' — "
                f"orphaned link, removed from '{id1}' only",
                file=sys.stderr,
            )
    return {"source": id1, "target": id2, "relation": link_relation}


def _unlink_dangling(
    source_id: str, raw_target: str, relation: str | None, tracker: Path, *, repo_root
) -> dict:
    """Remove a net-active link whose stored target is ``raw_target`` and resolves to nothing.

    Reached only when ``raw_target`` does not resolve to a ticket (lionly-squarish-xantus).
    The reducer keeps such a LINK's raw ``target_id`` (``process_link``) and an UNLINK cancels
    by ``link_uuid``, so the stored string is the only handle. With no match the resolver's
    diagnostic is replayed and the historical "does not exist" error stands. Matches spanning
    more than one relation without a relation selector are refused, never guessed. There is
    no reciprocal UNLINK: the target is not a ticket directory.
    """
    matches = _dangling_links(tracker / source_id, raw_target, relation)
    if not matches:
        resolve_ticket_id(raw_target, str(tracker))  # re-run loudly for its diagnostic
        raise CommandError(f"Error: ticket '{raw_target}' does not exist")
    relations = sorted({rel for _, rel in matches})
    if len(relations) > 1:
        raise CommandError(
            f"Error: '{source_id}' holds links to unresolvable target '{raw_target}' with "
            f"relations {', '.join(relations)}; name the relation to remove"
        )
    link_uuid, link_relation = matches[-1]
    append_event(
        source_id,
        "UNLINK",
        {"link_uuid": link_uuid, "target_id": raw_target},
        tracker,
        repo_root=repo_root,
    )
    print(
        f"Note: target '{raw_target}' does not resolve to a ticket; removed the stored "
        f"{link_relation} link {link_uuid} by its exact target string",
        file=sys.stderr,
    )
    return {"source": source_id, "target": raw_target, "relation": link_relation}


def unlink_cli(argv: list[str], *, repo_root=None) -> int:
    """CLI route for ``unlink``."""
    from rebar._cli._parsers.core.writes import build_unlink

    # Parser of record; raw positional slicing is retained because unlink's ids are
    # permissive (option-looking and ``--`` are valid id/relation values).
    build_unlink(prog="rebar unlink").parse_known_args(argv)
    if len(argv) < 2:
        print(_USAGE, file=sys.stderr)
        return 1
    relation = argv[2] if len(argv) > 2 else None
    try:
        out = unlink_core(argv[0], argv[1], relation, repo_root=repo_root)
    except CommandError as exc:
        print(exc.message, file=sys.stderr)
        return exc.returncode
    from rebar._commands import _confirm

    edge = f"{out['source']} -/-> {out['target']} ({out['relation']})"
    _confirm.emit("unlinked", edge, "unlinked", f"unlinked {edge}")
    return 0
