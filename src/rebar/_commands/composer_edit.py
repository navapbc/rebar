"""Tier B event-composer EDIT surface (docs/bash-migration.md §4).

Extracted from ``composer.py`` along its existing call-graph seam (module-size
policy): ``edit_core`` (validation + EDIT/TAG_DELTA append, shared by the library
facade), ``edit_cli`` (flag parsing + output), the ``_edit_govern`` parser-of-record
check, the EDIT field/flag vocabulary, and the helper cluster they compose from —
tag-list normalisation, the EDIT ``repos`` normaliser, the promote-only
``bridge_project`` guard, the TAG_DELTA compiler, the save-time description-cap
notice, the per-field coercion chain, and the ``--parent`` validation cascade.

This module is a LEAF: it imports only the shared command seam and the resolver
(plus in-function lazy imports), and NEVER imports ``composer`` — so ``composer``
can re-export ``edit_core``/``edit_cli`` at module top with no import cycle. That
re-export keeps every ``rebar._commands.composer.edit_*`` caller and monkeypatch
site working unchanged.
"""

from __future__ import annotations

import sys

from rebar._commands._seam import (
    CommandError,
    _warn_stderr,
    append_event,
    require_id,
    require_not_ghost,
    tracker_dir,
    validate_tag_name,
)
from rebar._engine_support.resolver import resolve_ticket_id
from rebar.types import TICKET_TYPES


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


