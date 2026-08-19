"""Tier B event-composer commands (docs/bash-migration.md §4): create + edit
(link / unlink / revert live in ``link_revert.py``).

These are the heavier leaf writes — multi-flag arg parsing, validation with
``--output json`` error envelopes, alias generation, and structured output. Each
splits into a ``*_core`` (validation + event compose + append through the seam,
returning structured data) shared by the library, and a ``*_cli`` (output-format
parsing + text/json formatting) invoked by the argparse CLI. The core and CLI
share the same Python helpers (alias compute, the shared reducer,
``rebar._engine_support.output``) so library and CLI behaviour match.
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
)
from rebar._commands.composer_edit import (
    _apply_tag_deltas,
    _edit_description_warning,
    _edit_repos_list,
    _enforce_promote_only,
    _parse_tag_list,
    _resolve_new_parent,
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
    "[--assignee <name>] [--description <text>] [--tags <tag1,tag2>] "
    "[--detected-by <source>]\n"
    "  ticket_type: bug | epic | story | task | session_log | code_review | identity\n"
    "  --priority, -p: 0-4 (0=critical, 4=backlog; default: 2)\n"
    "  --detected-by: detection channel (overrides REBAR_DETECTED_BY env var)"
)


def _warn_stderr(message: str | None) -> None:
    """Print an advisory ``Warning:`` line to stderr, or nothing when there is none.

    The branch lives here rather than in ``create_cli``/``edit_cli`` because both sit at
    their locked complexity ceiling (``.github/complexity-baseline.json``); it is also the
    one place the CLI's warning prefix is spelled.
    """
    if message:
        print(f"Warning: {message}", file=sys.stderr)


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


def _apply_bridge_repos(data: dict, bridge_project: str | None, repos) -> None:
    """Merge the story-cef7 bridge/project fields into a CREATE event's ``data``.

    Split out of :func:`create_core` (which sits at its locked complexity ceiling,
    ``.github/complexity-baseline.json``). ``bridge_project`` is TRI-STATE and carried
    PRESENT-ONLY: the ``None`` default means "flag absent — leave state's seeded None";
    an explicit ``""`` or a real key is stored verbatim so ``""`` (never-sync) and a sync
    target both survive replay. ``repos`` accepts a CSV string (mirroring ``--tags``) or a
    list; carried only when provided (the seeded ``[]`` covers the absent case).
    """
    if bridge_project is not None:
        data["bridge_project"] = bridge_project
    if repos is not None:
        data["repos"] = (
            [r.strip() for r in repos.split(",") if r.strip()]
            if isinstance(repos, str)
            else [r for r in repos if r]
        )


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
    detected_by: str | None = None,
    bridge_project: str | None = None,
    repos=None,
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

    # Detection-channel capture (ticket d3ed): explicit param wins over the env var
    # (an explicit empty string suppresses it); strip+lowercase, empty -> unset,
    # never blocks the create. Present-only, mirroring source_* above.
    from rebar import config as _config

    _detected_candidate = _config.resolve_detected_by(detected_by)
    if _detected_candidate is not None:
        _detected_norm = _detected_candidate.strip().lower()
        if _detected_norm:
            data["detected_by"] = _detected_norm

    _apply_bridge_repos(data, bridge_project, repos)

    append_event(ticket_id, "CREATE", data, tracker, repo_root=repo_root)
    # Save-time heads-up (ticket 594b): computed AFTER the event lands, so an oversized
    # description is reported the moment it is written instead of at review-plan time.
    # Advisory only — each surface emits it on its own channel (CLI stderr, library
    # logger, MCP result field); the create itself is unaffected either way.
    from rebar._commands.gates import description_cap_warning

    warning = description_cap_warning(
        description,
        ticket_type,
        ticket_id=alias or ticket_id,
        cfg_root=os.path.dirname(str(tracker)),
    )
    return {
        "id": ticket_id,
        "alias": alias or None,
        "title": title,
        "description_warning": warning,
    }


def _match_long_opt(args: list[str], i: int, name: str):
    """Match a long option ``--name value`` or ``--name=value`` at ``args[i]``.

    Split out of :func:`create_cli` (at its locked complexity ceiling). Returns
    ``(value, next_index)`` when it matches, else ``None`` — mirroring the exact
    ``elif a in (name,) and i + 1 < n`` / ``elif a.startswith(name + "=")`` pair it
    replaces, so a bare ``--name`` with no following value does NOT match (it falls
    through to the caller's unknown-option rejection, unchanged).
    """
    a = args[i]
    if a == name and i + 1 < len(args):
        return args[i + 1], i + 2
    prefix = f"{name}="
    if a.startswith(prefix):
        return a[len(prefix) :], i + 1
    return None


def create_cli(argv: list[str], *, repo_root=None) -> int:
    """CLI route for ``create``: parse --output + flags, format output.

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
    parent = priority = assignee = description = detected_by = None
    bridge_project = None
    repos = None
    tags = ""
    i, args = 2, rest
    n = len(args)
    while i < n:
        a = args[i]
        m = _match_long_opt(args, i, "--bridge-project")
        if m is not None:
            bridge_project, i = m
            continue
        m = _match_long_opt(args, i, "--repos")
        if m is not None:
            repos, i = m
            continue
        m = _match_long_opt(args, i, "--detected-by")
        if m is not None:
            detected_by, i = m
            continue
        m = _match_long_opt(args, i, "--assignee")
        if m is not None:
            assignee, i = m
            continue
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
        elif a.startswith("-"):
            # An option-looking token must NOT fall through to `parent`, or a typo
            # (or an option missing its value) resurfaces as the baffling
            # "parent … '--body-file' does not exist".
            print(f"Error: unrecognised option '{a}'\n{_USAGE}", file=sys.stderr)
            return 2
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
            detected_by=detected_by,
            bridge_project=bridge_project,
            repos=repos,
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
        # Normalized confirmation (ticket 6bda-9d58-8546-4638): one line, all the
        # data the old two-line form carried (alias, id, title).
        from rebar._commands._confirm import confirm_created

        confirm_created("", res)

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
    # Same stderr channel for the description-cap heads-up (ticket 594b): the create
    # already succeeded, so this is advisory and the exit code stays 0.
    _warn_stderr(res.get("description_warning"))
    return 0


# Extracted along the existing call-graph seams (module-size policy); re-exported
# so composer-path callers and monkeypatch sites still work.
from rebar._commands.link_revert import (  # noqa: E402,F401
    _REVERT_USAGE,
    _link_dry_run,
    link_cli,
    link_core,
    revert_cli,
    revert_core,
)

# ── EDIT surface ─────────────────────────────────────────────────────────────
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

    # Promote-only guard (story cef7): `bridge_project` may be set on an UNBOUND ticket
    # but never changed once the ticket already holds a tracker binding.
    _enforce_promote_only(out, resolved, tracker)

    warning: str | None = None
    if out:
        append_event(resolved, "EDIT", {"fields": out}, tracker, repo_root=repo_root)
        warning = _edit_description_warning(out, resolved, tracker, reduce_ticket)

    if has_tag_op:
        _apply_tag_deltas(resolved, tracker, repo_root, has_set, set_list, add_list, remove_list)

    return warning


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
    if review:
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
