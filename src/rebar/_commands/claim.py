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
from rebar._commands import txn
from rebar._commands._seam import CommandError
from rebar._commands.txn import ConcurrencyMismatch
from rebar._engine_support.output import OutputFormatError, error_envelope, parse_output

_CLAIM_USAGE = (
    "Usage: ticket claim <ticket_id> [--assignee=<name>] [--force[=<reason>]]\n"
    "  Claims an OPEN ticket (-> in_progress) and sets its assignee atomically.\n"
    "  Exits 10 if the ticket is not open (someone else already claimed it).\n"
    "  --force bypasses the plan-review claim gate (when enabled) with an audit note.\n"
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


def _parse_assignee(args: list[str]) -> str:
    """Parse [--assignee[=]<name>] from claim's args (other tokens skipped),
    mirroring ticket-claim.sh. Raises :class:`CommandError` on a value-less flag."""
    assignee = ""
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


def _plan_review_precheck(
    ticket_id: str, cfg_root: str, repo_root, *, force_plan_review: str
) -> None:
    """The plan-review CLAIM gate (epic 5fd2; runs BEFORE the locked claim core).

    When ``verify.require_plan_review_for_claim`` is on, claiming a work ticket
    requires a fresh, certified plan-review attestation (earn one with
    ``rebar review-plan <id>``). This is a FAST, LOCAL HMAC verify + freshness/
    material binding — NO LLM and NO network call (the heavy review is out-of-band).
    Bugs and session_logs are EXEMPT. ``--force`` bypasses with an audit comment.
    Raises :class:`CommandError` (block) when the attestation is absent/stale/wrong.
    Returns ``None`` (allow) when the gate is off, the ticket is exempt, or the
    attestation is valid."""
    from rebar._commands import gates
    from rebar.reducer import reduce_ticket as _reduce

    # Shared resolution + fail-OPEN-on-unreadable-config posture (see _commands/gates.py),
    # mirroring the completion close gate so the two can't drift.
    if not gates.gate_enabled(
        cfg_root,
        "require_plan_review_for_claim",
        ticket_id=ticket_id,
        gate_label="the plan-review claim gate",
    ):
        return None
    ticket_type = (_reduce(os.path.join(str(config.tracker_dir(repo_root)), ticket_id)) or {}).get(
        "ticket_type", ""
    )
    if ticket_type in ("bug", "session_log"):
        return None  # exempt from the plan-review gate
    if force_plan_review:
        # Audit the bypass (best-effort) so a forced claim is a durable signal.
        try:
            from rebar._commands import leaf

            leaf.comment(
                ticket_id,
                "FORCE_CLAIM: plan-review gate bypassed by user approval — no plan-review "
                f'attestation was verified. Reason: "{force_plan_review}".',
                repo_root=repo_root,
            )
        except Exception:
            pass
        return None
    from rebar import llm  # LAZY — preserves optionality (claim_gate_check is stdlib-only though)

    check = llm.claim_gate_check(ticket_id, repo_root=repo_root)
    if check.get("ok"):
        return None
    raise CommandError(
        f"Error: cannot claim {ticket_id}: {check.get('reason')}.\n"
        "  The plan-review claim gate is enabled (verify.require_plan_review_for_claim).\n"
        "  Recovery: run the plan review to earn an attestation, then claim:\n"
        f"    rebar review-plan {ticket_id}\n"
        f"    rebar claim {ticket_id}\n"
        '  Override: use --force="<reason>" to bypass (requires user approval).',
        returncode=1,
    )


def claim_compute(
    ticket_id: str, *, assignee: str = "", force_plan_review: str = "", repo_root=None
) -> dict:
    """Claim an ALREADY-RESOLVED ticket (ghost/init checks + the locked claim core).
    Returns ``{ticket_id, status, assignee}``; raises :class:`ConcurrencyMismatch`
    (exit 10) / :class:`CommandError`."""
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

    # Plan-review claim gate (opt-in; runs OUTSIDE the lock — a fast LOCAL HMAC
    # verify, no LLM/network). Blocks (fail-closed) on a missing/stale/wrong
    # attestation when enabled; --force bypasses with an audit comment. cfg_root is
    # the REPO root (parent of the tracker), where .rebar/config.conf lives.
    _plan_review_precheck(
        ticket_id, os.path.dirname(tracker), repo_root, force_plan_review=force_plan_review
    )

    from rebar._commands import _seam

    env_id = _seam.env_id(config.tracker_dir(repo_root))
    author = _seam.author("Unknown")
    txn.claim_core(tracker, ticket_id, env_id=env_id, author=author, assignee=assignee)
    # claim_core commits inline (not via write_and_push); push best-effort so a
    # claim that isn't followed by an append_event write still reaches origin.
    from rebar._store import push

    push.push_after_commit(tracker)
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
        claim_compute(
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
                        "claim_failed", raw_id, f"Failed to claim ticket '{ticket_id}'", 1
                    )
                )
                + "\n"
            )
        return 1

    if fmt == "json":
        sys.stdout.write(
            json.dumps(
                {"ticket_id": ticket_id, "status": "in_progress", "assignee": assignee or None}
            )
            + "\n"
        )
    else:
        suffix = f" (assignee: {assignee})" if assignee else ""
        sys.stdout.write(f"CLAIMED: {ticket_id}{suffix}\n")
    return 0
