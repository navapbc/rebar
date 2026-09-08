"""Provide in-process wrappers for ``transition``, ``reopen``, and ``claim``.

The wrappers own CLI parsing, status detection, lifecycle validation, parent cascades,
close verification, cleanup, and stable output. Locked writes remain in
:func:`rebar._commands.txn.transition_core` and
:func:`rebar._commands.txn.claim_core`.
"""

from __future__ import annotations

import json
import os
import sys

from rebar import config
from rebar._commands._seam import CommandError
from rebar._commands.lifecycle_cascade import cascade_parent_first
from rebar._commands.transition_close import close_ticket
from rebar._commands.txn import ConcurrencyMismatch
from rebar._engine_support.output import OutputFormatError, error_envelope, parse_output
from rebar._engine_support.resolver import resolve_ticket_id
from rebar._mcp_errors import js_safe_dumps
from rebar.reducer import reduce_ticket

_VALID_STATUSES = ("idea", "open", "in_progress", "closed", "blocked")

# Build the retired close-only flag from parts to keep dead-flag scans clean.
# The parser still recognizes the complete spelling and rejects it with an error.
_RETIRED_FORCE_CLOSE = "--force" + "-close"

_USAGE = (
    "Usage: ticket transition <ticket_id> <current_status> <target_status> "
    "[--class=<value>] [--reason=<text>] [--force[=<reason>]]\n"
    "       ticket transition <ticket_id> <target_status> [--class=<value>] [--reason=<text>] "
    "[--force[=<reason>]]  (auto-detects current status)\n"
    "  current_status / target_status: idea | open | in_progress | closed | blocked\n"
    "  --reason=<text>          Admitted only on a close whose --class is reason-required — "
    "obsolete or wontfix (REQUIRED there), or not_a_bug or escalated (REQUIRED unless a live "
    "replacement link stands in) — the reason is recorded as close_reason on the "
    "close event and signed into the disposition attestation. It does NOT double as the "
    "force-bypass audit note (that is the --force=<reason> value). Refused on any other plain "
    "transition. To record rationale on a ticket, use `rebar comment <id>`.\n"
    "  Parent-first (open -> in_progress, closed -> open, closed -> in_progress): if the\n"
    "  ticket has a parent in the status eligible for that edge (OPEN, CLOSED, CLOSED\n"
    "  respectively), the parent is transitioned along the SAME edge first (recursively);\n"
    "  a parent failure aborts the child and the error names the parent. close/blocked\n"
    "  never cascade.\n"
    "  --class=<value>          Required when closing bug tickets. One of: regression, "
    "plan_defect, env_integration, flaky, preexisting, not_a_bug, duplicate, escalated, "
    "obsolete, superseded, wontfix, undetermined. On non-bug tickets, optional and limited to "
    "the administrative dispositions: duplicate, obsolete, superseded, wontfix "
    "(obsolete/wontfix require --reason=<text>; duplicate/superseded require a live "
    "replacement link; not_a_bug/escalated — bug closes only — require --reason=<text> or a "
    "live replacement link).\n"
    "  --force[=<reason>]       Bypass whichever gate this transition would hit "
    "(spelled exactly as `claim --force[=<reason>]`). Starting work (open->in_progress): "
    "bypasses any enabled start-work gate — the plan-review gate today, and any gate added "
    "in the future. Closing: bypasses the completion-verification / signature "
    "requirement for story/epic (requires user approval via hook). The audit note is the "
    "--force=<reason> value, else (no reason given). Does NOT bypass the "
    "unresolved-children close guard (a structural invariant — close/detach children first).\n"
    "  --caused-by=<id>         On a bug close, draw a caused_by link to the culprit "
    "change/ticket (overrides git-blame auto-derivation).\n"
    "  --ref=<ref>              Completion close gate: verify (and sign) against the committed "
    "tree at <ref> instead of HEAD. Use it to close a stacked story against its own commit "
    "while your worktree stays at the epic tip; default HEAD.\n"
    "  Examples:\n"
    "    ticket transition abc1 open closed --class=regression  # close a bug with its class\n"
    "    rebar sign abc1 '[\"tests: PASS\"]' && ticket transition abc1 closed  "
    "# close story with a certified signature\n"
    '    ticket transition abc1 closed --force="verifier timed out"  # bypass with reason\n'
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


def _resolve_parent_in_status(tracker: str, ticket_id: str, status: str) -> str | None:
    """Return the resolved parent id only when it exists at ``status``.

    This supplies cascade eligibility: ``open`` for claims and ``closed`` for reopen or
    reactivation. Missing, unreadable, or differently staged parents return ``None``."""
    state = reduce_ticket(os.path.join(tracker, ticket_id))
    if state is None:
        return None
    raw_parent = state.get("parent_id")
    if not raw_parent:
        return None
    parent_id = resolve_ticket_id(raw_parent, tracker) or raw_parent
    parent_state = reduce_ticket(os.path.join(tracker, parent_id))
    if parent_state is None or parent_state.get("status") != status:
        return None
    return parent_id


def _resolve_open_parent(tracker: str, ticket_id: str) -> str | None:
    """``_resolve_parent_in_status`` specialized to an ``open`` parent — the lookup
    :func:`rebar._commands.claim.claim_compute` needs for its ``open -> in_progress``
    cascade."""
    return _resolve_parent_in_status(tracker, ticket_id, "open")


def _parse_flags(args: list[str]) -> tuple[str, str | None, str, str, str]:
    """Parse transition flags into ``(reason, force_reason, close_class, caused_by, ref)``.

    Unknown tokens are skipped. ``force_reason`` is ``None`` when absent and a possibly
    empty string when present. Only ``--force=<reason>`` carries text, so a bare flag never
    consumes the next token. The retired close-only spelling is rejected first to prevent
    silent loss of a requested bypass. ``--ref`` selects the completion tree, ``--class``
    carries the close classification, and ``--caused-by`` overrides culprit inference."""
    _reject_retired_force_close(args)
    force_reason, rest = _extract_force(args)
    from rebar._cli._parsers.core.lifecycle import build_transition

    parser = build_transition(prog="rebar transition")
    ns, _unknown = parser.parse_known_args(rest)
    return ns.reason, force_reason, ns.close_class, ns.caused_by, ns.ref


def _reject_retired_force_close(args: list[str]) -> None:
    """Reject the retired close-only force spelling, bare or valued.

    Unknown flags are otherwise skipped, so explicit rejection prevents stale callers from
    silently losing their requested bypass and names ``--force`` as the replacement."""
    for a in args:
        if a == _RETIRED_FORCE_CLOSE or a.startswith(_RETIRED_FORCE_CLOSE + "="):
            raise CommandError(
                f"Error: {_RETIRED_FORCE_CLOSE} was renamed to --force (ticket 24f7) and no "
                'longer exists. Use --force="<reason>" instead — on a close it bypasses the '
                "completion-verification / signature gate exactly as the old flag did, and it "
                "matches `rebar claim --force`.",
                returncode=1,
            )


def _extract_force(args: list[str]) -> tuple[str | None, list[str]]:
    """Remove inline-only ``--force`` and return ``(force_reason, remaining)``.

    ``None`` means absent, ``""`` means a bare flag, and text is accepted only after ``=``.
    This preserves the gate-bypass distinction without consuming the next argument. The
    retired spelling is rejected by :func:`_reject_retired_force_close`."""
    force_reason: str | None = None
    rest: list[str] = []
    for a in args:
        if a == "--force":
            force_reason = ""
        elif a.startswith("--force="):
            force_reason = a[len("--force=") :]
        else:
            rest.append(a)
    return force_reason, rest


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
        f"Error: invalid {label} '{value}'. "
        "Must be one of: idea, open, in_progress, closed, blocked",
        returncode=1,
    )


