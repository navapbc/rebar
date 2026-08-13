"""Tier B event-composer EDIT leaf helpers (docs/bash-migration.md §4).

Extracted from ``composer.py`` along its existing call-graph seam (module-size
policy): the pure, near-pure, and lazily-dependent helper cluster that
``composer.edit_core`` composes from — tag-list normalisation, the EDIT ``repos``
normaliser, the promote-only ``bridge_project`` guard, the TAG_DELTA compiler, the
save-time description-cap notice, and the ``--parent`` validation cascade.

This module is a LEAF: it imports only the shared command seam and the resolver
(plus in-function lazy imports), and NEVER imports ``composer`` — so ``composer``
can import these helpers at module top with no import cycle. ``edit_core`` and
``edit_cli`` themselves live in ``composer`` (they carry the locked
complexity-baseline key ``composer.py::edit_core`` and must not be re-keyed).
"""

from __future__ import annotations

import os

from rebar._commands._seam import (
    CommandError,
    append_event,
    validate_tag_name,
)
from rebar._engine_support.resolver import resolve_ticket_id


def _parse_tag_list(value, *, validate: bool) -> list[str]:
    """Normalise a tag spec (CSV string or list) to a deduped, trimmed tag list.

    ``validate`` rejects empty/whitespace-only/control-char names via the shared
    :func:`validate_tag_name` (applied to tags ENTERING state — adds/sets);
    removals skip it (you may legitimately remove a previously-malformed tag, and
    an empty token there is just dropped). Order-preserving dedup.
    """
    if value is None:
        return []
    items = value.split(",") if isinstance(value, str) else list(value)
    out: list[str] = []
    for raw in items:
        t = str(raw).strip()
        if not t:
            continue  # CSV cleanliness: empty tokens (a,,b / --set-tags="") dropped
        if validate:
            t = validate_tag_name(t)  # non-empty here, so only control-char check fires
        if t not in out:
            out.append(t)
    return out


def _edit_repos_list(raw_value) -> list[str]:
    """Normalise an EDIT ``repos`` field value (CSV string or list) to a trimmed list.

    Split out of :func:`composer.edit_core` (at its locked complexity ceiling). Mirrors
    the CREATE path but str()-coerces list items and tolerates a ``None`` list, matching
    the prior inline behaviour exactly.
    """
    return (
        [r.strip() for r in raw_value.split(",") if r.strip()]
        if isinstance(raw_value, str)
        else [str(r) for r in (raw_value or []) if r]
    )


def _enforce_promote_only(out: dict, resolved: str, tracker) -> None:
    """Enforce the story-cef7 promote-only rule for ``bridge_project`` on an EDIT.

    Split out of :func:`composer.edit_core` (at its locked complexity ceiling).
    ``bridge_project`` may be set on an UNBOUND ticket but never changed once the ticket
    already holds a tracker binding — a bound ticket's sync target is fixed.
    ``binding_jira_key_map`` returns ``{local_id: jira_key}``; membership of the resolved
    id means it is bound. ``repos`` has no such guard (freely editable). Raises
    :class:`CommandError` when the guard trips; a no-op when ``bridge_project`` is not
    being edited.
    """
    if "bridge_project" not in out:
        return
    from rebar._ids import binding_jira_key_map

    if resolved in binding_jira_key_map(str(tracker)):
        raise CommandError(
            f"Error: bridge_project is promote-only; ticket {resolved} already holds a binding"
        )


def _apply_tag_deltas(
    resolved: str,
    tracker,
    repo_root,
    has_set: bool,
    set_list: list[str],
    add_list: list[str],
    remove_list: list[str],
) -> None:
    """Compile the observed-vs-requested tag delta and append a TAG_DELTA event (P2.3).

    Split out of :func:`composer.edit_core` (at its locked complexity ceiling).
    ``has_set`` selects the wholesale-set compilation (add-wins: add what's missing,
    remove observed tags not in the target set) vs the add/remove no-op suppression (only
    add what's absent, only remove what's present). Appends nothing when the compiled
    delta is empty.
    """
    from rebar.reducer import reduce_ticket
    from rebar.reducer._version import TAG_DELTA

    observed = list((reduce_ticket(str(tracker / resolved)) or {}).get("tags") or [])
    if has_set:
        added = [t for t in set_list if t not in observed]
        removed = [t for t in observed if t not in set_list]
    else:
        added = [t for t in add_list if t not in observed]
        removed = [t for t in remove_list if t in observed]
    if added or removed:
        append_event(
            resolved,
            TAG_DELTA,
            {"added": added, "removed": removed},
            tracker,
            repo_root=repo_root,
        )


def _edit_description_warning(out: dict, resolved: str, tracker, reduce_ticket) -> str | None:
    """The save-time description-cap notice for an EDIT that wrote a description.

    Split out of :func:`composer.edit_core` (which sits at its locked complexity
    ceiling). The ticket type is the one the ticket has AFTER this edit, since the same
    call may change it. Returns ``None`` when no description was written, the description
    is within ``verify.max_ticket_description_chars``, or the plan-review start-work gate
    does not apply — see :func:`rebar._commands.gates.description_cap_warning`.
    """
    if "description" not in out:
        return None
    from rebar._commands.gates import description_cap_warning

    state = reduce_ticket(str(tracker / resolved)) or {}
    return description_cap_warning(
        out["description"],
        str(out.get("ticket_type") or state.get("ticket_type") or ""),
        ticket_id=str(state.get("alias") or resolved),
        cfg_root=os.path.dirname(str(tracker)),
    )


def _resolve_new_parent(value: str, ticket_id: str, tracker, reduce_ticket) -> str:
    """The ``--parent`` validation cascade; returns the resolved parent_id (or ""
    for the ``null`` detach sentinel)."""
    if value == "":
        raise CommandError(
            "Error: --parent requires a non-empty value (use --parent=null to detach)"
        )
    if value == "null":
        return ""
    new_parent = resolve_ticket_id(value, str(tracker))
    if not new_parent or not (tracker / new_parent).is_dir():
        raise CommandError(f"Error: parent ticket '{value}' does not exist")
    if new_parent == ticket_id:
        raise CommandError("Error: ticket cannot be its own parent")
    status = (reduce_ticket(str(tracker / new_parent)) or {}).get("status", "") or ""
    if status not in ("open", "in_progress"):
        if status == "":
            raise CommandError(
                f"Error: cannot verify status of parent ticket '{new_parent}' — refusing "
                f"to re-parent (fail-closed). Verify the ticket exists and is in an active "
                f"state, then retry."
            )
        raise CommandError(
            f"Error: cannot re-parent to {status} ticket '{new_parent}'. Reopen the parent "
            f"first with: ticket transition {new_parent} {status} open"
        )
    walk_id, count = new_parent, 0
    while walk_id and count < 64:
        walk_parent = (reduce_ticket(str(tracker / walk_id)) or {}).get("parent_id", "") or ""
        if not walk_parent or walk_parent == "None":
            break
        if walk_parent == ticket_id:
            raise CommandError(
                f"Error: cannot set parent — would create a cycle (ticket {ticket_id} is an "
                f"ancestor of {new_parent})"
            )
        walk_id = walk_parent
        count += 1
    return new_parent
