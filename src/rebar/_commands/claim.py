"""Claim command with plan-review precheck, parent cascade, and outcome
formatting. transition re-exports the entry points for existing callers."""

from __future__ import annotations

import os
import sys

from rebar import config
from rebar._commands import gates, txn
from rebar._commands._seam import CommandError
from rebar._commands.txn import ConcurrencyMismatch
from rebar._engine_support.output import OutputFormatError, error_envelope, parse_output
from rebar._mcp_errors import js_safe_dumps

_CLAIM_USAGE = (
    "Usage: ticket claim <ticket_id> [--assignee=<name>] [--force[=<reason>]] [--review]\n"
    "  Claims an OPEN ticket (-> in_progress) and sets its assignee atomically.\n"
    "  Exits 10 if the ticket is not open (someone else already claimed it).\n"
    "  Parent-first: if the ticket has an OPEN parent, the parent is claimed first\n"
    "  (recursively, same assignee); a parent failure aborts the child and the\n"
    "  error names the parent.\n"
    "  --force bypasses any enabled start-work gate (e.g. plan-review today, and any gate "
    "added in the future) with an audit note.\n"
    "  --review: when the plan-review gate applies and the attestation is stale/missing,\n"
    "  run `review-plan` (signed) first; claim only on PASS (BLOCK/INDETERMINATE/retryable\n"
    "  exit 1/2/11 without claiming). Not propagated to a cascaded parent claim.\n"
)


def _parse_force(args: list[str]) -> str:
    """Parse ``[--force[=<reason>]]`` from claim's args (``""`` when absent).

    Delegates to the ONE claim-style extractor so ``claim`` and ``link`` cannot drift on
    the spelling; the name is kept because tests bind to it.
    """
    from rebar._cli._parsers.core.writes import extract_force

    return extract_force(args)[0]


def _parse_assignee(args: list[str]) -> str | None:
    """Parse optional assignee, skipping unrelated tokens.
    None means use the configured default. An explicit empty value clears it.
    A flag without a value raises CommandError."""
    import re

    from rebar._cli._parser import ParseError
    from rebar._cli._parsers.core.lifecycle import build_claim

    parser = build_claim(prog="rebar claim")
    try:
        ns, _unknown = parser.parse_known_args(args)
    except ParseError as exc:
        m = re.match(r"argument (\S+): expected one argument", str(exc))
        if m:
            raise CommandError(f"Error: {m.group(1)} requires a value", returncode=1) from None
        raise CommandError(f"Error: {exc}", returncode=1) from None
    return ns.assignee


def _config_default_assignee(repo_root=None) -> str:
    """Read ticket.default_assignee from the repo root, not the tracker
    location. The environment override wins. Unset or unreadable config
    returns an empty string, including with a relocated tracker."""
    try:
        return config.compose_config(root=repo_root).ticket.default_assignee or ""
    except Exception:  # noqa: BLE001 — non-critical: fall back to no default, never fail the claim
        return ""


def _review_before_claim(ticket_id: str, tracker: str, repo_root) -> None:
    """For claim --review, check whether the gate applies and whether its signed
    review is current. Run full review only when stale or missing. Non-PASS
    blocks claim. Review runs before the claim's short write lock."""
    from rebar.reducer import reduce_ticket

    # repo_root (possibly None -> discover), NOT the tracker's parent: a relocated store
    # would otherwise resolve an empty config and the gate would read as disabled.
    cfg_root = repo_root
    ticket_type = (reduce_ticket(os.path.join(tracker, ticket_id)) or {}).get("ticket_type", "")
    if not gates._plan_review_gate_applies(cfg_root, ticket_type, ticket_id=ticket_id):
        sys.stderr.write("plan-review gate not enabled for this ticket; --review skipped\n")
        return
    from rebar import llm  # LAZY — preserves optionality, mirrors plan_review_precheck

    if llm.claim_gate_check(ticket_id, repo_root=repo_root).get("ok"):
        return  # attestation already current — nothing to review
    result = llm.review_plan(ticket_id, sign=True, repo_root=repo_root)
    # Lazy in-function import of the _cli helper from a _commands module — the
    # established pattern (see the lazy `from rebar._cli import _help` in
    # _commands/transition.py's reopen_cli).
    from rebar._cli._llm_commands import _disposition_exit_code, _render_plan_review_text

    _render_plan_review_text(result)
    code = _disposition_exit_code(result, indeterminate_code=2)
    if code != 0:
        raise CommandError(
            f"Error: claim of {ticket_id} not attempted: the plan review did not PASS "
            "(summary above). Revise the plan and re-run, or check currency with "
            f"`rebar review-plan {ticket_id} --status`.",
            returncode=code,
        )