def _edit_description_warning(
    out: dict, resolved: str, tracker, reduce_ticket, repo_root=None
) -> str | None:
    """The save-time description-cap notice for an EDIT that wrote a description.

    Split out of :func:`composer.edit_core` (which sits at its locked complexity
    ceiling). The ticket type is the one the ticket has AFTER this edit, since the same
    call may change it. Returns ``None`` when no description was written, the description
    is within ``verify.max_ticket_description_chars``, or the plan-review start-work gate
    does not apply — see :func:`rebar._commands.gates.description_cap_warning`.
    """
    if "description" not in out:
        return None
    from rebar import config as _config
    from rebar._commands.gates import description_cap_warning

    state = reduce_ticket(str(tracker / resolved)) or {}
    return description_cap_warning(
        out["description"],
        str(out.get("ticket_type") or state.get("ticket_type") or ""),
        ticket_id=str(state.get("alias") or resolved),
        # The config root — RESOLVED (explicit repo_root > REBAR_ROOT > git toplevel of
        # cwd), NOT os.path.dirname(tracker): the store is relocatable (REBAR_TRACKER_DIR),
        # where the tracker's parent has no config and the advisory silently vanished
        # (auspicial-friended-merganser sibling).
        cfg_root=str(_config.repo_root(repo_root)),
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


# Tags are NOT an EDIT field any more (P2.3): they mutate via TAG_DELTA deltas
# (--add-tag/--remove-tag/--set-tags), so a whole-field EDIT can never clobber a
# concurrent tag add. The library/MCP ``edit(tags=...)`` arg is a DEPRECATED alias
# for --set-tags, intercepted in edit_core before this field set is validated.
_EDIT_FIELDS = (
    "title",
    "priority",
    "assignee",
    "ticket_type",
    "description",
    "parent",
    "bridge_project",
    "repos",
)
_EDIT_USAGE = (
    "Usage: ticket edit <ticket_id> [--title=VALUE] [--priority=VALUE] [--assignee=VALUE] "
    "[--ticket_type=VALUE] [--description=VALUE] [--parent=VALUE] "
    "[--add-tag=t1,t2] [--remove-tag=t1,t2] [--set-tags=t1,t2] [--review]"
)

_TAG_FLAGS = ("add-tag", "remove-tag", "set-tags")

# Dashed CLI spellings mapped to their `_EDIT_FIELDS` state-field name (story cef7).
_EDIT_FLAG_ALIASES = {"bridge-project": "bridge_project"}


def _coerce_edit_fields(fields: dict, resolved: str, tracker, reduce_ticket) -> dict:
    """Validate + coerce an EDIT's ``--field=value`` pairs into the event's ``fields``.

    Extracted from :func:`edit_core` when the EDIT surface moved into this module
    (module-size policy): the per-field guard chain is its own cluster, and keeping it
    here holds ``edit_core`` under the complexity threshold so the move does not mint a
    new complexity-baseline key. Behaviour is unchanged — the same guards, the same
    messages, and the same ``parent`` -> ``parent_id`` key mapping.
    """
    out: dict = {}
    for key, raw_value in fields.items():
        # `repos` carries a list (or a CSV string) — normalise BEFORE the blanket str()
        # coercion below would stringify a list into its repr. Freely editable, no guard.
        if key == "repos":
            out["repos"] = _edit_repos_list(raw_value)
            continue
        value = "" if raw_value is None else str(raw_value)
        if key == "title":
            if value.strip() == "":
                raise CommandError(
                    "Error: --title requires a non-empty value (empty values silently "
                    "clobber the title; bug 4f50)"
                )
            out["title"] = value.replace("→", "->")
        elif key == "description":
            if value == "":
                raise CommandError(
                    "Error: --description requires a non-empty value (empty values "
                    "silently clobber prior content; bug e78f-9f79)"
                )
            out["description"] = value
        elif key == "priority":
            if value not in ("0", "1", "2", "3", "4"):
                raise CommandError(f"Error: invalid priority '{value}'. Must be 0-4")
            out["priority"] = int(value)
        elif key == "ticket_type":
            if value not in TICKET_TYPES:
                raise CommandError(
                    f"Error: invalid ticket type '{value}'. "
                    "Must be one of: bug, epic, story, task, session_log, code_review, identity"
                )
            out["ticket_type"] = value
        elif key == "parent":
            out["parent_id"] = _resolve_new_parent(value, resolved, tracker, reduce_ticket)
        else:  # assignee
            out[key] = value
    return out


def edit_core(
    ticket_id: str,
    fields: dict,
    *,
    tag_add=None,
    tag_remove=None,
    tag_set=None,
    repo_root=None,
) -> str | None:
    """Validate fields and append an EDIT event (mirrors ``ticket_edit``), plus tag
    add/remove/set deltas as a TAG_DELTA event (P2.3).

    Returns the save-time description-cap warning when this edit wrote a description
    over ``verify.max_ticket_description_chars`` and the plan-review start-work gate
    applies (else ``None``); callers emit it on their own channel. Advisory only — the
    edit has already been appended by then.

    Field guards: unknown-field reject, non-empty title/description, priority 0-4,
    ticket_type enum, and the ``--parent`` cascade (``null`` detaches; else resolve
    → exists → not-self → fail-closed status gate (open/in_progress only) → ancestor
    cycle walk), mapping ``parent`` → ``parent_id`` in the event. Title gets the
    U+2192→``->`` normalisation; numeric priority is stored as int.

    Tags: ``tag_add``/``tag_remove`` are add/remove deltas; ``tag_set`` (mutually
    exclusive with add/remove) is a wholesale set COMPILED to a delta against the
    locally-observed tags (add-wins: a concurrent unobserved remote add survives).
    (The ``edit_ticket(tags=...)`` set-alias was removed pre-1.0 — DE7; ``tags`` is
    now just an unknown field, so use ``set_tags``/``add_tags``/``remove_tags``.)
    """
    from rebar.reducer import reduce_ticket

    tracker = tracker_dir(repo_root)
    fields = dict(fields)

    for name in fields:
        if name not in _EDIT_FIELDS:
            raise CommandError(f"Error: unknown field '{name}'. Allowed: {' '.join(_EDIT_FIELDS)}")

    add_list = _parse_tag_list(tag_add, validate=True)
    remove_list = _parse_tag_list(tag_remove, validate=False)
    has_set = tag_set is not None
    set_list = _parse_tag_list(tag_set, validate=True) if has_set else []
    if has_set and (add_list or remove_list):
        raise CommandError("Error: --set-tags cannot be combined with --add-tag/--remove-tag")
    overlap = [t for t in add_list if t in remove_list]
    if overlap:
        raise CommandError(f"Error: tag(s) {overlap} given to both --add-tag and --remove-tag")
    has_tag_op = has_set or bool(add_list) or bool(remove_list)

    if not fields and not has_tag_op:
        raise CommandError("Error: at least one --field=value pair is required")
    if not (tracker / ".env-id").is_file():
        raise CommandError("Error: ticket system not initialized. Run 'ticket init' first.")
    resolved = require_id(ticket_id, tracker)
    require_not_ghost(resolved, tracker)

    out = _coerce_edit_fields(fields, resolved, tracker, reduce_ticket)

    # Promote-only guard (story cef7): `bridge_project` may be set on an UNBOUND ticket
    # but never changed once the ticket already holds a tracker binding.
    _enforce_promote_only(out, resolved, tracker)

    warning: str | None = None
    if out:
        append_event(resolved, "EDIT", {"fields": out}, tracker, repo_root=repo_root)
        warning = _edit_description_warning(out, resolved, tracker, reduce_ticket, repo_root)

    if has_tag_op:
        _apply_tag_deltas(resolved, tracker, repo_root, has_set, set_list, add_list, remove_list)

    return warning


def _edit_govern(ticket_id: str) -> int | None:
    """Parser-of-record governance for :func:`edit_cli`.

    edit's accepted grammar exceeds argparse (arbitrary field keys incl. ``repos``,
    which ``build_edit`` lacks), so the factory governs only via a minimal,
    always-argparse-valid argv — the ticket id after ``--`` so it is never read as an
    option. Returns a render exit code when a (rejecting) factory refuses it, else
    ``None`` to continue.
    """
    from rebar._cli._parser import ParseError, render_parse_error
    from rebar._cli._parsers.core.writes import build_edit

    try:
        build_edit(prog="rebar edit").parse_args(["--", ticket_id])
    except ParseError as exc:
        return render_parse_error(exc)
    return None


def edit_cli(argv: list[str], *, repo_root=None) -> int:
    """CLI route for ``edit``: parse ticket_id + --field pairs +
    tag-delta flags (--add-tag / --remove-tag / --set-tags)."""
    if len(argv) < 2:
        print(_EDIT_USAGE, file=sys.stderr)
        return 1
    ticket_id, rest = argv[0], argv[1:]
    # --review (story a114) is the one VALUELESS flag: pop it BEFORE the
    # --key=value field loop below (which would otherwise consume the next token
    # as its value). Handled after edit_core commits — see the tail of this function.
    review = "--review" in rest
    rest = [a for a in rest if a != "--review"]
    fields: dict = {}
    tag_add: list[str] = []
    tag_remove: list[str] = []
    tag_set: list[str] | None = None
    # Field NAMES this invocation set, in parse order (never values) — the
    # confirmation line's payload (ticket 6bda-9d58-8546-4638).
    set_names: list[str] = []

    def _accept_tag(name: str, val: str) -> None:
        nonlocal tag_set
        items = [t for t in val.split(",")]
        set_names.append(name)
        if name == "add-tag":
            tag_add.extend(items)
        elif name == "remove-tag":
            tag_remove.extend(items)
        else:  # set-tags
            tag_set = (tag_set or []) + items

    i, n = 0, len(rest)
    while i < n:
        arg = rest[i]
        if arg.startswith("--") and "=" in arg:
            name, val = arg[2:].split("=", 1)
            i += 1
        elif arg.startswith("--"):
            name = arg[2:]
            if i + 1 >= n:
                print(f"Error: --{name} requires a value", file=sys.stderr)
                return 1
            val = rest[i + 1]
            i += 2
        else:
            print(f"Error: unexpected argument '{arg}'", file=sys.stderr)
            return 1

        # `--bridge-project` reads more naturally with a dash but the state field (and
        # `_EDIT_FIELDS`) is `bridge_project`; accept the dashed spelling as an alias.
        name = _EDIT_FLAG_ALIASES.get(name, name)

        if name in _TAG_FLAGS:
            _accept_tag(name, val)
        elif name == "tags":
            print(
                "Error: --tags is no longer an edit field. Use --set-tags=t1,t2 to "
                "replace, or --add-tag / --remove-tag to mutate.",
                file=sys.stderr,
            )
            return 1
        elif name not in _EDIT_FIELDS:
            print(
                f"Error: unknown field '{name}'. Allowed: {' '.join(_EDIT_FIELDS)}",
                file=sys.stderr,
            )
            return 1
        else:
            fields[name] = val
            set_names.append(name)
    # Parser of record. edit's accepted grammar is genuinely not argparse-expressible
    # without a behavior delta: it accepts arbitrary ``--<field>`` keys validated
    # against ``_EDIT_FIELDS`` — which includes ``repos``, a field ``build_edit`` has no
    # argument for, so feeding the extracted fields back through the factory would
    # itself raise — plus dashed-alias mapping, tag-delta comma-accumulation, the
    # valueless ``--review``, and bespoke ``requires a value``/``unexpected
    # argument``/``unknown field`` diagnostics. The loop above therefore owns the
    # accepted grammar. The factory still governs via ``_edit_govern`` (a minimal,
    # always-argparse-valid argv; a rejecting factory raises → fail).
    _rc = _edit_govern(ticket_id)
    if _rc is not None:
        return _rc
    try:
        edit_warning = edit_core(
            ticket_id,
            fields,
            tag_add=tag_add or None,
            tag_remove=tag_remove or None,
            tag_set=tag_set,
            repo_root=repo_root,
        )
    except CommandError as exc:
        print(exc.message, file=sys.stderr)
        return exc.returncode
    # Mutation confirmation (ticket 6bda-9d58-8546-4638): field NAMES only, never
    # values — comma-joined in invocation order (deduped).
    from rebar._commands import _confirm

    names = ", ".join(dict.fromkeys(set_names))
    _confirm.emit("edited", ticket_id, names, f"edited {ticket_id}: {names}")
    # Advisory description-cap heads-up (ticket 594b) on the CLI's stderr channel; the
    # edit already committed, so it never changes the exit code. Emitted BEFORE --review
    # so the author sees why the review is about to refuse admission.
    _warn_stderr(edit_warning)
    if review:
        # --review (story a114): re-run the signed plan review strictly AFTER the
        # EDIT event committed (and its short-lived store lock was released) — the
        # edit stays committed whatever the verdict, and no store flock is held
        # while the (possibly multi-minute) review runs. A raising review_plan
        # propagates via the standard CLI error path. NOT atomic against
        # concurrent store reconvergence — `rebar review-plan <id> --status` is
        # the cheap currency check.
        from rebar import llm  # LAZY — preserves optionality

        resolved = resolve_ticket_id(ticket_id, str(tracker_dir(repo_root))) or ticket_id
        result = llm.review_plan(resolved, sign=True, repo_root=repo_root)
        # Lazy in-function import of the _cli helper from a _commands module — the
        # established pattern (see the lazy `from rebar._cli import _help` in
        # _commands/transition.py's reopen_cli).
        from rebar._cli._llm_commands import _disposition_exit_code, _render_plan_review_text

        _render_plan_review_text(result)
        return _disposition_exit_code(result, indeterminate_code=2)
    return 0