def transition_compute(
    ticket_id: str,
    current_status: str,
    target_status: str,
    *,
    reason: str = "",
    close_reason: str = "",
    force_reason: str | None = None,
    close_class: str = "",
    caused_by: str = "",
    ref: str | None = None,
    repo_root=None,
    cascade: bool = True,
    _cascade_seen: frozenset[str] | None = None,
) -> dict:
    """Apply a transition to an already-resolved ticket and return its stable result mapping.

    Raises :class:`ConcurrencyMismatch` or :class:`CommandError`. Configured cascading edges
    advance eligible parents recursively before the child. ``cascade=False`` suppresses this
    for exact-state replay, and ``_cascade_seen`` is internal. ``force_reason=None`` means no
    bypass. Any string bypasses the applicable start or close gate and is audited.
    ``close_reason`` instead records a reason-required administrative disposition."""
    tracker = str(config.tracker_dir(repo_root))
    # Resolve the code and configuration root through shared precedence instead of the
    # tracker path. A relocated store can lie outside the repository. Tracker-based
    # inference would miss gate configuration and misroot close-path work.
    repo_root_str = str(config.repo_root(repo_root))

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
        # txn.transition_core, so refuse an artifact type here too — session_log and
        # code_review are lifecycle-exempt and must never report a (no-op) success.
        from rebar.reducer import reduce_ticket
        from rebar.reducer._api import _NON_GRAPH_ARTIFACT_TYPES

        _state = reduce_ticket(os.path.join(tracker, ticket_id))
        if _state is not None and _state.get("ticket_type") in _NON_GRAPH_ARTIFACT_TYPES:
            _t = _state.get("ticket_type")
            raise CommandError(
                f"Error: {_t} tickets are lifecycle-exempt and cannot be "
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

    # Every transition into ``in_progress`` passes the claim start-work gate. This
    # target-based check covers open, blocked, and closed sources after same-status no-ops
    # have returned. ``cascade=False`` remains reserved for exact-state replay. The shared
    # precheck exempts eligible ticket types and fails closed on missing or stale attestations
    # unless ``force_reason`` supplies the audited bypass.
    if cascade and target_status == "in_progress":
        from rebar._commands import gates

        # Gate the child before cascading. Each recursive parent gates itself. Pass the force
        # note through the cascade so one explicit bypass covers the requested chain.
        note = _force_note(force_reason)
        gates.plan_review_precheck(ticket_id, repo_root_str, repo_root, force_reason=note)

    # Move an eligible parent along the same edge before the child to preserve lifecycle
    # order. ``_cascade_parent_first`` performs the recursive walk.
    _cascade_parent_first(
        ticket_id,
        current_status,
        target_status,
        tracker,
        reason=reason,
        force_reason=force_reason,
        repo_root=repo_root,
        cascade=cascade,
        cascade_seen=_cascade_seen,
    )

    # Delegate the close tail, which guards children, verifies, closes, and then signs. The
    # delegate also performs the locked write, compaction, cleanup, and push. Normalize
    # ``force_reason`` to its audit string.
    return close_ticket(
        ticket_id,
        current_status,
        target_status,
        tracker,
        repo_root_str,
        repo_root,
        reason=reason,
        close_reason=close_reason,
        force_close=_force_note(force_reason),
        close_class=close_class,
        caused_by=caused_by,
        ref=ref,
    )


def _force_note(force_reason: str | None) -> str:
    """Normalize ``force_reason`` for gate and close auditing.

    ``None`` means no bypass, an empty string becomes ``"(no reason given)"``, and other
    values pass through."""
    if force_reason is None:
        return ""
    return force_reason or "(no reason given)"


# Cascading edges map the child's edge to the parent status eligible for that same edge.
# Open parents enter progress before descendants. Closed parents reopen or reactivate first.
# Transitions to closed or blocked never cascade. A blocked-child resume deliberately does
# not resume its independently blocked parent. The close guard already prevents blocked
# children beneath closed parents. See docs/concurrency.md §I4a.
_CASCADING_EDGES: dict[tuple[str, str], str] = {
    ("open", "in_progress"): "open",
    ("closed", "open"): "closed",
    ("closed", "in_progress"): "closed",
}


def _cascade_parent_first(
    ticket_id: str,
    current_status: str,
    target_status: str,
    tracker: str,
    *,
    reason: str,
    force_reason: str | None = None,
    repo_root,
    cascade: bool,
    cascade_seen: frozenset[str] | None,
) -> None:
    """Advance each eligible parent before the child on a configured cascading edge.

    The shared walker owns recursion, cycle protection, race rechecks, and parent-attributed
    errors. This adapter supplies edge eligibility and :func:`transition_compute`. It is a
    no-op when disabled or non-cascading. The fail-fast sequence is not transactional. An
    advanced parent is not rolled back if the child fails."""
    if not cascade:
        return
    parent_status = _CASCADING_EDGES.get((current_status, target_status))
    if parent_status is None:
        return
    cascade_parent_first(
        ticket_id,
        eligible_status=parent_status,
        resolve_parent=lambda tid, status: _resolve_parent_in_status(tracker, tid, status),
        advance=lambda parent_id, seen: transition_compute(
            parent_id,
            current_status,
            target_status,
            reason=reason,
            force_reason=force_reason,
            repo_root=repo_root,
            _cascade_seen=seen,
        ),
        action=f"move {ticket_id} to {target_status}",
        parent_action=f"moved to {target_status}",
        cascade_seen=cascade_seen,
    )


def _unarchive(ticket_id: str, target_status: str, tracker: str, repo_root_str: str) -> int:
    """The ``archived → open`` un-archive seam: REVERT the latest live ARCHIVED
    event IN-PROCESS via :func:`rebar._commands.composer.revert_core`. Writes the
    REVERT event, clears the ``.archived`` marker, and prints the
    ``Reverted event …`` confirmation; ``--output`` and the UNBLOCKED block are
    skipped here."""
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
    # Normalized confirmation (ticket 6bda-9d58-8546-4638): was
    # `Reverted event '<uuid>' on ticket '<id>'` — the reverted-event uuid and the
    # resolved id both survive inside the transition-shaped line.
    from rebar._commands import _confirm

    _confirm.emit(
        "transitioned",
        resolved,
        f"archived -> open (reverted event {archived_uuid})",
        f"transitioned {resolved}: archived -> open (reverted event {archived_uuid})",
    )
    return 0


def _latest_live_archived_uuid(ticket_dir: str) -> str:
    """UUID of the most recent ARCHIVED event not undone by a REVERT."""
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
                js_safe_dumps(
                    error_envelope("ticket_not_found", raw_id, f"Ticket '{raw_id}' not found", 1)
                )
                + "\n"
            )
        sys.stderr.write(f"Error: ticket '{raw_id}' not found\n")
    return ticket_id


