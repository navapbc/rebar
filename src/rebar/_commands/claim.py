"""In-process ``claim`` command + the plan-review CLAIM gate.

Extracted from :mod:`rebar._commands.transition` (call-graph seam: the ``claim``
command cluster) so each module stays within the module-size budget. Owns the claim
arg parsing (``--assignee`` / ``--force``), the plan-review claim-gate precheck
(epic 5fd2 — a fast LOCAL signature check, no LLM on the claim path), the locked
claim core call, and the dispatcher-identical CLAIMED / error-envelope output.
``transition`` re-exports :func:`claim_cli` + :func:`claim_compute` for callers.
"""

from __future__ import annotations

import json
import os
import sys

from rebar import config
from rebar._commands import gates, txn
from rebar._commands._seam import CommandError
from rebar._commands.txn import ConcurrencyMismatch
from rebar._engine_support.output import OutputFormatError, error_envelope, parse_output

_CLAIM_USAGE = (
    "Usage: ticket claim <ticket_id> [--assignee=<name>] [--force[=<reason>]]\n"
    "  Claims an OPEN ticket (-> in_progress) and sets its assignee atomically.\n"
    "  Exits 10 if the ticket is not open (someone else already claimed it).\n"
    "  Parent-first: if the ticket has an OPEN parent, the parent is claimed first\n"
    "  (recursively, same assignee); a parent failure aborts the child and the\n"
    "  error names the parent.\n"
    "  --force bypasses the plan-review start-work gate (when enabled) with an audit note.\n"
)


def _parse_force(args: list[str]) -> str:
    """Parse [--force[=<reason>]] from claim's args. A bare ``--force`` yields a
    default justification so the bypass is still audit-logged (never empty)."""
    for i, a in enumerate(args):
        if a.startswith("--force="):
            return a[len("--force=") :] or "(no reason given)"
        if a == "--force":
            nxt = args[i + 1] if i + 1 < len(args) else ""
            return nxt if nxt and not nxt.startswith("--") else "(no reason given)"
    return ""


def _parse_assignee(args: list[str]) -> str | None:
    """Parse [--assignee[=]<name>] from claim's args (other tokens skipped),
    mirroring ticket-claim.sh. Raises :class:`CommandError` on a value-less flag.

    Returns ``None`` when ``--assignee`` is ABSENT (the "unspecified" sentinel that
    triggers the configured ``ticket.default_assignee`` fallback); returns the given
    value — possibly an explicit empty string — when the flag IS present (an explicit
    ``--assignee ""`` clears the assignee and never falls back). Story c36c / f.ffea27."""
    assignee: str | None = None
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--assignee="):
            assignee = a[len("--assignee=") :]
            i += 1
        elif a == "--assignee":
            if i + 1 >= len(args):
                raise CommandError("Error: --assignee requires a value", returncode=1)
            assignee = args[i + 1]
            i += 2
        else:
            i += 1
    return assignee


def _config_default_assignee(tracker: str) -> str:
    """The configured ``ticket.default_assignee`` (env > file), or ``""`` if unset or
    unreadable. Read from the repo root (the tracker's parent), where ``rebar.toml`` /
    ``.rebar/`` live — the same cfg root the plan-review gate uses. A malformed config
    must never break a claim, so any load error degrades to no default."""
    try:
        return config.load_config(root=os.path.dirname(tracker)).ticket.default_assignee or ""
    except Exception:  # noqa: BLE001 — non-critical: fall back to no default, never fail the claim
        return ""


