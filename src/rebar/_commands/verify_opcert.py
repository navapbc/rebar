"""Required-environment completion-certificate merge gate (``rebar verify-opcert``).

For each closed ticket in merged history, verify its ``completion-verifier`` certificate
against the configured trusted environment. The terminal close STATUS commit anchors
grandfathering. The certificate's SIGNATURE commit anchors key-era validity. Missing or invalid
certificates fail enforced tickets, pre-boundary tickets are advisory, and an unset required
environment disables enforcement. Exit 2 reports config or store errors, 1 an enforced failure,
and 0 a pass or advisory result.
"""

from __future__ import annotations

import os
import sys

from rebar import config
from rebar._cli._parser import guard_parse_errors
from rebar._cli._parsers.advanced.verify import build_opcert
from rebar._mcp_errors import js_safe_dumps

KIND = "completion-verifier"


def _close_anchor_event(events, ticket_id):
    """Return the last close-STATUS event for the ticket, or ``None``.

    Collection order is timestamp order, so the last match is the terminal enforcement anchor."""
    anchor = None
    for ev in events:
        if ev.ticket_id != ticket_id or ev.event is None:
            continue
        if ev.event.get("event_type") != "STATUS":
            continue
        if (ev.event.get("data") or {}).get("status") == "closed":
            anchor = ev
    return anchor


def _opcert_anchor_event(events, ticket_id):
    """Return the last uncompacted completion-verifier SIGNATURE event, or ``None``.

    Its introducing commit is the storage anchor for key-era validation, independent of the
    certificate's claimed merged commit. Snapshot-only or missing records remain unresolved so
    the caller fails closed."""
    from rebar.reducer._processors import attestation_kind

    anchor = None
    for ev in events:
        if ev.ticket_id != ticket_id or ev.event is None:
            continue
        if ev.event.get("event_type") != "SIGNATURE":
            continue
        data = ev.event.get("data") or {}
        if not data.get("envelope"):
            continue
        if attestation_kind(data.get("manifest"), data) != KIND:
            continue
        anchor = ev
    return anchor


def _authoritative_material(ticket_id: str, repo_root) -> str | None:
    """Recompute the ticket's current material fingerprint from live state (the authoritative
    value the op-cert must bind), or ``None`` if it cannot be established (fail-closed)."""
    try:
        from rebar.llm.plan_review.attest import current_material_fingerprint

        return current_material_fingerprint(ticket_id, repo_root=repo_root)
    except Exception:  # noqa: BLE001 — cannot establish authoritative material → reject (fail-closed)
        return None