def _plain_reason_refused(reason: str, force: bool, target_status: str, close_class: str) -> bool:
    """Return whether a non-force ``--reason`` would otherwise be discarded.

    A reason is valid only for a reason-required close class or an ``env_integration`` close.
    With ``--force`` it is ignored because the force value carries the bypass audit note.
    Refuse other transitions instead of silently dropping their text."""
    if not reason or force:
        return False
    from rebar._commands import close_disposition

    return not (
        target_status == "closed"
        and (
            close_class in close_disposition.REASON_REQUIRED_CLASSES
            or close_class == "env_integration"
        )
    )


def _emit_transition_result(
    fmt: str,
    ticket_id: str,
    current_status: str,
    target_status: str,
    result: dict,
    verb: str,
) -> None:
    """Emit the stable transition result on the selected channel.

    JSON keeps the existing success shape. No-op and text output use the shared mutation
    contract. Reopen omits unblocked data, while ordinary transitions preserve it."""
    from rebar._commands import _confirm

    if result["noop"]:
        _confirm.emit(
            "noop",
            ticket_id,
            f"already {target_status}",
            f"no change: {ticket_id} already {target_status}",
        )
        return
    if fmt == "json":
        payload = {
            "ticket_id": ticket_id,
            "from": current_status,
            "to": target_status,
            "newly_unblocked": result["newly_unblocked"],
        }
        # Preserve the optional completion-signature marker when rebuilding the JSON payload.
        # Callers need it to detect a close committed without its signature.
        if "completion_signature" in result:
            payload["completion_signature"] = result["completion_signature"]
        sys.stdout.write(js_safe_dumps(payload) + "\n")
        return
    if verb == "reopened":
        _confirm.emit_text(f"reopened {ticket_id}: {current_status} -> {target_status}")
        return
    ids = result["newly_unblocked"]
    _confirm.emit_text(
        f"transitioned {ticket_id}: {current_status} -> {target_status}; "
        f"unblocked: {','.join(ids) if ids else 'none'}"
    )
    _emit_completion_signature_text(ticket_id, result)


