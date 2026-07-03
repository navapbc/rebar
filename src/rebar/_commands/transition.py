"""In-process ``transition`` / ``reopen`` / ``claim`` wrappers.

These wrappers drive the locked write cores in :mod:`rebar._commands.txn`
(``transition_core`` / ``claim_core``) and own everything around the locked write:
``--output`` parsing, the 2-arg current-status autodetect, the ``archived → open``
un-archive seam, status validation, the idempotent no-op, the ghost / init checks,
the open-children close guard, ``newly_unblocked`` detection (via
:func:`rebar.graph._unblock.batch_close_operations`), the force-close audit
comment, compact-on-close, per-ticket scratch cleanup, the
``{ticket_id,from,to,newly_unblocked}`` json / ``UNBLOCKED:`` text output, and
claim's error-envelope (ticket_not_found / concurrency_conflict / claim_failed) +
``CLAIMED:`` output.
"""

from __future__ import annotations

import json
import os
import sys

from rebar import config
from rebar._commands._seam import CommandError
from rebar._commands.transition_close import close_ticket
from rebar._commands.txn import ConcurrencyMismatch
from rebar._engine_support.output import OutputFormatError, error_envelope, parse_output
from rebar._engine_support.resolver import resolve_ticket_id
from rebar.reducer import reduce_ticket

_VALID_STATUSES = ("open", "in_progress", "closed", "blocked")

_USAGE = (
    "Usage: ticket transition <ticket_id> <current_status> <target_status> "
    "[--reason=<text>] [--force] [--force-close=<reason>]\n"
    "       ticket transition <ticket_id> <target_status> [--reason=<text>] [--force] "
    "[--force-close=<reason>]  (auto-detects current status)\n"
    "  current_status / target_status: open | in_progress | closed | blocked\n"
    "  Parent-first (open -> in_progress only): if the ticket has an OPEN parent, the\n"
    "  parent is transitioned first (recursively); a parent failure aborts the child\n"
    "  and the error names the parent. close/reopen/blocked never cascade.\n"
    "  --reason=<text>          Required when closing bug tickets. Must start with "
    "'Fixed:' or 'Escalated to user:'.\n"
    "  --force                  Bypass the plan-review gate when starting work "
    "(open->in_progress); the --reason text becomes the audit note. Does NOT bypass the "
    "unresolved-children close guard (a structural invariant — close/detach children first).\n"
    "  --verdict-hash=<hash>    DEPRECATED (ignored): the story/epic close gate now "
    "requires a certified signature ('rebar sign'), not a verdict hash.\n"
    "  --force-close=<reason>   Bypass the signature requirement for story/epic "
    "(requires user approval via hook).\n"
    "  Examples:\n"
    '    ticket transition abc1 open closed --reason="Fixed: patched null check in foo.sh"\n'
    "    rebar sign abc1 '[\"tests: PASS\"]' && ticket transition abc1 closed  "
    "# close story with a certified signature\n"
    '    ticket transition abc1 closed --force-close="verifier timed out"  # bypass with reason\n'
)


def _usage() -> int:
    sys.stderr.write(_USAGE)
    return 1


def _read_status(tracker: str, ticket_id: str) -> str | None:
    state = reduce_ticket(os.path.join(tracker, ticket_id))
    if state is None:
        return None
    status = state.get("status")
    if status in (None, "error", "fsck_needed"):
        return None
    return status


def _resolve_open_parent(tracker: str, ticket_id: str) -> str | None:
    """Return the resolved id of ``ticket_id``'s parent IFF the parent exists and is
    currently ``open`` — else ``None``.

    The parent-first cascade in :func:`claim_compute` / :func:`transition_compute`
    uses this: grabbing a child (claim, or transition ``open -> in_progress``) first
    grabs its OPEN parent. A parent that is already ``in_progress`` / ``closed`` /
    ``blocked`` (or absent / unreadable) yields ``None`` — no cascade, the child op
    proceeds alone."""
    state = reduce_ticket(os.path.join(tracker, ticket_id))
    if state is None:
        return None
    raw_parent = state.get("parent_id")
    if not raw_parent:
        return None
    parent_id = resolve_ticket_id(raw_parent, tracker) or raw_parent
    parent_state = reduce_ticket(os.path.join(tracker, parent_id))
    if parent_state is None or parent_state.get("status") != "open":
        return None
    return parent_id


