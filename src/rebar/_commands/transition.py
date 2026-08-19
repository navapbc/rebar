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
from rebar._commands._seam import CommandError, write_arg_parser
from rebar._commands.transition_close import close_ticket
from rebar._commands.txn import ConcurrencyMismatch
from rebar._engine_support.output import OutputFormatError, error_envelope, parse_output
from rebar._engine_support.resolver import resolve_ticket_id
from rebar.reducer import reduce_ticket

_VALID_STATUSES = ("idea", "open", "in_progress", "closed", "blocked")

# The close-gate escape hatch retired by ticket 24f7 in favour of a single `--force`
# (see :func:`_parse_flags`). Built from parts so a tree-wide grep for the dead flag
# spelling stays clean while the parser can still recognise — and loudly reject — it.
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
    "  Parent-first (open -> in_progress only): if the ticket has an OPEN parent, the\n"
    "  parent is transitioned first (recursively); a parent failure aborts the child\n"
    "  and the error names the parent. close/reopen/blocked never cascade.\n"
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
    """Return the resolved id of ``ticket_id``'s parent IFF the parent exists and its
    current status is ``status`` — else ``None``.

    This is the lookup behind the parent-first cascade: the caller names the parent
    status that is ELIGIBLE to be cascaded on the edge being taken (``"open"`` for
    ``open -> in_progress``, ``"closed"`` for the ``closed -> open`` reopen). A parent
    in any other status (or absent / unreadable) yields ``None`` — no cascade, the
    child op proceeds alone."""
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
    """Parse [--reason[=]] [--class[=]] [--force[=]] [--caused-by[=]] [--ref[=]] from the args
    AFTER <current> <target>. Returns (reason, force_reason, close_class, caused_by, ref).
    Unknown tokens are silently skipped.

    ``--force`` / ``--force=<reason>`` (ticket 24f7) is THE single escape hatch, spelled
    exactly as ``claim --force[=<reason>]``. ``force_reason`` is ``None`` when the flag is
    ABSENT and a (possibly empty) string when present, so the caller can tell "not passed"
    from "passed with no value". Which gate it bypasses is decided by the target status:
    ``open -> in_progress`` bypasses the start-work (plan-review) gate; ``-> closed``
    bypasses the completion-verification / signature gate.

    It REPLACES the former close-only spelling outright — no alias (operator decision
    2026-08-07, clean break). Because this loop silently skips unknown tokens, a bare removal
    would have made a stale close-only invocation a SILENT no-op that closes the ticket
    through the very gate it meant to bypass. So the retired spelling is matched explicitly
    and rejected with an error naming ``--force``. The loop compares tokens exactly (there is
    no prefix/abbreviation matching here), so truncations stay unknown tokens rather than
    abbreviation-matching onto a live flag.

    ``--ref <ref>`` / ``--ref=<ref>`` (bug 80af) is the OPTIONAL completion-close-gate target:
    the git ref whose committed tree the gate verifies (and signs against) instead of HEAD.
    Default ``""`` (→ HEAD, today's behavior).

    ``--class <value>`` / ``--class=<value>`` (ticket ed13) is the REQUIRED bounded
    classification for a bug close (the vocabulary lives in ``txn.CLOSE_CLASSES``); it is
    parsed here and threaded to ``transition_core``, which validates it.

    ``--caused-by <id>`` / ``--caused-by=<id>`` (ticket 555e) is the OPTIONAL explicit culprit
    override for a bug close: it threads through to :func:`close_ticket`, which draws a
    best-effort ``caused_by`` link from the (now-closed) bug to that change/ticket. Absent an
    explicit value, the close auto-derives the culprit via git-blame.

    (The ``--verdict-hash`` flag was removed pre-1.0 — DE7; the story/epic close gate
    requires a certified signature via ``rebar sign``. It is now just an unknown
    token, silently skipped.)"""
    _reject_retired_force_close(args)
    force_reason, rest = _extract_force(args)
    parser = write_arg_parser()
    parser.add_argument("--reason", default="")
    parser.add_argument("--class", dest="close_class", default="")
    parser.add_argument("--caused-by", dest="caused_by", default="")
    parser.add_argument("--ref", default="")
    ns, _unknown = parser.parse_known_args(rest)
    return ns.reason, force_reason, ns.close_class, ns.caused_by, ns.ref