def claim_compute(
    ticket_id: str,
    *,
    assignee: str | None = None,
    force_reason: str = "",
    review: bool = False,
    repo_root=None,
    _cascade_seen: frozenset[str] | None = None,
) -> dict:
    """Claim an ALREADY-RESOLVED ticket (ghost/init checks + the locked claim core).
    Returns ``{ticket_id, status, assignee}``; raises :class:`ConcurrencyMismatch`
    (exit 10) / :class:`CommandError`.

    Default-assignee fallback (story c36c): an UNSPECIFIED assignee (``None``) falls
    back to the configured ``ticket.default_assignee``; an explicit ``""`` clears and
    never falls back. The fallback is resolved at the TOP (before the cascade) so a
    cascaded parent claim inherits the same resolved default.

    Parent-first cascade: if the ticket has an OPEN parent the parent is claimed first
    (recursively up the chain, with the same assignee) before the child; a parent
    failure aborts the child with an error naming the parent. ``_cascade_seen`` is the
    internal recursion guard — callers leave it ``None``."""
    tracker = str(config.tracker_dir(repo_root))
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

    # Resolve the default before parent cascade. Preserve an explicit empty
    # assignee as a clear.
    if assignee is None:
        assignee = _config_default_assignee(repo_root)

    # Run any needed signed review before claim or parent cascade.
    # A non-PASS verdict blocks the write.
    if review:
        _review_before_claim(ticket_id, tracker, repo_root)

    # Check signed plan-review currency from the repo root outside the lock.
    # A reasoned force records the bypass.
    gates.plan_review_precheck(ticket_id, repo_root, repo_root, force_reason=force_reason)

    # Claim open parents first. A failed parent blocks the child. A parent
    # that already left open during the race lets the child proceed.
    from rebar._commands.lifecycle_cascade import cascade_parent_first
    from rebar._commands.transition import _resolve_open_parent

    cascade_parent_first(
        ticket_id,
        eligible_status="open",
        resolve_parent=lambda tid, _status: _resolve_open_parent(tracker, tid),
        advance=lambda parent_id, seen: claim_compute(
            parent_id,
            assignee=assignee,
            force_reason=force_reason,
            repo_root=repo_root,
            _cascade_seen=seen,
        ),
        action=f"claim {ticket_id}",
        parent_action="claimed",
        cascade_seen=_cascade_seen,
    )

    from rebar._commands import _seam

    env_id = _seam.env_id(config.tracker_dir(repo_root))
    author = _seam.author("Unknown")
    # The shared finalize seam attributes and signs claim events.
    txn.claim_core(
        tracker,
        ticket_id,
        env_id=env_id,
        author=author,
        assignee=assignee,
        repo_root=repo_root,
    )
    # Claim commits inline, then pushes best-effort.
    from rebar._store import push

    push.push_after_commit(tracker)

    # After push, detect a competing remote claim through the reduced
    # assignee and report lost ownership.
    if assignee:
        from rebar.reducer import reduce_ticket

        reduced = reduce_ticket(os.path.join(tracker, ticket_id))
        won = reduced.get("assignee") if reduced else None
        if won and won != assignee:
            raise ConcurrencyMismatch(
                f"claim lost on cross-clone merge: {ticket_id} is now assigned to {won!r}, "
                f"not {assignee!r} (a concurrent claim won the merge). Pick another ticket."
            )
    return {"ticket_id": ticket_id, "status": "in_progress", "assignee": assignee or None}


def claim_cli(argv: list[str], *, repo_root=None) -> int:
    """``rebar claim`` entry: parse ``--output`` / ``--assignee``, resolve, run the
    locked claim core, and emit the ``claimed`` confirmation / error-envelope output."""
    # Lazy import avoids a transition<->claim import cycle (transition re-exports us).
    from rebar._commands.transition import _resolve_id_or_report

    try:
        fmt, rest = parse_output(argv, "report")
    except OutputFormatError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    if len(rest) < 1:
        sys.stderr.write(_CLAIM_USAGE)
        return 1

    raw_id = rest[0]
    try:
        assignee = _parse_assignee(rest[1:])
    except CommandError as exc:
        sys.stderr.write(exc.message + "\n")
        return exc.returncode
    force_reason = _parse_force(rest[1:])
    review = "--review" in rest[1:]

    tracker = str(config.tracker_dir(repo_root))
    ticket_id = _resolve_id_or_report(raw_id, tracker, fmt)
    if ticket_id is None:
        return 1

    try:
        result = claim_compute(
            ticket_id,
            assignee=assignee,
            force_reason=force_reason,
            review=review,
            repo_root=repo_root,
        )
    except ConcurrencyMismatch as exc:
        sys.stderr.write(exc.message + "\n")
        if fmt == "json":
            sys.stdout.write(
                js_safe_dumps(
                    error_envelope(
                        "concurrency_conflict",
                        raw_id,
                        f"Ticket '{ticket_id}' is not open (already claimed)",
                        10,
                    )
                )
                + "\n"
            )
        return 10
    except CommandError as exc:
        sys.stderr.write(exc.message + "\n")
        if fmt == "json":
            sys.stdout.write(
                js_safe_dumps(
                    error_envelope(
                        "claim_failed",
                        raw_id,
                        f"Failed to claim ticket '{ticket_id}'",
                        exc.returncode,
                    )
                )
                + "\n"
            )
        return exc.returncode

    # Use claim_compute's RETURN (the post-fallback assignee), not the raw parsed
    # value, so JSON + the report suffix reflect a default that was applied (f9ea30).
    if fmt == "json":
        sys.stdout.write(js_safe_dumps(result) + "\n")
    else:
        # Normalized confirmation (ticket 6bda-9d58-8546-4638): was
        # `CLAIMED: <id> (assignee: <who>)` — the id and the post-fallback assignee
        # both survive; the status hop claim performs is now explicit.
        from rebar._commands import _confirm

        resolved = result.get("assignee")
        suffix = f" (assignee {resolved})" if resolved else ""
        _confirm.emit_text(f"claimed {ticket_id}: open -> in_progress{suffix}")
    return 0