def _parse_flags(args: list[str]) -> tuple[str, bool, str, str]:
    """Parse [--reason[=]] [--force] [--verdict-hash[=]] [--force-close[=]] from the
    args AFTER <current> <target>. Returns (reason, force, verdict_hash,
    force_close_reason). Mirrors ticket-transition.sh's flag loop (unknown tokens
    are silently skipped)."""
    reason = ""
    force = False
    verdict_hash = ""
    force_close = ""
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--reason="):
            reason = a[len("--reason=") :]
            i += 1
        elif a == "--reason":
            if i + 1 >= len(args):
                raise CommandError("Error: --reason requires a value", returncode=1)
            reason = args[i + 1]
            i += 2
        elif a == "--force":
            force = True
            i += 1
        elif a.startswith("--verdict-hash="):
            verdict_hash = a[len("--verdict-hash=") :]
            i += 1
        elif a == "--verdict-hash":
            if i + 1 >= len(args):
                raise CommandError("Error: --verdict-hash requires a value", returncode=1)
            verdict_hash = args[i + 1]
            i += 2
        elif a.startswith("--force-close="):
            force_close = a[len("--force-close=") :]
            i += 1
        elif a == "--force-close":
            if i + 1 >= len(args):
                raise CommandError("Error: --force-close requires a reason", returncode=1)
            force_close = args[i + 1]
            i += 2
        else:
            i += 1
    return reason, force, verdict_hash, force_close


def _validate_status(label: str, value: str) -> None:
    if value in _VALID_STATUSES:
        return
    if value.startswith("--"):
        raise CommandError(
            f"Error: invalid {label} '{value}'. Options like --reason must come AFTER "
            "<target_status>.\n"
            "  Correct: ticket transition <id> [<current_status>] <target_status> "
            '--reason="<text>"',
            returncode=1,
        )
    raise CommandError(
        f"Error: invalid {label} '{value}'. Must be one of: open, in_progress, closed, blocked",
        returncode=1,
    )