def _commit_in_gated_history(commit: str, repo_root) -> bool:
    """True iff ``commit`` is an ancestor of (or equal to) the gated code HEAD — i.e. a real commit
    in the main history under test, not an arbitrary/off-history value. Fail-closed on any error."""
    import subprocess

    root = repo_root or "."
    try:
        proc = subprocess.run(
            ["git", "-C", root, "merge-base", "--is-ancestor", commit, "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return proc.returncode == 0
    except Exception:  # noqa: BLE001 — git failure → not provably in history → reject (fail-closed)
        return False


@guard_parse_errors
def cli(argv: list[str]) -> int:
    p = build_opcert(prog="rebar verify-opcert")
    args = p.parse_args(argv)

    try:
        cfg = config.compose_config(root=args.root)
    except config.ConfigError as exc:
        print(f"verify-opcert: {exc}", file=sys.stderr)
        return 2

    required_env = args.require_environment or cfg.verify.require_environment
    since_ref = args.since if args.since is not None else cfg.verify.opcert_enforce_since
    as_json = args.format == "json"

    tracker = str(config.tracker_dir(args.root))
    if not os.path.isdir(tracker):
        print(
            f"verify-opcert: ticket store not found at {tracker!r} "
            "(infrastructure issue — the tickets store is not mounted; not an op-cert problem)",
            file=sys.stderr,
        )
        return 2

    # Local imports keep the CLI intercept lean and reuse the authorship gate's merged-log walk /
    # introducing-commit resolution / grandfathering rule verbatim.

    import rebar
    from rebar.attest import authorship, opcert, trusted_env

    from .verify_authorship import _collect_all, _is_enforced, _resolve_commit

    events = _collect_all(tracker)
    commit_map = authorship.build_introducing_commit_map(repo_root=args.root)

    ticket_ids = sorted({ev.ticket_id for ev in events if ev.ticket_id})

    report: list[dict] = []
    problems: list[tuple[str, str, bool]] = []  # (ticket_id, reason, grandfathered)
    in_scope = 0
    satisfied = 0
    enforced_not_satisfied = 0

    for ticket_id in ticket_ids:
        # IN SCOPE for the completion-verifier lane iff the ticket is CLOSED.
        try:
            state = rebar.show_ticket(ticket_id, repo_root=args.root)
        except Exception:  # noqa: BLE001 — an unreadable ticket is treated as out of scope
            continue
        if not isinstance(state, dict) or state.get("status") != "closed":
            continue
        in_scope += 1
        # Resolve the terminal close STATUS as the enforcement anchor. A compacted or missing
        # event leaves no proven grandfathering commit, so ``_is_enforced`` fails closed.
        anchor = _close_anchor_event(events, ticket_id)
        close_commit = (
            _resolve_commit(anchor, args.root, commit_map) if anchor is not None else None
        )

        # Read + verify the ticket's completion-verifier op-cert against the PINNED key.
        reason = ""
        ok = False
        if not required_env:
            reason = "no required environment configured"
        else:
            rec = (state.get("attestations") or {}).get(KIND)
            got = opcert.opcert_from_record(rec) if isinstance(rec, dict) else None
            if got is None:
                reason = "missing op-cert"
            else:
                envelope, bound = got
                merged_commit = bound.get("merged_log_commit")
                # Recompute material from the current ticket state. Do not trust the
                # self-reported record because ticket-branch data bypasses Gerrit review.
                auth_material = _authoritative_material(ticket_id, args.root)
                # Resolve storage anchor S from the terminal certificate SIGNATURE event and
                # judge trusted-key era at S, not at the claimed merged commit. Unresolvable S
                # fails closed.
                s_anchor = _opcert_anchor_event(events, ticket_id)
                s_commit = (
                    _resolve_commit(s_anchor, args.root, commit_map)
                    if s_anchor is not None
                    else None
                )
                s_position = s_anchor.position if s_anchor is not None else None
                if auth_material is None:
                    reason = "cannot recompute authoritative material fingerprint"
                elif not isinstance(merged_commit, str) or not merged_commit:
                    reason = "malformed op-cert (missing merged_log_commit)"
                elif not _commit_in_gated_history(merged_commit, args.root):
                    # The bound merged-log commit must be a REAL commit in the gated main history
                    # (ancestor of HEAD), not an arbitrary/off-history value. It no longer anchors
                    # key validity, but still constrains the code-state claim the cert makes.
                    reason = "op-cert merged_log_commit is not in the gated main history"
                elif s_commit is None:
                    # The cert's storage anchor could not be resolved (its SIGNATURE event was
                    # compacted, or is otherwise unresolvable) → we cannot judge key era-validity at
                    # S, so we must NOT certify. Fail closed.
                    reason = "cannot resolve op-cert storage anchor (compacted/unresolvable)"
                else:
                    verdict = trusted_env.verify_required_environment(
                        envelope,
                        ticket_id,
                        auth_material,
                        merged_commit,
                        required_env,
                        kind=KIND,
                        storage_anchor_commit=s_commit,
                        storage_anchor_position=s_position,
                        repo_root=args.root,
                    )
                    ok = bool(verdict.verified)
                    if not ok:
                        reason = f"invalid op-cert ({verdict.verdict}: {verdict.reason})"

        if ok:
            satisfied += 1
            continue
        enforced = _is_enforced(close_commit, since_ref, tracker)
        grandfathered = not enforced
        if enforced:
            enforced_not_satisfied += 1
        problems.append((ticket_id, reason, grandfathered))
        report.append(
            {
                "ticket_id": ticket_id,
                "commit": close_commit,
                "reason": reason,
                "grandfathered": grandfathered,
            }
        )

    summary = (
        f"verify-opcert: {satisfied} satisfied, {len(problems)} unsatisfied "
        f"({in_scope} closed ticket(s) in scope)"
    )

    if as_json:
        print(js_safe_dumps(report))
        for tid, reason, gf in problems:
            print(f"  {tid}: {reason}{' [grandfathered]' if gf else ''}", file=sys.stderr)
        print(summary, file=sys.stderr)
    else:
        for tid, reason, gf in problems:
            print(f"  {tid}: {reason}{' [grandfathered]' if gf else ''}")
        print(summary)

    out = sys.stderr if as_json else sys.stdout
    if not required_env:
        print(
            "verify-opcert: advisory — no required environment configured "
            f"({in_scope} closed ticket(s) not enforced).",
            file=out,
        )
        return 0
    if enforced_not_satisfied:
        print(
            f"verify-opcert: FAIL — {enforced_not_satisfied} enforced closed ticket(s) "
            f"lack a valid op-cert from {required_env} (enforcement on).",
            file=sys.stderr,
        )
        return 1
    print(
        f"verify-opcert: OK — every enforced closed ticket carries a valid op-cert from "
        f"{required_env}.",
        file=out,
    )
    return 0
