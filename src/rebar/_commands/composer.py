"""Tier B event-composer commands (docs/bash-migration.md §4): create (+ edit/link/
unlink/revert as they land).

These are the heavier leaf writes — multi-flag arg parsing, validation with
``--output json`` error envelopes, alias generation, and structured output. Each
splits into a ``*_core`` (validation + event compose + append through the seam,
returning structured data) shared by the library, and a ``*_cli`` (output-format
parsing + text/json formatting) used by the bash dispatcher's Python route. The
core reuses the same Python helpers the bash already delegated to (alias compute,
the shared reducer, ``rebar._engine_support.output``) so behaviour matches.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid as _uuid

from rebar._commands._seam import (
    CommandError,
    append_event,
    require_id,
    require_not_ghost,
    tracker_dir,
    validate_tag_name,
)
from rebar._engine_support.output import OutputFormatError, error_envelope, parse_output
from rebar._engine_support.resolver import resolve_ticket_id

logger = logging.getLogger(__name__)

_TYPES = ("bug", "epic", "story", "task", "session_log", "code_review", "identity")

# Ticket types exempt from the plan-review file-impact-coverage gate (P9). Kept in
# lockstep with the gate's own exemption at
# rebar.llm.plan_review.orchestrator (bug/session_log short-circuit before P9). The
# create-time warning below mirrors it so a freshly-created work ticket is nudged to
# record file_impact early, before `review-plan` flags it.
_FILE_IMPACT_EXEMPT_TYPES = ("bug", "session_log", "code_review", "identity")

_USAGE = (
    "Usage: ticket create <ticket_type> <title> [--parent <id>] [--priority <n>] "
    "[--assignee <name>] [--description <text>] [--tags <tag1,tag2>]\n"
    "  ticket_type: bug | epic | story | task | session_log | code_review | identity\n"
    "  --priority, -p: 0-4 (0=critical, 4=backlog; default: 2)"
)


def _new_ticket_id() -> str:
    """Fresh 16-hex canonical ticket id (``xxxx-xxxx-xxxx-xxxx``), as bash generates."""
    u = _uuid.uuid4().hex
    return f"{u[:4]}-{u[4:8]}-{u[8:12]}-{u[12:16]}"


def _compute_alias(ticket_id: str) -> str:
    """Human alias for a NEW ticket via the in-process helper (``rebar._alias``).

    New tickets use the v2 ``adjective-adjective-animal`` generator
    (:func:`rebar._alias.compute_genesis_alias`), backed by the bundled gfycat
    wordlist; the alias is persisted onto the CREATE event so the format is locked
    in at genesis. (Legacy tickets are unaffected — their read-time backfill still
    uses the adjective-noun-noun :func:`compute_alias`.) Same hex fallback when the
    wordlist is unavailable. The ``or`` guards the ``None`` a malformed (<12-hex) id
    would return; native ids are always 16-hex so this is belt-and-suspenders.
    """
    from rebar._alias import compute_genesis_alias

    return compute_genesis_alias(ticket_id) or ticket_id.replace("-", "")[:8]


def create_core(
    ticket_type: str,
    title: str,
    *,
    parent: str | None = None,
    priority: int | str | None = None,
    assignee: str | None = None,
    description: str | None = None,
    tags=None,
    source: dict | None = None,
    status: str | None = None,
    identity: dict | None = None,
    repo_root=None,
    creation_channel: str,
) -> dict:
    """Validate, compose, and append a CREATE event; return ``{id, alias, title}``.

    Mirrors ``ticket_create``'s validation order and messages: ticket_type enum
    (carries the invalid_ticket_type envelope), non-empty title, title ≤ 255, the
    U+2192→``->`` normalisation, priority 0-4, init check, and parent resolution
    (exists / has CREATE-or-SNAPSHOT / not closed). Raises :class:`CommandError` on
    any failure.

    ``source`` (P1.2 import): optional provenance recorded onto the CREATE event so
    the reducer can surface where an imported ticket came from. Recognised keys —
    ``source_id``, ``source_created_at``, ``source_author``, ``source_env`` — are
    copied into the event data when non-None. The new ticket always gets a fresh
    local id and a fresh HLC timestamp; provenance is additive metadata, never a
    foreign-timestamp injection.

    ``creation_channel`` (epic jira-reb-977, story 6fe2): the public ingress that
    produced this genesis CREATE — one of ``cli`` / ``mcp`` / ``python`` / ``jira`` /
    ``import`` (``unknown`` is projection-only and rejected here). It is REQUIRED
    (keyword-only, no default) so every converging caller of this internal seam must
    declare its channel; it is validated via
    :func:`rebar.reducer._version.validate_creation_channel` and stored UNCONDITIONALLY
    into the CREATE ``data`` (unlike the present-only ``source_*`` fields), then
    projected immutably into compiled ticket state.
    """
    from rebar.reducer import reduce_ticket
    from rebar.reducer._version import validate_creation_channel

    validate_creation_channel(creation_channel)

    tracker = tracker_dir(repo_root)

    if ticket_type not in _TYPES:
        raise CommandError(
            f"Error: invalid ticket type '{ticket_type}'. "
            "Must be one of: bug, epic, story, task, session_log, code_review, identity",
            error_code="invalid_ticket_type",
            input_str=ticket_type,
        )
    if not title.strip():
        raise CommandError("Error: title must be non-empty")
    if len(title) > 255:
        raise CommandError(f"Error: title exceeds 255 characters ({len(title)} chars)")

    prio = "2" if priority is None or priority == "" else str(priority)
    if prio not in ("0", "1", "2", "3", "4"):
        raise CommandError(f"Error: invalid priority '{prio}'. Must be 0-4")

    title = title.replace("→", "->")

    if not (tracker / ".env-id").is_file():
        raise CommandError("Error: ticket system not initialized. Run 'ticket init' first.")

    parent_id = ""
    if parent:
        resolved = resolve_ticket_id(parent, str(tracker)) or parent
        if not (tracker / resolved).is_dir():
            raise CommandError(f"Error: parent ticket '{resolved}' does not exist")
        pdir = tracker / resolved
        if not any(
            p.name.endswith(("-CREATE.json", "-SNAPSHOT.json")) and not p.name.startswith(".")
            for p in pdir.iterdir()
        ):
            raise CommandError(f"Error: parent ticket '{resolved}' has no CREATE or SNAPSHOT event")
        if (reduce_ticket(str(pdir)) or {}).get("status") == "closed":
            raise CommandError(
                f"Error: cannot create child of closed ticket '{resolved}'. "
                f"Reopen the parent first with: ticket transition {resolved} closed open"
            )
        parent_id = resolved

    tags_list = (
        [t.strip() for t in tags.split(",") if t.strip()]
        if isinstance(tags, str)
        else [t for t in (tags or []) if t]
    )

    ticket_id = _new_ticket_id()
    alias = _compute_alias(ticket_id)

    data = {
        "ticket_type": ticket_type,
        "title": title,
        "parent_id": parent_id,
        "description": description or "",
        "tags": tags_list,
        "priority": int(prio),
        "id": ticket_id,
        # Creation-channel provenance (story 6fe2): stamped UNCONDITIONALLY (unlike the
        # present-only source_*/identity fields) so every genesis CREATE records which
        # interface produced it; the reducer projects it immutably into ticket state.
        "creation_channel": creation_channel,
    }
    if assignee:
        data["assignee"] = assignee
    if alias:
        data["alias"] = alias
    # Genesis status (soup-drift-augur): only the `rebar idea` command passes a
    # non-`open` status, so the ticket is born in `idea` in a single CREATE event
    # (no intervening STATUS event → never momentarily `open`/claimable). Absent,
    # the reducer defaults to `open`, so a normal create is unchanged and no general
    # `create --status` flag is exposed.
    if status:
        data["status"] = status
    if source:
        for _src_key in ("source_id", "source_created_at", "source_author", "source_env"):
            _src_val = source.get(_src_key)
            if _src_val is not None:
                data[_src_key] = _src_val
    # Identity entity payload (epic gnu-whale-ichor): an `identity` ticket carries an
    # `email` plus `mappings` (external-provider account ids) and `keys` (OpenSSH
    # authorized-keys lines) on its CREATE event so the reducer surfaces them in
    # compiled state. Threaded additively like `source` above, so a normal create is
    # unchanged. Only recognised keys are copied (never the raw dict).
    if identity:
        for _id_key in ("email", "mappings", "keys"):
            _id_val = identity.get(_id_key)
            if _id_val is not None:
                data[_id_key] = _id_val

    append_event(ticket_id, "CREATE", data, tracker, repo_root=repo_root)
    return {"id": ticket_id, "alias": alias or None, "title": title}


def create_cli(argv: list[str], *, repo_root=None) -> int:
    """Dispatcher Python route for ``create``: parse --output + flags, format output.

    Returns the process exit code; reproduces the bash text/json output and the
    json error envelope on validation failure.
    """
    try:
        fmt, rest = parse_output(argv, "report")
    except OutputFormatError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if len(rest) < 2:
        print(_USAGE, file=sys.stderr)
        return 1

    ticket_type, title = rest[0], rest[1]
    parent = priority = assignee = description = None
    tags = ""
    i, args = 2, rest
    n = len(args)
    while i < n:
        a = args[i]
        if a in ("--parent",) and i + 1 < n:
            parent = args[i + 1]
            i += 2
        elif a.startswith("--parent="):
            parent = a[len("--parent=") :]
            i += 1
        elif a in ("--priority", "-p") and i + 1 < n:
            priority = args[i + 1]
            i += 2
        elif a.startswith("--priority="):
            priority = a[len("--priority=") :]
            i += 1
        elif a in ("--assignee",) and i + 1 < n:
            assignee = args[i + 1]
            i += 2
        elif a.startswith("--assignee="):
            assignee = a[len("--assignee=") :]
            i += 1
        elif a in ("--description", "-d") and i + 1 < n:
            description = args[i + 1]
            i += 2
        elif a.startswith("--description="):
            description = a[len("--description=") :]
            i += 1
        elif a in ("--tags",) and i + 1 < n:
            tags = f"{tags},{args[i + 1]}" if tags else args[i + 1]
            i += 2
        elif a.startswith("--tags="):
            v = a[len("--tags=") :]
            tags = f"{tags},{v}" if tags else v
            i += 1
        else:
            parent = a
            i += 1  # bare positional → parent (backward-compatible)

    try:
        res = create_core(
            ticket_type,
            title,
            parent=parent,
            priority=priority,
            assignee=assignee,
            description=description,
            tags=tags,
            repo_root=repo_root,
            creation_channel="cli",
        )
    except CommandError as exc:
        if fmt == "json" and exc.error_code:
            print(
                json.dumps(
                    error_envelope(exc.error_code, exc.input_str, exc.message, exc.returncode)
                )
            )
        print(exc.message, file=sys.stderr)
        return exc.returncode

    if fmt == "json":
        print(json.dumps({"id": res["id"], "alias": res["alias"], "title": res["title"]}))
    else:
        alias, tid = res["alias"], res["id"]
        if alias and alias != tid:
            print(f"Created ticket {alias} ({tid}): {res['title']}")
        else:
            print(f"Created ticket {tid}: {res['title']}")
        print(tid)

    # Nudge the author to record file_impact now (it cannot be passed at create
    # time). The plan-review file-impact-coverage gate (P9) flags any leaf work
    # ticket lacking it, so surfacing the requirement here — right after create —
    # lets it be fixed before `review-plan` runs. Warning only (stderr), so stdout
    # stays pure in both text and json modes. Exempt types mirror the gate.
    if ticket_type not in _FILE_IMPACT_EXEMPT_TYPES:
        new_id = res["id"]
        print(
            f"Warning: no file_impact recorded for {ticket_type} {new_id} — "
            "set it before plan-review with: "
            f"""rebar set-file-impact {new_id} '[{{"path":"...","reason":"..."}}]' """
            "(the file-impact-coverage gate will otherwise flag it).",
            file=sys.stderr,
        )
    return 0


# Tags are NOT an EDIT field any more (P2.3): they mutate via TAG_DELTA deltas
# (--add-tag/--remove-tag/--set-tags), so a whole-field EDIT can never clobber a
# concurrent tag add. The library/MCP ``edit(tags=...)`` arg is a DEPRECATED alias
# for --set-tags, intercepted in edit_core before this field set is validated.
_EDIT_FIELDS = ("title", "priority", "assignee", "ticket_type", "description", "parent")
_EDIT_USAGE = (
    "Usage: ticket edit <ticket_id> [--title=VALUE] [--priority=VALUE] [--assignee=VALUE] "
    "[--ticket_type=VALUE] [--description=VALUE] [--parent=VALUE] "
    "[--add-tag=t1,t2] [--remove-tag=t1,t2] [--set-tags=t1,t2]"
)


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


def edit_core(
    ticket_id: str,
    fields: dict,
    *,
    tag_add=None,
    tag_remove=None,
    tag_set=None,
    repo_root=None,
) -> None:
    """Validate fields and append an EDIT event (mirrors ``ticket_edit``), plus tag
    add/remove/set deltas as a TAG_DELTA event (P2.3).

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
    from rebar.reducer._version import TAG_DELTA

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

    out: dict = {}
    for key, value in fields.items():
        value = "" if value is None else str(value)
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
            if value not in _TYPES:
                raise CommandError(
                    f"Error: invalid ticket type '{value}'. "
                    "Must be one of: bug, epic, story, task, session_log, code_review, identity"
                )
            out["ticket_type"] = value
        elif key == "parent":
            out["parent_id"] = _resolve_new_parent(value, resolved, tracker, reduce_ticket)
        else:  # assignee
            out[key] = value

    if out:
        append_event(resolved, "EDIT", {"fields": out}, tracker, repo_root=repo_root)

    if has_tag_op:
        observed = list((reduce_ticket(str(tracker / resolved)) or {}).get("tags") or [])
        if has_set:
            # Compile the wholesale set to a delta vs observed (add-wins):
            # add what's missing, remove observed tags not in the target set.
            added = [t for t in set_list if t not in observed]
            removed = [t for t in observed if t not in set_list]
        else:
            # No-op suppression: only add what's absent, only remove what's present.
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