def transition_compute(
    ticket_id: str,
    current_status: str,
    target_status: str,
    *,
    reason: str = "",
    force: bool = False,
    verdict_hash: str = "",
    force_close: str = "",
    repo_root=None,
    cascade: bool = True,
    _cascade_seen: frozenset[str] | None = None,
) -> dict:
    """Validate, guard, write, and post-process a transition for an ALREADY-RESOLVED
    ticket id. Returns ``{ticket_id, from, to, newly_unblocked, noop}``. Raises
    :class:`ConcurrencyMismatch` (exit 10) / :class:`CommandError`. Does NOT parse
    ``--output`` or autodetect current — that is the CLI wrapper's job.

    Parent-first cascade: on an ``open -> in_progress`` transition, if the ticket has
    an OPEN parent the parent is transitioned first (recursively up the chain) before
    the child; a parent failure aborts the child with an error naming the parent. Pass
    ``cascade=False`` to suppress this for callers that replay an exact recorded state
    per-ticket (e.g. NDJSON import) where pre-moving a parent would conflict with that
    parent's own explicit transition. ``_cascade_seen`` is the internal recursion guard
    (the ids already on the cascade stack) — callers leave it ``None``."""
    tracker = str(config.tracker_dir(repo_root))
    repo_root_str = os.path.dirname(tracker)

    _validate_status("current_status", current_status)
    if target_status == "deleted":
        raise CommandError(
            f"Error: deleted is not a valid transition target -- use ticket delete "
            f"{ticket_id} to delete a ticket",
            returncode=1,
        )
    _validate_status("target_status", target_status)

    if current_status == target_status:
        # Same-status no-op short-circuits BEFORE the authoritative guard in
        # txn.transition_core, so refuse a session_log here too — it is
        # lifecycle-exempt and must never report a (no-op) transition success.
        from rebar.reducer import reduce_ticket

        _state = reduce_ticket(os.path.join(tracker, ticket_id))
        if _state is not None and _state.get("ticket_type") == "session_log":
            raise CommandError(
                "Error: session_log tickets are lifecycle-exempt and cannot be "
                "transitioned (they are not claimed, transitioned, or closed)",
                returncode=1,
            )
        return {
            "ticket_id": ticket_id,
            "from": current_status,
            "to": target_status,
            "newly_unblocked": [],
            "noop": True,
        }

    # Ghost check (ticket dir exists + has a CREATE/SNAPSHOT event).
    ticket_dir = os.path.join(tracker, ticket_id)
    if not os.path.isdir(ticket_dir):
        raise CommandError(f"Error: ticket '{ticket_id}' does not exist", returncode=1)
    if not any(
        (n.endswith("-CREATE.json") or n.endswith("-SNAPSHOT.json")) and not n.startswith(".")
        for n in os.listdir(ticket_dir)
    ):
        raise CommandError(
            f"Error: ticket {ticket_id} has no CREATE or SNAPSHOT event", returncode=1
        )

    if not os.path.isfile(os.path.join(tracker, ".env-id")):
        raise CommandError(
            "Error: ticket system not initialized. Run 'ticket init' first.", returncode=1
        )

    # Plan-review START-WORK gate. ANY entry into `in_progress` starts work on the
    # ticket's plan, so it goes through the SAME consolidated gate as `claim` (see
    # _commands/gates.py): blocks (fail-closed) on a missing/stale attestation when
    # enabled, exempts bug/session_log, and --force bypasses with an audit note (the
    # bypass reason is the --reason text). Keying on the TARGET (not `current=="open"`)
    # closes every side-door into in_progress — `open`, a `blocked` resume, or a
    # `closed`-then-reactivate — so un-reviewed work can't slip past via an alternate
    # edge. A same-status no-op was already short-circuited above, so reaching here with
    # target in_progress means current is open/blocked/closed. A legitimately-reviewed
    # ticket keeps a valid attestation and passes (including a normal block/resume).
    # cascade=False (replay/import re-materializing a recorded status verbatim) skips it.
    if cascade and target_status == "in_progress":
        from rebar._commands import gates

        # Gate THIS ticket first (mirrors claim_compute's order); the recursive parent
        # transition below gates the parent in turn, so every ticket in the chain that
        # starts work is gated. The --force bypass propagates up the cascade so a forced
        # start does not stall on an un-reviewed ancestor (claim/transition parity).
        force_reason = (reason or "(no reason given)") if force else ""
        gates.plan_review_precheck(ticket_id, repo_root_str, repo_root, force_reason=force_reason)

    # Parent-first cascade (open -> in_progress only): if this ticket has an OPEN
    # parent, transition it first (recursively up the chain) so a child is never
    # moved to in_progress while its parent is still open. See _cascade_open_parent.
    _cascade_open_parent(
        ticket_id,
        current_status,
        target_status,
        tracker,
        reason=reason,
        force=force,
        repo_root=repo_root,
        cascade=cascade,
        cascade_seen=_cascade_seen,
    )

    # Locked write + close-path tail: the open-children guard, the completion-
    # verification precheck (ordered verify -> close -> sign), the locked write,
    # post-close signing / force-close audit, and compact-on-close + scratch
    # cleanup + best-effort push. Lives in the sibling module (module-size seam);
    # see :func:`rebar._commands.transition_close.close_ticket`.
    return close_ticket(
        ticket_id,
        current_status,
        target_status,
        tracker,
        repo_root_str,
        repo_root,
        reason=reason,
        verdict_hash=verdict_hash,
        force_close=force_close,
    )


