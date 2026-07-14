"""``rebar verify-opcert`` — required-environment operation-certificate merge-gate (story 4214).

The op-cert lane of the shipped ``verify-identity`` merge-gate: it walks the MERGED LOG, groups by
ticket, and for each in-scope CLOSED ticket verifies that the required trusted environment produced
a valid ``completion-verifier`` operation certificate (a DSSE envelope stored on the ticket by the
e4df keystone). Posture + grandfather boundary come from ``rebar.toml`` —
``verify.require_environment`` (which environment must sign) and ``verify.opcert_enforce_since``
(the grandfather ref) — exactly as
``verify-identity`` reads ``identity.*`` (no CI variable).

* A ticket is IN SCOPE when it has a terminal ``STATUS`` event whose target is ``closed`` (rebar has
  no separate CLOSE event type). Its enforcement anchor is that event's introducing commit.
* ENFORCED (anchor is a descendant of ``verify.opcert_enforce_since``) + missing/foreign/wrong-era
  op-cert → the gate FAILs (exit 1). Grandfathered (anchor predates the boundary) → advisory.
* ``verify.require_environment`` unset → advisory everywhere (exit 0).

Verification of a present cert delegates to ``trusted_env.verify_required_environment`` (verify
against the PINNED key, not the cert's self-claimed keyid); the stored envelope is read with
``opcert.opcert_from_record``.

The exit-code contract + walk are pinned by the RED oracle: 2 = infra error (no config/store),
1 = an ENFORCED closed ticket lacks a valid op-cert (enforcement on), 0 = pass/advisory.
"""

from __future__ import annotations

import argparse
import os
import sys

from rebar import config

KIND = "completion-verifier"

_USAGE = (
    "rebar verify-opcert [--require-environment <env_id>] [--since <ref>] "
    "[--format {text,json}] [--root <path>]"
)


def _close_anchor_event(events, ticket_id):
    """The terminal close-STATUS scoped event for ``ticket_id`` (a STATUS event whose
    ``data.status`` is ``closed``), or ``None`` if the ticket has no close event in scope.

    Later events sort after earlier ones (``_collect_all`` walks event files in filename =
    timestamp order), so the LAST close-STATUS seen is the terminal one — the enforcement anchor."""
    anchor = None
    for ev in events:
        if ev.ticket_id != ticket_id or ev.event is None:
            continue
        if ev.event.get("event_type") != "STATUS":
            continue
        if (ev.event.get("data") or {}).get("status") == "closed":
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
        )
        return proc.returncode == 0
    except Exception:  # noqa: BLE001 — git failure → not provably in history → reject (fail-closed)
        return False


def cli(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="rebar verify-opcert",
        usage=_USAGE,
        description=(
            "Verify the required-environment operation certificate of the store's CLOSED tickets "
            "(the op-cert merge-gate). Walks the merged log, groups by ticket, and for each "
            "in-scope closed ticket verifies that verify.require_environment (or "
            "--require-environment) "
            "produced a valid completion-verifier op-cert against its OUT-OF-BAND-PINNED key "
            "(.rebar/trusted_environments.yaml). Advisory unless a required environment is set, in "
            "which case any ENFORCED closed ticket without a valid cert fails the gate (non-zero "
            "exit). Tickets whose close commit predates --since / verify.opcert_enforce_since are "
            "grandfathered: reported but never fail the gate."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--require-environment",
        metavar="ENV_ID",
        help="environment that must sign (default: verify.require_environment)",
    )
    p.add_argument(
        "--since",
        help="grandfather boundary: only enforce tickets closed at/descending this ref "
        "(default: verify.opcert_enforce_since)",
    )
    p.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="output format (default: text). json prints only a report array to stdout",
    )
    p.add_argument("--root", help="repo root (default: cwd); resolves the ticket store")
    args = p.parse_args(argv)

    try:
        cfg = config.load_config(root=args.root)
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
    import json

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
        # Enforcement anchor = the terminal close-STATUS event's introducing commit. If that event
        # has been compacted into a SNAPSHOT the anchor is unresolvable (None). We must NOT drop the
        # ticket from scope — that would be FAIL-OPEN (a compacted closed ticket could carry no
        # op-cert yet never be enforced). Leave close_commit=None and let `_is_enforced(None, ...)`
        # FAIL CLOSED (treat as enforced): a ticket we cannot prove is grandfathered is enforced.
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
                # AUTHORITATIVE material fingerprint: recompute from the LIVE ticket state — never
                # trust the record's self-reported `material_fingerprint`. The record (and its
                # envelope) live on the auto-pushed, non-Gerrit-gated tickets branch, so an attacker
                # can craft a self-consistent record (envelope binds X, record claims X); binding
                # the RECOMPUTED value forces the cert to attest the ticket's REAL current material.
                auth_material = _authoritative_material(ticket_id, args.root)
                if auth_material is None:
                    reason = "cannot recompute authoritative material fingerprint"
                elif not isinstance(merged_commit, str) or not merged_commit:
                    reason = "malformed op-cert (missing merged_log_commit)"
                elif not _commit_in_gated_history(merged_commit, args.root):
                    # The bound merged-log commit must be a REAL commit in the gated main history
                    # (ancestor of HEAD), not an arbitrary/off-history value the attacker chose to
                    # land in a favorable key-validity era.
                    reason = "op-cert merged_log_commit is not in the gated main history"
                else:
                    verdict = trusted_env.verify_required_environment(
                        envelope,
                        ticket_id,
                        auth_material,
                        merged_commit,
                        required_env,
                        kind=KIND,
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
        print(json.dumps(report))
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