_TAG_FLAGS = ("add-tag", "remove-tag", "set-tags")


def edit_cli(argv: list[str], *, repo_root=None) -> int:
    """Dispatcher Python route for ``edit``: parse ticket_id + --field pairs +
    tag-delta flags (--add-tag / --remove-tag / --set-tags)."""
    if len(argv) < 2:
        print(_EDIT_USAGE, file=sys.stderr)
        return 1
    ticket_id, rest = argv[0], argv[1:]
    fields: dict = {}
    tag_add: list[str] = []
    tag_remove: list[str] = []
    tag_set: list[str] | None = None

    def _accept_tag(name: str, val: str) -> None:
        nonlocal tag_set
        items = [t for t in val.split(",")]
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
    try:
        edit_core(
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
    return 0


def link_core(
    src_raw: str, tgt_raw: str, relation: str, *, repo_root=None, quiet: bool = False
) -> None:
    """Resolve endpoints and add a LINK via the shared graph (mirrors ticket_link's
    non-dry-run path → ticket-graph.py --link → add_dependency).

    add_dependency owns relation validation, hierarchy promotion (+ the REDIRECT
    note), the redundant-link guard, cycle detection, and the LINK event write —
    the SAME function the bash path calls, so parity is structural. ``quiet``
    suppresses add_dependency's stdout/stderr (the library facade discards it, as
    the subprocess path did); the CLI lets it through. Raises :class:`CommandError`.
    """
    import contextlib
    import io

    from rebar.graph._links import CyclicDependencyError, add_dependency

    tracker = tracker_dir(repo_root)
    src_id = resolve_ticket_id(src_raw, str(tracker))
    if src_id is None:
        raise CommandError(f"Error: ticket '{src_raw}' does not exist")
    tgt_id = resolve_ticket_id(tgt_raw, str(tracker))
    if tgt_id is None:
        raise CommandError(f"Error: ticket '{tgt_raw}' does not exist")
    sink = io.StringIO()
    try:
        if quiet:
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                add_dependency(src_id, tgt_id, str(tracker), relation)
        else:
            add_dependency(src_id, tgt_id, str(tracker), relation)
    except (CyclicDependencyError, ValueError) as exc:
        raise CommandError(f"Error: {exc}") from None


def _link_dry_run(src_raw: str, tgt_raw: str, relation: str, *, repo_root=None) -> int:
    """In-process ``link --dry-run`` preview (Tier E E6.5a — replaces the
    ticket-link.sh subprocess). Resolves endpoints, asks the shared hierarchy
    resolver what WOULD happen, and prints the byte-identical ``[DRY RUN]`` line
    without writing any event. Missing tickets error like the bash _check_ticket_
    exists; a resolver failure falls back to the plain "Would create" preview."""
    from rebar.graph._hierarchy import resolve_hierarchy_link

    tracker = str(tracker_dir(repo_root))
    src_id = resolve_ticket_id(src_raw, tracker)
    if src_id is None:
        print(f"Error: ticket '{src_raw}' does not exist", file=sys.stderr)
        return 1
    tgt_id = resolve_ticket_id(tgt_raw, tracker)
    if tgt_id is None:
        print(f"Error: ticket '{tgt_raw}' does not exist", file=sys.stderr)
        return 1
    try:
        res = resolve_hierarchy_link(src_id, tgt_id, tracker, relation)
    except Exception:  # noqa: BLE001 — resolver unavailable → plain preview (bash parity)
        print(f"[DRY RUN] Would create: {src_id} {relation} {tgt_id} (no event written)")
        return 0
    if res.get("is_redundant"):
        print(
            f"[DRY RUN] Would reject: {src_id} {relation} {tgt_id} — "
            "redundant link (direct child) (no event written)"
        )
    elif res.get("was_redirected"):
        rs = res.get("resolved_source", src_id)
        rt = res.get("resolved_target", tgt_id)
        print(f"[DRY RUN] Would promote: {rs} {relation} {rt} (no event written)")
    else:
        print(f"[DRY RUN] Would create: {src_id} {relation} {tgt_id} (no event written)")
    return 0


def link_cli(argv: list[str], *, repo_root=None) -> int:
    """Dispatcher Python route for ``link``: parse --dry-run, resolve, delegate."""
    dry_run = "--dry-run" in argv
    rest = [a for a in argv if a != "--dry-run"]
    if len(rest) < 3:
        print("Usage: ticket link <id1> <id2> <relation>", file=sys.stderr)
        return 1
    src_raw, tgt_raw, relation = rest[0], rest[1], rest[2]

    if dry_run:
        return _link_dry_run(src_raw, tgt_raw, relation, repo_root=repo_root)

    try:
        link_core(src_raw, tgt_raw, relation, repo_root=repo_root)
    except CommandError as exc:
        print(exc.message, file=sys.stderr)
        return exc.returncode
    return 0


_REVERT_USAGE = (
    "Usage: ticket revert <ticket_id> <target_uuid> [--reason=<text>]\n"
    "  ticket_id:   ticket directory name\n"
    "  target_uuid: UUID of the event to revert\n"
    "  --reason=    optional reason text"
)


def revert_core(ticket_id: str, target_uuid: str, reason: str = "", *, repo_root=None) -> str:
    """Append a REVERT event targeting an existing event (mirrors ticket-revert.sh).

    Resolves the id, ghost-checks, finds the target event by UUID, rejects
    REVERT-of-REVERT, then appends the REVERT event through the seam. Reverting an
    ARCHIVED event also clears the ``.archived`` marker (the reducer un-archives).
    Returns the resolved ticket id. Raises :class:`CommandError`.
    """
    from rebar.reducer.marker import remove_marker

    tracker = tracker_dir(repo_root)
    if not (tracker / ".env-id").is_file():
        raise CommandError("Error: ticket system not initialized. Run 'ticket init' first.")
    resolved = require_id(ticket_id, tracker)
    ticket_dir = tracker / resolved
    require_not_ghost(resolved, tracker)

    target_type = None
    for entry in sorted(os.listdir(ticket_dir)):
        if entry.startswith(".") or not entry.endswith(".json"):
            continue
        try:
            with open(ticket_dir / entry, encoding="utf-8") as fh:
                ev = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        if ev.get("uuid") == target_uuid:
            target_type = ev.get("event_type", "")
            break
    if target_type is None:
        raise CommandError(
            f"Error: event not found: no event with UUID '{target_uuid}' in ticket '{resolved}'"
        )
    if target_type == "REVERT":
        raise CommandError(
            f"Error: cannot revert a REVERT event (target UUID '{target_uuid}' is a REVERT)"
        )

    append_event(
        resolved,
        "REVERT",
        {"target_event_uuid": target_uuid, "target_event_type": target_type, "reason": reason},
        tracker,
        repo_root=repo_root,
    )
    if target_type == "ARCHIVED":
        try:
            remove_marker(str(ticket_dir))
        except Exception:  # noqa: BLE001 — best-effort .archived marker clear on REVERT-of-ARCHIVED; broad-but-logged
            logger.warning(
                "could not clear .archived marker for %s after REVERT; continuing",
                resolved,
                exc_info=True,
            )
    return resolved


def revert_cli(argv: list[str], *, repo_root=None) -> int:
    """Dispatcher Python route for ``revert``: parse args, print the confirmation."""
    if len(argv) < 2:
        print(_REVERT_USAGE, file=sys.stderr)
        return 1
    ticket_id, target_uuid = argv[0], argv[1]
    reason = ""
    for arg in argv[2:]:
        if arg.startswith("--reason="):
            reason = arg[len("--reason=") :]
        else:
            print(f"Error: unknown argument '{arg}'", file=sys.stderr)
            print(_REVERT_USAGE, file=sys.stderr)
            return 1
    try:
        resolved = revert_core(ticket_id, target_uuid, reason, repo_root=repo_root)
    except CommandError as exc:
        print(exc.message, file=sys.stderr)
        return exc.returncode
    print(f"Reverted event '{target_uuid}' on ticket '{resolved}'")
    return 0