def _cascade_open_parent(
    ticket_id: str,
    current_status: str,
    target_status: str,
    tracker: str,
    *,
    reason: str,
    force: bool,
    repo_root,
    cascade: bool,
    cascade_seen: frozenset[str] | None,
) -> None:
    """Parent-first cascade for an ``open -> in_progress`` transition: if ``ticket_id``
    has an OPEN parent, transition the parent first (recursively up the chain, via
    :func:`transition_compute`) so a child is never moved to ``in_progress`` while its
    parent is still ``open``. If the parent transition fails, the child is NOT
    transitioned and the raised error names the parent as the cause (preserving the
    parent's exit code / concurrency identity — a raced parent surfaces as exit-10 /
    ConcurrencyError at the leaf too). ``cascade_seen`` breaks any malformed parent
    cycle.

    A no-op unless ``cascade`` is set AND this is the ``open -> in_progress`` edge (the
    cascade pulls an OPEN parent into progress; a blocked-ticket resume has no such
    parent semantics). ``cascade=False`` (replay/import re-materializing a recorded
    status verbatim) suppresses it. Mirrors :func:`claim_compute`'s cascade."""
    if not (cascade and current_status == "open" and target_status == "in_progress"):
        return
    seen = cascade_seen or frozenset()
    parent_id = _resolve_open_parent(tracker, ticket_id)
    if parent_id is not None and parent_id != ticket_id and parent_id not in seen:
        try:
            transition_compute(
                parent_id,
                "open",
                "in_progress",
                reason=reason,
                force=force,
                repo_root=repo_root,
                _cascade_seen=seen | {ticket_id},
            )
        except CommandError as exc:
            msg = (
                f"Error: cannot move {ticket_id} to in_progress: transitioning its "
                f"parent {parent_id} to in_progress failed first, so the child was not "
                f"transitioned.\n  Parent error: {exc.message}"
            )
            # Preserve the concurrency identity: a parent that raced surfaces as
            # exit-10 / ConcurrencyError at the leaf too. ConcurrencyMismatch
            # hardcodes returncode=10.
            if isinstance(exc, ConcurrencyMismatch):
                raise ConcurrencyMismatch(msg) from None
            raise CommandError(msg, returncode=exc.returncode) from None


def _unarchive(ticket_id: str, target_status: str, tracker: str, repo_root_str: str) -> int:
    """The ``archived → open`` un-archive seam: REVERT the latest live ARCHIVED
    event IN-PROCESS via :func:`rebar._commands.composer.revert_core` (Tier E
    E6.5a — replacing the ticket-revert.sh subprocess). Same REVERT event +
    ``.archived`` marker clear + ``Reverted event …`` confirmation the bash
    ``exec`` produced; ``--output`` and the UNBLOCKED block are skipped here."""
    if target_status != "open":
        sys.stderr.write(
            "Error: from 'archived' the only valid transition is to 'open' "
            f"(un-archive). Use: ticket transition {ticket_id} archived open\n"
        )
        return 1
    archived_uuid = _latest_live_archived_uuid(os.path.join(tracker, ticket_id))
    if not archived_uuid:
        sys.stderr.write("Error: no live ARCHIVED event (status may be stale)\n")
        return 1
    from rebar._commands._seam import CommandError
    from rebar._commands.composer import revert_core

    try:
        resolved = revert_core(
            ticket_id,
            archived_uuid,
            "un-archive via transition archived open",
            repo_root=repo_root_str,
        )
    except CommandError as exc:
        sys.stderr.write(exc.message + "\n")
        return exc.returncode
    sys.stdout.write(f"Reverted event '{archived_uuid}' on ticket '{resolved}'\n")
    return 0


def _latest_live_archived_uuid(ticket_dir: str) -> str:
    """UUID of the most recent ARCHIVED event not undone by a REVERT (mirrors the
    inline heredoc in ticket-transition.sh)."""
    archived: dict[str, int] = {}
    reverted: set[str] = set()
    try:
        names = os.listdir(ticket_dir)
    except OSError:
        return ""
    for fname in names:
        if fname.startswith(".") or not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(ticket_dir, fname), encoding="utf-8") as f:
                ev = json.load(f)
        except Exception:  # noqa: BLE001 — per-file best-effort event parse; skip an unreadable/corrupt event file
            continue
        et = ev.get("event_type")
        if et == "ARCHIVED":
            archived[ev.get("uuid", "")] = ev.get("timestamp", 0)
        elif et == "REVERT":
            t = ev.get("data", {}).get("target_event_uuid", "")
            if t:
                reverted.add(t)
    live = [(ts, u) for u, ts in archived.items() if u and u not in reverted]
    return max(live)[1] if live else ""


