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
        anchor = _close_anchor_event(events, ticket_id)
        if anchor is None:
            # Closed with no close-STATUS event in scope (e.g. compacted away) — cannot anchor
            # enforcement; skip rather than guess a commit.
            continue
        in_scope += 1
        close_commit = _resolve_commit(anchor, args.root, commit_map)

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
                material = bound.get("material_fingerprint")
                merged_commit = bound.get("merged_log_commit")
                if not isinstance(material, str) or not isinstance(merged_commit, str):
                    # A record missing its bound subject fields cannot verify — malformed cert.
                    reason = "malformed op-cert (missing bound subject fields)"
                else:
                    verdict = trusted_env.verify_required_environment(
                        envelope,
                        ticket_id,
                        material,
                        merged_commit,
                        required_env,
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