def _reject_retired_force_close(args: list[str]) -> None:
    """Reject the retired close-only spelling (``_RETIRED_FORCE_CLOSE``) EXPLICITLY (24f7).

    ``_parse_flags`` silently skips unknown tokens, so a bare removal would have turned a
    stale ``_RETIRED_FORCE_CLOSE`` invocation into a SILENT no-op that closes the ticket
    through the very gate it meant to bypass. Matching it here (bare or ``=<reason>``) and
    erroring — with a message naming ``--force`` as the replacement — keeps the failure
    loud."""
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
    """Pull transition's inline-only ``--force`` out of ``args``, returning
    ``(force_reason, remaining)``.

    ``--force`` is handled here rather than by argparse because argparse cannot express an
    inline-only optional value that is distinct from "flag absent" WITHOUT consuming a
    following token — and transition's ``--force`` must NEVER swallow the next argv token
    (ticket 24f7): the reason rides only via ``--force=<reason>``. ``force_reason`` is ``None``
    when the flag is ABSENT and a (possibly empty) string when present, so the caller can tell
    "not passed" from "passed with no value" (the None-vs-"" distinction drives the close-gate
    bypass). ``_RETIRED_FORCE_CLOSE`` is NOT matched here — it is rejected earlier by
    :func:`_reject_retired_force_close`."""
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
    (the ids already on the cascade stack) — callers leave it ``None``.

    ``force_reason`` is the single face of the one CLI ``--force[=<reason>]`` flag and the
    library ``force`` parameter (ticket blusterous-earthly-kitten): ``None`` means "not
    forcing"; any string (the audit reason, ``"(no reason given)"`` for a bare bypass)
    forces, bypassing BOTH the start-work plan-review gate AND the completion-verify close
    gate — whichever this transition hits — recording the reason in the audit trail. It
    replaces the former three-way ``force`` (bool) / ``force_reason`` / ``force_close``
    split. ``close_reason`` (ticket fc20) is the NON-force justification for a reason-only
    administrative close (``--class obsolete``/``wontfix``): it persists as the
    ``close_reason`` key on the close STATUS event and is signed into the disposition
    attestation — distinct from the force-bypass reason, which records why a gate was
    bypassed."""
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

    # Plan-review START-WORK gate. ANY entry into `in_progress` starts work on the
    # ticket's plan, so it goes through the SAME consolidated gate as `claim` (see
    # _commands/gates.py): blocks (fail-closed) on a missing/stale attestation when
    # enabled, exempts bug/session_log, and a non-None `force_reason` bypasses with an
    # audit note. Keying on the TARGET (not `current=="open"`)
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
        # starts work is gated. The force bypass propagates up the cascade so a forced
        # start does not stall on an un-reviewed ancestor (claim/transition parity).
        # The audit note is the `--force=<reason>` value, or `"(no reason given)"` for a
        # bare bypass — identical to `claim` (the `--reason` fallback was dropped in
        # blusterous-earthly-kitten so `--reason` feeds only close_reason).
        note = _force_note(force_reason)
        gates.plan_review_precheck(ticket_id, repo_root_str, repo_root, force_reason=note)

    # Parent-first cascade: on a cascading edge (open -> in_progress, and the
    # closed -> open reopen), if this ticket has a parent in the eligible status,
    # transition it first (recursively up the chain) so a child is never left ahead
    # of its parent in the lifecycle. See _cascade_parent_first.
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

    # Locked write + close-path tail: the open-children guard, the completion-
    # verification precheck (ordered verify -> close -> sign), the locked write,
    # post-close signing / force-close audit, and compact-on-close + scratch
    # cleanup + best-effort push. Lives in the sibling module (module-size seam);
    # see :func:`rebar._commands.transition_close.close_ticket`. The single
    # ``force_reason`` (None = not forcing) drives the close-gate bypass too: normalize it
    # to the audit-note string the close path records (empty = no bypass).
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
    """The audit-note string for a force bypass: ``""`` when not forcing (``force_reason``
    is ``None``), the reason when given, or ``"(no reason given)"`` for a bare bypass — the
    same placeholder ``claim`` uses (ticket blusterous-earthly-kitten). This is the single
    normalization point that turns the unified ``force_reason: str | None`` into the truthy
    audit string the start-work gate and the close path both consume."""
    if force_reason is None:
        return ""
    return force_reason or "(no reason given)"


# The cascading edges of the parent-first cascade, mapping the ``(current, target)``
# status edge the CHILD is taking to the parent status that is ELIGIBLE to cascade on
# it. Both entries move the parent along the SAME edge, ahead of the child:
#
#   open -> in_progress : an ``open`` parent is pulled into progress, so a descendant is
#                         never in progress while an ancestor is merely open;
#   closed -> open      : a ``closed`` parent is reopened, so a reopened descendant is
#                         never left under a still-closed ancestor (bug
#                         cranial-sulfur-peafowl).
#
# Every other edge — notably ``* -> closed`` (which has its own open-children guard) and
# ``* -> blocked`` — is absent and therefore never cascades.
_CASCADING_EDGES: dict[tuple[str, str], str] = {
    ("open", "in_progress"): "open",
    ("closed", "open"): "closed",
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
    """Parent-first cascade for a cascading edge (see :data:`_CASCADING_EDGES`): if
    ``ticket_id``'s parent sits in the status eligible for this edge, transition the
    parent along the SAME edge first (recursively up the chain, via
    :func:`transition_compute`) so a child never runs ahead of its parent — not into
    ``in_progress`` while the parent is still ``open``, and not back to ``open`` while
    the parent is still ``closed``.

    If the parent transition fails, the child is NOT transitioned and the raised error
    names the parent as the cause (preserving the parent's exit code / concurrency
    identity — a raced parent surfaces as exit-10 / ConcurrencyError at the leaf too).
    ``cascade_seen`` breaks any malformed parent cycle.

    A no-op unless ``cascade`` is set AND the edge is in :data:`_CASCADING_EDGES`;
    ``cascade=False`` (replay/import re-materializing a recorded status verbatim)
    suppresses it. Mirrors :func:`claim_compute`'s cascade, and like it is sequential
    and fail-fast rather than transactional: a parent already moved is not rolled back
    if the child then fails."""
    if not cascade:
        return
    parent_status = _CASCADING_EDGES.get((current_status, target_status))
    if parent_status is None:
        return
    seen = cascade_seen or frozenset()
    parent_id = _resolve_parent_in_status(tracker, ticket_id, parent_status)
    if parent_id is None or parent_id == ticket_id or parent_id in seen:
        return
    try:
        transition_compute(
            parent_id,
            current_status,
            target_status,
            reason=reason,
            force_reason=force_reason,
            repo_root=repo_root,
            _cascade_seen=seen | {ticket_id},
        )
    except CommandError as exc:
        msg = (
            f"Error: cannot move {ticket_id} to {target_status}: transitioning its "
            f"parent {parent_id} to {target_status} failed first, so the child was not "
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
                json.dumps(
                    error_envelope("ticket_not_found", raw_id, f"Ticket '{raw_id}' not found", 1)
                )
                + "\n"
            )
        sys.stderr.write(f"Error: ticket '{raw_id}' not found\n")
    return ticket_id


def _plain_reason_refused(reason: str, force: bool, target_status: str, close_class: str) -> bool:
    """Whether a ``--reason`` given WITHOUT ``--force`` must be refused.

    Post-unification (ticket blusterous-earthly-kitten) ``--reason`` records ONLY a
    reason-required disposition's ``close_reason`` — a close carrying
    ``--class obsolete``/``wontfix``/``not_a_bug``/``escalated`` (``txn.close_class_refusal``
    REQUIRES it unless a live replacement link stands in for the bug-only pair). It no longer
    doubles as the gate-bypass audit note; that note rides on ``--force``'s own value
    (``--force="<reason>"``). With ``--force`` present ``--reason`` is permissively ignored
    (not refused). Any OTHER plain transition silently discarding the text would be worse than
    refusing it. A guard function so ``transition_cli`` stays under its shrink-only complexity
    ceiling."""
    if not reason or force:
        return False
    from rebar._commands import close_disposition

    return not (
        target_status == "closed" and close_class in close_disposition.REASON_REQUIRED_CLASSES
    )


def _emit_transition_result(
    fmt: str,
    ticket_id: str,
    current_status: str,
    target_status: str,
    result: dict,
    verb: str,
) -> None:
    """Print ``transition``'s result on the active channel (ticket 6bda-9d58-8546-4638).

    The JSON success payload is the PRE-EXISTING shape, byte-identical; the no-op
    path (which never had a JSON shape) and the text confirmations follow the
    shared mutation contract. ``verb`` is ``"reopened"`` for the ``reopen`` alias —
    its line drops the unblocked segment (reopening cannot unblock anything) —
    and ``"transitioned"`` otherwise, where the line preserves the UNBLOCKED ids
    datum the old close-path output carried.
    """
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
        # Same field-by-field rebuild hazard as the library return: without this the
        # completion-signature marker never reaches a CLI consumer parsing --output json,
        # leaving a close that landed WITHOUT its signature undetectable except by reading
        # English off stderr (bug silvern-dewy-damselfly).
        if "completion_signature" in result:
            payload["completion_signature"] = result["completion_signature"]
        sys.stdout.write(json.dumps(payload) + "\n")
        return
    if verb == "reopened":
        _confirm.emit_text(f"reopened {ticket_id}: {current_status} -> {target_status}")
        return
    ids = result["newly_unblocked"]
    _confirm.emit_text(
        f"transitioned {ticket_id}: {current_status} -> {target_status}; "
        f"unblocked: {','.join(ids) if ids else 'none'}"
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
        return _unarchive(ticket_id, target_status, tracker, os.path.dirname(tracker))

    try:
        reason, force_reason, close_class, caused_by, ref = _parse_flags(flag_args)
        # One `--force[=<reason>]` flag, one unified bypass (ticket
        # blusterous-earthly-kitten). `force_reason` is None when `--force` is absent, "" for a
        # bare `--force` (normalized to "(no reason given)" downstream), or the `--force=<reason>`
        # text — and it bypasses BOTH the start-work and completion-verify close gates, exactly
        # as `claim --force[=<reason>]`. `--reason` no longer stands in as the force note; it
        # feeds only close_reason.
        force = force_reason is not None
        if _plain_reason_refused(reason, force, target_status, close_class):
            raise CommandError(
                "Error: --reason is only meaningful on a close whose --class is "
                "reason-required (obsolete, wontfix, not_a_bug, or escalated — where it "
                "records the disposition's close_reason). A plain transition discards it. Use "
                '--force="<reason>" to record a gate-bypass audit note, '
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
    out_flag = ["--output", fmt] if fmt != "text" else []
    return transition_cli(
        [rest[0], "closed", "open", *out_flag], repo_root=repo_root, _confirm_verb="reopened"
    )