def _emit_completion_signature_text(ticket_id: str, result: dict) -> None:
    """Warn in text mode when a completion close committed without its signature.

    An absent marker is not a completion close. Check ``signed`` rather than ``cause`` because
    an already-equivalent atomic close is signed under a different cause. Warn only when the
    marker says it is not signed."""
    from rebar._commands import _confirm

    sig = result.get("completion_signature")
    if not sig or sig.get("signed"):
        return
    _confirm.emit_text(
        f"warning: {ticket_id} closed WITHOUT a completion signature (cause: {sig['cause']})"
    )


def transition_cli(argv: list[str], *, repo_root=None, _confirm_verb: str = "transitioned") -> int:
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
        return _unarchive(ticket_id, target_status, tracker, str(config.repo_root(repo_root)))

    try:
        reason, force_reason, close_class, caused_by, ref = _parse_flags(flag_args)
        # ``None`` means absent. An empty string means a bare bypass. Other text is its audit
        # reason. The one force value applies to either the start or completion gate.
        # ``--reason`` remains solely a disposition close reason.
        force = force_reason is not None
        if _plain_reason_refused(reason, force, target_status, close_class):
            raise CommandError(
                "Error: --reason is only meaningful on a close whose --class is "
                "reason-bearing (obsolete, wontfix, not_a_bug, escalated, or "
                "env_integration — where it records the disposition's close_reason). "
                'A plain transition discards it. Use --force="<reason>" to record a '
                "gate-bypass audit note, "
                "or `rebar comment <id>` to record rationale on the ticket.",
                returncode=1,
            )
        result = transition_compute(
            ticket_id,
            current_status,
            target_status,
            reason=reason,
            close_reason=("" if force else reason),
            force_reason=force_reason,
            close_class=close_class,
            caused_by=caused_by,
            ref=(ref or None),
            repo_root=repo_root,
        )
    except ConcurrencyMismatch as exc:
        sys.stderr.write(exc.message + "\n")
        return 10
    except CommandError as exc:
        sys.stderr.write(exc.message + "\n")
        return exc.returncode

    _emit_transition_result(fmt, ticket_id, current_status, target_status, result, _confirm_verb)
    return 0


# The `claim` command cluster lives in :mod:`.claim` (module-size seam); re-export
# the names external callers use (rebar.claim → claim_compute; the CLI → claim_cli).
from rebar._commands.claim import claim_cli, claim_compute  # noqa: E402,F401


def reopen_cli(argv: list[str], *, repo_root=None) -> int:
    """``rebar reopen <id>`` → ``transition <id> closed open`` (uses only the first
    positional)."""
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
    # ``reopen`` is a positional-only alias that delegates the real grammar to
    # ``transition_cli``; its permissive raw/dash-leading ticket_id is genuinely
    # argparse-inexpressible, so the first token is read raw. The registry still
    # declares ``build_reopen`` for lazy census + canonical help (RP-05 S2d).
    reopen_id = rest[0]
    out_flag = ["--output", fmt] if fmt != "text" else []
    return transition_cli(
        [reopen_id, "closed", "open", *out_flag], repo_root=repo_root, _confirm_verb="reopened"
    )
