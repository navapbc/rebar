"""Session-log capture helper (epic 7738, story e7e4).

A thin convenience layer over the shared write path: create a ``session_log`` on
first use and append entries to the SAME log on subsequent calls, so verbose
logging is low-friction without agents hand-assembling ``create`` + ``comment``.

The "current" log is tracked by a LOCAL, git-ignored pointer file
(``<repo>/.rebar/current_session_log``) — the same ``.rebar`` local-state root
``scratch`` uses — so stateless CLI invocations within a checkout converge on one
log. ``start`` rotates to a fresh log (and re-points). The pointer never enters
the shared tickets branch, so it does not propagate across machines.

All writes go through the existing locked seam (``composer.create_core`` /
``_seam.append_event`` / ``composer.link_core``), so the helper flows identically
to library, CLI, and MCP, and inherits the session_log write-path rules from add5
(gate/lifecycle exempt; blocking links refused; relates_to / discovered_from
allowed).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from rebar._commands._seam import (
    CommandError,
    append_event,
    require_id,
    require_not_ghost,
    tracker_dir,
)
from rebar._commands.composer import create_core, link_core
from rebar._commands.session_id import resolve_session_id

_DEFAULT_TITLE = "Session log"
_POINTER_NAME = "current_session_log"


def _pointer_path(repo_root=None) -> Path:
    from rebar import config

    root = repo_root or config.repo_root()
    return Path(root) / ".rebar" / _POINTER_NAME


def _resolve_session_fp() -> str | None:
    """The current session fingerprint used to auto-rotate the current-log pointer.

    Delegates to the shared :func:`rebar._commands.session_id.resolve_session_id`
    (epic crust-fetch-stump, story 6014) so session-id precedence is defined in ONE
    place. NOTE the unified contract puts ``REBAR_SESSION_ID`` BEFORE
    ``CLAUDE_CODE_SESSION_ID`` — an intentional inversion of this function's former
    order that only differs when BOTH are set. As before, we stop at ``None`` (never
    git HEAD) so a commit within one session does not spuriously rotate the log.
    """
    return resolve_session_id()


def _read_pointer(repo_root=None) -> dict | None:
    """Read the current-log pointer as ``{"id", "session"}``.

    Tolerates the LEGACY bare-string format (a pointer written before session
    fingerprinting): any file that is not JSON with an ``id`` key is treated as
    ``{"id": <raw>, "session": None}`` so pre-existing pointers keep working (and,
    with ``session`` None, never trigger rotation — degrade-safe).
    """
    try:
        raw = _pointer_path(repo_root).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict) and obj.get("id"):
            return {"id": obj["id"], "session": obj.get("session")}
    except (ValueError, TypeError):
        pass
    return {"id": raw, "session": None}  # legacy bare-string pointer


def _write_pointer(ticket_id: str, session: str | None = None, repo_root=None) -> None:
    from rebar._store.fsutil import atomic_write

    p = _pointer_path(repo_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"id": ticket_id, "session": session}, ensure_ascii=False)
    atomic_write(p, payload, encoding="utf-8")  # atomic publish


def _is_live_session_log(ticket_id: str, tracker: str) -> bool:
    """True iff ``ticket_id`` is an existing, non-deleted/archived session_log."""
    from rebar.reducer import reduce_ticket

    d = os.path.join(tracker, ticket_id)
    if not os.path.isdir(d):
        return False
    try:
        state = reduce_ticket(d)
    except Exception:  # noqa: BLE001 — unreadable/corrupt ticket is not a live session_log; fall open to False
        return False
    return (
        isinstance(state, dict)
        and state.get("ticket_type") == "session_log"
        and state.get("status") != "deleted"
        and not state.get("archived")
    )


def _resolve_current(tracker: str, repo_root=None) -> dict | None:
    """Return the pointer dict (``{"id", "session"}``) iff it names a live log."""
    ptr = _read_pointer(repo_root)
    if ptr and _is_live_session_log(ptr["id"], tracker):
        return ptr
    return None


def _link_optional(log_id: str, *, relates_to=None, discovered_from=None, repo_root=None) -> None:
    # Non-blocking relations only — add5 already refuses blocks/depends_on on a
    # session_log, so an attempted blocking link here would raise CommandError.
    if relates_to:
        link_core(log_id, relates_to, "relates_to", repo_root=repo_root, quiet=True)
    if discovered_from:
        link_core(log_id, discovered_from, "discovered_from", repo_root=repo_root, quiet=True)


def start(
    *,
    summary=None,
    relates_to=None,
    discovered_from=None,
    repo_root=None,
    creation_channel: str = "python",
) -> dict:
    """Explicitly create a NEW session_log and make it the current one.

    Returns ``{"id", "alias"}``. ``summary`` becomes the title (the documented
    short-work-summary convention); absent, a default title is used. ``creation_channel``
    stamps the session_log's genesis CREATE (``python`` for the library, ``cli`` from the
    CLI, ``mcp`` from the MCP adapter).
    """
    res = create_core(
        "session_log",
        summary or _DEFAULT_TITLE,
        description="",
        repo_root=repo_root,
        creation_channel=creation_channel,
    )
    _write_pointer(res["id"], _resolve_session_fp(), repo_root)
    _link_optional(
        res["id"], relates_to=relates_to, discovered_from=discovered_from, repo_root=repo_root
    )
    return {"id": res["id"], "alias": res["alias"]}


def append(
    entry,
    *,
    summary=None,
    relates_to=None,
    discovered_from=None,
    repo_root=None,
    creation_channel: str = "python",
) -> dict:
    """Append ``entry`` (a COMMENT) to the current session_log, creating one on
    first use. Returns ``{"id", "alias", "created"}`` (``created`` True iff this
    call created the log). ``creation_channel`` stamps the genesis CREATE only when this
    call creates the log (an append to an existing log writes a COMMENT, not a CREATE)."""
    if not entry:
        raise CommandError("Error: session-log entry must be non-empty")
    tracker = tracker_dir(repo_root)
    current = _resolve_current(str(tracker), repo_root)
    # Session-scoped auto-rotation (bug kooky-graft-cap; defensive rotation
    # slum-shoal-gully): if THIS session has a fingerprint that differs from the one on
    # the current-log pointer, treat the log as absent so a fresh one is started below.
    # A DIFFERING pointer fingerprint includes a MISSING one (``session=None``): a
    # fingerprint-less pointer was written by a prior run with no session id, so a
    # fingerprinted session must rotate off it rather than pollute that stranger's log.
    # Only ``now is None`` (this session has no fingerprint) suppresses rotation — a
    # session that cannot identify itself never rotates, so a continuous no-id session
    # keeps one log (degrade-safe, backward compatible).
    now = _resolve_session_fp()
    if current is not None and now is not None and current.get("session") != now:
        current = None
    created = False
    alias = None
    if current is None:
        res = start(
            summary=summary,
            relates_to=relates_to,
            discovered_from=discovered_from,
            repo_root=repo_root,
            creation_channel=creation_channel,
        )
        current_id, alias, created = res["id"], res["alias"], True
    else:
        current_id = current["id"]
        _link_optional(
            current_id, relates_to=relates_to, discovered_from=discovered_from, repo_root=repo_root
        )
    resolved = require_id(current_id, tracker)
    require_not_ghost(resolved, tracker)
    append_event(resolved, "COMMENT", {"body": entry}, tracker, repo_root=repo_root)
    return {"id": current_id, "alias": alias, "created": created}


# ───────────────────────────────── CLI ───────────────────────────────────────
_USAGE = (
    'Usage: rebar session-log <append "<entry>" | start> '
    "[--summary=<text>] [--relates-to=<id>] [--discovered-from=<id>]"
)


def _parse_opts(argv: list[str]) -> tuple[dict, list[str]]:
    opts: dict = {"summary": None, "relates_to": None, "discovered_from": None}
    positionals: list[str] = []
    for arg in argv:
        if arg.startswith("--summary="):
            opts["summary"] = arg[len("--summary=") :]
        elif arg.startswith("--relates-to="):
            opts["relates_to"] = arg[len("--relates-to=") :]
        elif arg.startswith("--discovered-from="):
            opts["discovered_from"] = arg[len("--discovered-from=") :]
        elif arg.startswith("-"):
            raise CommandError(f"Error: unknown option '{arg}'\n{_USAGE}")
        else:
            positionals.append(arg)
    return opts, positionals


def session_log_cli(argv: list[str]) -> int:
    """``rebar session-log append "<entry>"`` / ``rebar session-log start`` — verb
    dispatch mirroring scratch's sub-action pattern. Prints a JSON result."""
    if not argv:
        print(_USAGE)
        return 1
    verb, rest = argv[0], argv[1:]
    try:
        opts, positionals = _parse_opts(rest)
        if verb == "start":
            if positionals:
                raise CommandError(f"Error: 'start' takes no positional args\n{_USAGE}")
            result = start(creation_channel="cli", **opts)
        elif verb == "append":
            if len(positionals) != 1:
                raise CommandError(f"Error: 'append' requires exactly one <entry>\n{_USAGE}")
            result = append(positionals[0], creation_channel="cli", **opts)
        else:
            print(f"Error: unknown session-log action '{verb}'\n{_USAGE}", file=sys.stderr)
            return 1
    except CommandError as exc:
        print(exc.message, file=sys.stderr)
        return exc.returncode
    print(json.dumps(result, ensure_ascii=False))
    return 0