def _resolve_id_or_report(raw_id: str, tracker: str, fmt: str) -> str | None:
    """Resolve a ticket id for a ``*_cli`` handler; on failure emit the standard
    ``ticket_not_found`` envelope (when ``fmt == "json"``) plus the stderr line, and
    return ``None`` so the caller returns exit 1. Shared by ``transition_cli`` and
    ``claim_cli`` (the identical resolve-id-or-emit block was inlined in both)."""
    ticket_id = resolve_ticket_id(raw_id, tracker)
    if ticket_id is None:
        if fmt == "json":
            sys.stdout.write(
                json.dumps(
                    error_envelope("ticket_not_found", raw_id, f"Ticket '{raw_id}' not found", 1)
                )
                + "\n"
            )
        sys.stderr.write(f"Error: ticket '{raw_id}' not found\n")
    return ticket_id


def transition_cli(argv: list[str], *, repo_root=None) -> int:
    """``rebar transition`` entry: parse ``--output``, autodetect/validate, run the
    un-archive seam or :func:`transition_compute`, print, return the exit code."""
    try:
        fmt, rest = parse_output(argv, "report")
    except OutputFormatError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2

    if len(rest) < 1:
        return _usage()

    raw_id = rest[0]
    tracker = str(config.tracker_dir(repo_root))
    ticket_id = _resolve_id_or_report(raw_id, tracker, fmt)
    if ticket_id is None:
        return 1

    tail = rest[1:]
    if len(tail) < 1:
        return _usage()

    if len(tail) == 1:
        current_status = _read_status(tracker, ticket_id)
        if current_status is None:
            sys.stderr.write(
                f"Error: could not read current status for ticket '{ticket_id}'. "
                "Provide current_status explicitly.\n"
            )
            return _usage()
        target_status = tail[0]
        flag_args: list[str] = []
    else:
        current_status = tail[0]
        target_status = tail[1]
        flag_args = tail[2:]

    # Un-archive seam — before status validation (archived is not a valid status).
    if current_status == "archived":
        return _unarchive(ticket_id, target_status, tracker, os.path.dirname(tracker))

    try:
        reason, force, verdict_hash, force_close = _parse_flags(flag_args)
        result = transition_compute(
            ticket_id,
            current_status,
            target_status,
            reason=reason,
            force=force,
            verdict_hash=verdict_hash,
            force_close=force_close,
            repo_root=repo_root,
        )
    except ConcurrencyMismatch as exc:
        sys.stderr.write(exc.message + "\n")
        return 10
    except CommandError as exc:
        sys.stderr.write(exc.message + "\n")
        return exc.returncode

    if result["noop"]:
        sys.stdout.write("No transition needed\n")
        return 0

    if fmt == "json":
        sys.stdout.write(
            json.dumps(
                {
                    "ticket_id": ticket_id,
                    "from": current_status,
                    "to": target_status,
                    "newly_unblocked": result["newly_unblocked"],
                }
            )
            + "\n"
        )
    elif target_status == "closed":
        ids = result["newly_unblocked"]
        sys.stdout.write(f"UNBLOCKED: {','.join(ids) if ids else 'none'}\n")
    return 0


# The `claim` command cluster lives in :mod:`.claim` (module-size seam); re-export
# the names external callers use (rebar.claim → claim_compute; the CLI → claim_cli).
from rebar._commands.claim import claim_cli, claim_compute  # noqa: E402,F401


def reopen_cli(argv: list[str], *, repo_root=None) -> int:
    """``rebar reopen <id>`` → ``transition <id> closed open`` (uses only the first
    positional, like the dispatcher arm)."""
    try:
        fmt, rest = parse_output(argv, "report")
    except OutputFormatError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    if len(rest) < 1:
        from rebar._cli import _help

        text = _help.subcommand_help("reopen")
        if text:
            sys.stderr.write(text)
        return 1
    out_flag = ["--output", fmt] if fmt != "text" else []
    return transition_cli([rest[0], "closed", "open", *out_flag], repo_root=repo_root)
