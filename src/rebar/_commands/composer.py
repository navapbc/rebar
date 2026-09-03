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

import logging
import sys
import uuid as _uuid

from rebar._commands._seam import (
    CommandError,
    _warn_stderr,
    append_event,
    tracker_dir,
)
from rebar._engine_support.output import OutputFormatError, error_envelope, parse_output
from rebar._engine_support.resolver import resolve_ticket_id
from rebar._mcp_errors import js_safe_dumps
from rebar.types import PLAN_REVIEW_EXEMPT_TYPES, TICKET_TYPES

logger = logging.getLogger(__name__)

_TYPES = TICKET_TYPES  # canonical, see rebar.types (mirror F7)

# Types exempt from the plan-review file-impact-coverage gate (P9); the create-time
# warning mirrors it so a new work ticket records file_impact early. The SAME predicate
# as the start-work exemption, hence the single declaration in rebar.types (mirror F3).
# The old comment claimed lockstep with a "short-circuit before P9" in orchestrator.py;
# that citation had drifted — the literal there is in the drift-refresh path.
_FILE_IMPACT_EXEMPT_TYPES = PLAN_REVIEW_EXEMPT_TYPES

_USAGE = (
    "Usage: ticket create <ticket_type> <title> [--parent <id>] [--priority <n>] "
    "[--assignee <name>] [--description <text>] [--tags <tag1,tag2>] "
    "[--detected-by <source>]\n"
    f"  ticket_type: {' | '.join(_TYPES)}\n"
    "  --priority, -p: 0-4 (0=critical, 4=backlog; default: 2)\n"
    "  --detected-by: detection channel (overrides REBAR_DETECTED_BY env var)"
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
    # Config root RESOLVED as readers do (repo_root > REBAR_ROOT > git toplevel), NOT
    # dirname(tracker) — that suppressed both advisories on a relocated store (2ec7).
    cfg_root = str(_config.repo_root(repo_root))
    # Save-time heads-up (ticket 594b): computed AFTER the event lands, so an oversized
    # description is reported the moment it is written instead of at review-plan time.
    # Advisory only — each surface emits it on its own channel (CLI stderr, library
    # logger, MCP result field); the create itself is unaffected either way.
    from rebar._commands.gates import description_cap_warning

    warning = description_cap_warning(
        description,
        ticket_type,
        ticket_id=alias or ticket_id,
        cfg_root=cfg_root,
    )
    # Create-time advisory duplicate probe (ticket eac3-ed70-764a-4f9e): a recent
    # same-normalized-title create inside the journal window, surfaced the same way —
    # after the event lands, on each surface's own channel, never blocking the write.
    from rebar._commands.recent_creates import duplicate_create_warning

    dup_warning = duplicate_create_warning(
        tracker,
        ticket_id=ticket_id,
        alias=alias or None,
        title=title,
        cfg_root=cfg_root,
    )
    return {
        "id": ticket_id,
        "alias": alias or None,
        "title": title,
        "description_warning": warning,
        "duplicate_warning": dup_warning,
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


def _create_govern(
    *,
    ticket_type,
    title,
    parent,
    priority,
    assignee,
    description,
    detected_by,
    bridge_project,
    repos,
    tags,
    fmt,
) -> int | None:
    """Parser-of-record governance for :func:`create_cli`.

    Reconstruct a canonical, always-argparse-valid argv from the values the bespoke
    scan extracted (positionals after ``--`` so they are never read as options) and let
    the factory govern it. Returns a render exit code when a (rejecting) factory refuses
    the argv, else ``None`` to continue.
    """
    from rebar._cli._parser import ParseError, render_parse_error
    from rebar._cli._parsers.core.writes import build_create

    optflags = [
        *([f"--parent={parent}"] if parent is not None else []),
        *([f"--priority={priority}"] if priority is not None else []),
        *([f"--assignee={assignee}"] if assignee is not None else []),
        *([f"--description={description}"] if description is not None else []),
        *([f"--detected-by={detected_by}"] if detected_by is not None else []),
        *([f"--bridge-project={bridge_project}"] if bridge_project is not None else []),
        *([f"--repos={repos}"] if repos is not None else []),
        *([f"--tags={tags}"] if tags else []),
        *([f"--output={fmt}"] if fmt in ("text", "json") else []),
    ]
    try:
        build_create(prog="rebar create").parse_args([*optflags, "--", ticket_type, title])
    except ParseError as exc:
        return render_parse_error(exc)
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
        m = _match_long_opt(args, i, "--parent")
        if m is not None:
            parent, i = m
            continue
        if a in ("--priority", "-p") and i + 1 < n:
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

    # Parser of record. The scan above owns the parts argparse cannot express
    # byte-for-byte: ``--tags`` comma-accumulation across repeats, the
    # bare-positional→parent fallback, value flags that consume an option-looking next
    # token, and the bespoke ``unrecognised option`` reject text/exit. The factory
    # still governs via ``_create_govern`` (a rejecting factory raises → fail).
    _rc = _create_govern(
        ticket_type=ticket_type,
        title=title,
        parent=parent,
        priority=priority,
        assignee=assignee,
        description=description,
        detected_by=detected_by,
        bridge_project=bridge_project,
        repos=repos,
        tags=tags,
        fmt=fmt,
    )
    if _rc is not None:
        return _rc

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
                js_safe_dumps(
                    error_envelope(exc.error_code, exc.input_str, exc.message, exc.returncode)
                )
            )
        print(exc.message, file=sys.stderr)
        return exc.returncode

    if fmt == "json":
        print(js_safe_dumps({"id": res["id"], "alias": res["alias"], "title": res["title"]}))
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
    # Same stderr channel for the description-cap heads-up (ticket 594b) and the
    # duplicate-title advisory (ticket eac3): the create already succeeded, so both are
    # advisory and the exit code stays 0.
    _warn_stderr(res.get("description_warning"))
    _warn_stderr(res.get("duplicate_warning"))
    return 0


# Extracted along the existing call-graph seams (module-size policy); re-exported
# so composer-path callers and monkeypatch sites still work.
# The EDIT surface lives in ``composer_edit`` (module-size policy); re-exported so
# ``rebar._commands.composer.edit_*`` callers and monkeypatch sites still work.
from rebar._commands.composer_edit import (  # noqa: E402,F401
    _EDIT_FIELDS,
    _EDIT_USAGE,
    edit_cli,
    edit_core,
)
from rebar._commands.link_revert import (  # noqa: E402,F401
    _REVERT_USAGE,
    _link_dry_run,
    link_cli,
    link_core,
    revert_cli,
    revert_core,
)