def claim_compute(
    ticket_id: str,
    *,
    assignee: str | None = None,
    force_plan_review: str = "",
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

    # Default-assignee fallback (story c36c): resolve an UNSPECIFIED assignee (None)
    # to the configured ticket.default_assignee HERE — before the parent-cascade
    # recursion below — so a cascaded parent claim inherits the same resolved default
    # (advisory f7ca28). An explicit "" (clear) is left untouched and never falls back.
    if assignee is None:
        assignee = _config_default_assignee(tracker)

    # Plan-review start-work gate (opt-in; runs OUTSIDE the lock — a fast LOCAL HMAC
    # verify, no LLM/network). Blocks (fail-closed) on a missing/stale/wrong
    # attestation when enabled; --force bypasses with an audit comment. The same
    # consolidated check guards `transition open -> in_progress` (see _commands/gates.py),
    # so the two start-work paths can't diverge. cfg_root is the REPO root (parent of
    # the tracker), where .rebar/config.conf lives.
    gates.plan_review_precheck(
        ticket_id, os.path.dirname(tracker), repo_root, force_reason=force_plan_review
    )

    # Parent-first cascade: if this ticket has an OPEN parent, claim the parent first
    # (recursively up the chain) so a child is never claimed while its parent is still
    # open. If the parent claim fails, the child is NOT claimed and the error names the
    # parent as the cause. _cascade_seen breaks any malformed parent cycle. The helper
    # lives in .transition (claim re-exports through it); import lazily to avoid a cycle.
    from rebar._commands.transition import _resolve_open_parent

    seen = _cascade_seen or frozenset()
    parent_id = _resolve_open_parent(tracker, ticket_id)
    if parent_id is not None and parent_id != ticket_id and parent_id not in seen:
        try:
            claim_compute(
                parent_id,
                assignee=assignee,
                force_plan_review=force_plan_review,
                repo_root=repo_root,
                _cascade_seen=seen | {ticket_id},
            )
        except CommandError as exc:
            # TOCTOU: the cascade DECISION above read the parent as ``open`` WITHOUT the
            # write lock. A concurrent agent may have moved the parent
            # ``open -> in_progress`` between that read and this locked parent claim, so
            # the claim we just attempted was rejected. Re-check the parent's live
            # status: if it is no longer ``open`` (a peer progressed it — or it is now
            # closed/blocked), the cascade's whole purpose (never work a child under an
            # OPEN parent) is already satisfied, so this is BENIGN — fall through and
            # claim the child, matching the single-agent contract "parent already
            # in_progress -> only the requested ticket moves". Only a parent still
            # genuinely ``open`` (e.g. its own gate blocked the claim) is a real failure
            # that must abort the child.
            if _resolve_open_parent(tracker, ticket_id) is None:
                pass  # parent progressed concurrently; proceed to claim the child
            else:
                msg = (
                    f"Error: cannot claim {ticket_id}: claiming its parent {parent_id} failed "
                    f"first, so the child was not claimed.\n  Parent error: {exc.message}"
                )
                # Preserve the concurrency identity: a parent that raced surfaces as
                # exit-10 / ConcurrencyError at the leaf too (so the "pick another" retry
                # path still fires). ConcurrencyMismatch hardcodes returncode=10.
                if isinstance(exc, ConcurrencyMismatch):
                    raise ConcurrencyMismatch(msg) from None
                raise CommandError(msg, returncode=exc.returncode) from None

    from rebar._commands import _seam

    env_id = _seam.env_id(config.tracker_dir(repo_root))
    author = _seam.author("Unknown")
    # claim_core stamps attribution + signs the STATUS + EDIT events via the shared finalize
    # seam (bug 0ba4), given repo_root.
    txn.claim_core(
        tracker,
        ticket_id,
        env_id=env_id,
        author=author,
        assignee=assignee,
        repo_root=repo_root,
    )
    # claim_core commits inline (not via write_and_push); push best-effort so a
    # claim that isn't followed by an append_event write still reaches origin.
    from rebar._store import push

    push.push_after_commit(tracker)

    # Best-effort cross-clone claim-loss detection (audit reliability #1, story 3003).
    # If push_after_commit merged a competing claim that was already on the remote, the
    # locally-reduced `assignee` (the authoritative ownership field, HLC last-writer-wins)
    # now reflects that other clone's ownership. Surface the loss so the losing agent stops
    # instead of duplicating work. When no competing merge is visible (offline, sync.push
    # off/async, unreachable remote), the assignee is still ours and we return success
    # unchanged — a resolved fork is still discoverable later via fsck/show.
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
    locked claim core, and emit the dispatcher-identical CLAIMED / error-envelope
    output."""
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
    force_plan_review = _parse_force(rest[1:])

    tracker = str(config.tracker_dir(repo_root))
    ticket_id = _resolve_id_or_report(raw_id, tracker, fmt)
    if ticket_id is None:
        return 1

    try:
        result = claim_compute(
            ticket_id, assignee=assignee, force_plan_review=force_plan_review, repo_root=repo_root
        )
    except ConcurrencyMismatch as exc:
        sys.stderr.write(exc.message + "\n")
        if fmt == "json":
            sys.stdout.write(
                json.dumps(
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
                json.dumps(
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
        sys.stdout.write(json.dumps(result) + "\n")
    else:
        resolved = result.get("assignee")
        suffix = f" (assignee: {resolved})" if resolved else ""
        sys.stdout.write(f"CLAIMED: {ticket_id}{suffix}\n")
    return 0
