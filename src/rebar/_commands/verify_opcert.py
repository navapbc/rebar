"""Required-environment completion-certificate merge gate (``rebar verify-opcert``).

For each closed ticket in merged history, verify its ``completion-verifier`` certificate
against the required environment and this environment's ``verify.trusted_opcert_keys`` list
(ADR 0125). The terminal close STATUS commit anchors grandfathering. Missing, unlisted or
invalid certificates fail enforced tickets, pre-boundary tickets are advisory, and an unset
required environment disables enforcement. A required environment without a trust list is a
configuration error. Exit 2 reports config or store errors, 1 an enforced failure, and 0 a
pass or advisory result. The three ``[verify]`` keys that decide the gate come from the
project config committed on the reviewed base branch, not from the working tree
(:mod:`rebar._opcert_gate_config`).
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
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        return proc.returncode == 0
    except Exception:  # noqa: BLE001 — git failure → not provably in history → reject (fail-closed)
        return False


def _completion_cert_problem(state, ticket_id, root, required_env, trusted_pairs) -> str:
    """Why the completion certificate of ``ticket_id`` fails the gate; ``""`` when it passes."""
    from rebar._opcert_verify import verify_completion_opcert
    from rebar.attest import opcert

    rec = (state.get("attestations") or {}).get(KIND)
    got = opcert.opcert_from_record(rec) if isinstance(rec, dict) else None
    if got is None:
        return "missing op-cert"
    envelope, bound = got
    merged_commit = bound.get("merged_log_commit")
    # Recompute material from the current ticket state. Do not trust the self-reported
    # record because ticket-branch data bypasses Gerrit review.
    auth_material = _authoritative_material(ticket_id, root)
    if auth_material is None:
        return "cannot recompute authoritative material fingerprint"
    if not isinstance(merged_commit, str) or not merged_commit:
        return "malformed op-cert (missing merged_log_commit)"
    if not _commit_in_gated_history(merged_commit, root):
        # The bound merged-log commit must be a REAL commit in the gated main history (ancestor
        # of HEAD), not an arbitrary/off-history value: it constrains the code-state claim.
        return "op-cert merged_log_commit is not in the gated main history"
    verdict = verify_completion_opcert(
        envelope,
        ticket_id,
        auth_material,
        merged_commit,
        kind=KIND,
        required_env=required_env,
        trusted_pairs=trusted_pairs,
    )
    return "" if verdict.verified else f"invalid op-cert ({verdict.verdict}: {verdict.reason})"


def _sets_closed(event: dict) -> bool:
    """Whether one raw event can leave the ticket ``closed``: a STATUS to closed, a CREATE
    whose genesis status is closed, or an EDIT that writes ``status: closed``."""
    data = event.get("data") or {}
    kind = event.get("event_type")
    if kind in ("STATUS", "CREATE"):
        return data.get("status") == "closed"
    if kind == "EDIT":
        return (data.get("fields") or {}).get("status") == "closed"
    return False


def _may_be_closed(ticket_dir: str) -> bool:
    """Whether the ticket's raw event files leave any chance that it is closed.

    Read straight from the ticket directory, not from the merged-log walk, because a SNAPSHOT
    that folded unsigned events contributes nothing to that walk. A SNAPSHOT (compacted
    history), an unparseable file, no event file at all, or any event that sets ``closed``
    (see :func:`_sets_closed`) all mean "may be closed". The gate does not re-derive which
    branch of a forked STATUS chain the reducer keeps: that reduce is what failed here."""
    import json

    from .verify_authorship import _active_event_files

    names = _active_event_files(ticket_dir)
    if not names:
        return True
    for name in names:
        try:
            with open(os.path.join(ticket_dir, name), encoding="utf-8") as fh:
                event = json.load(fh)
        except (OSError, ValueError):
            return True
        if not isinstance(event, dict):
            return True
        if event.get("event_type") == "SNAPSHOT" or _sets_closed(event):
            return True
    return False


def _gate_ticket_ids(events, tracker: str) -> list[str]:
    """Every ticket the gate must consider: those in the merged-log walk plus every ticket
    directory in the store, so a ticket whose compacted history yields no walked event is
    still enumerated. Gate-exempt and archived tickets stay out, as in the walk."""
    from .verify_authorship import _is_archived_ticket, _is_gate_exempt_ticket

    ids = {ev.ticket_id for ev in events if ev.ticket_id}
    try:
        names = os.listdir(tracker)
    except OSError:
        names = []
    for name in names:
        ticket_dir = os.path.join(tracker, name)
        if name.startswith(".") or name in ids or not os.path.isdir(ticket_dir):
            continue
        if not (_is_gate_exempt_ticket(ticket_dir) or _is_archived_ticket(ticket_dir)):
            ids.add(name)
    return sorted(ids)


def _scoped_state(rebar, ticket_id, root, tracker) -> tuple[dict | None, str]:
    """The closed ticket's state, or ``None`` when it is out of scope, plus why it is unreadable.

    A ticket whose state cannot be reduced is NOT dropped unless its raw events prove it was
    never closed (:func:`_may_be_closed`): otherwise it stays in scope with an ``unreadable
    ticket state`` reason, so an enforcing gate fails closed on it (bug
    scarlet-unsocial-limpkin)."""
    try:
        state = rebar.show_ticket(ticket_id, repo_root=root)
    except Exception as exc:  # noqa: BLE001 — any reduce failure; judged below, never skipped
        if not _may_be_closed(os.path.join(tracker, ticket_id)):
            return None, ""
        return {}, f"unreadable ticket state ({type(exc).__name__}: {exc})"
    if not isinstance(state, dict) or state.get("status") != "closed":
        return None, ""
    return state, ""


@guard_parse_errors
def cli(argv: list[str]) -> int:
    p = build_opcert(prog="rebar verify-opcert")
    args = p.parse_args(argv)

    # Trust and posture come from the reviewed base branch's committed project config, never
    # from the checkout under review, so a change cannot widen its own gate (golden-busied-nag).
    from rebar._engine_support.reads import local_read_context
    from rebar._opcert_gate_config import gate_config
    from rebar._operation_config import bind_operation_snapshot

    try:
        cfg, trust_source, snapshot = gate_config(args.root)
    except config.ConfigError as exc:
        print(f"verify-opcert: {exc}", file=sys.stderr)
        return 2
    print(f"verify-opcert: trust configuration from {trust_source}", file=sys.stderr)
    # Bind the same configuration for the whole run, so snapshot-aware readers (the store
    # location, `tracker.dir`, above all) use main's values: a change pointing `tracker.dir`
    # at an empty directory would otherwise leave nothing in scope. Reads stay local: the
    # gate verifies the store as mounted, so no read-time sync consults the working tree's
    # `sync.pull` (load_config does not see the snapshot).
    with bind_operation_snapshot(snapshot), local_read_context():
        return _gate(args, cfg)


def _gate(args, cfg) -> int:
    """Run the merge gate under the base-branch configuration ``cfg``."""
    required_env = args.require_environment or cfg.verify.require_environment
    since_ref = args.since if args.since is not None else cfg.verify.opcert_enforce_since
    as_json = args.format == "json"

    from rebar._opcert_verify import opcert_pairs

    trusted_pairs = opcert_pairs(cfg.verify.trusted_opcert_keys)
    if required_env and trusted_pairs is None:
        print(
            f"verify-opcert: verify.require_environment is {required_env!r} but "
            "verify.trusted_opcert_keys is empty; a required environment needs a trust list "
            "naming its keys (ADR 0125)",
            file=sys.stderr,
        )
        return 2

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
    from rebar.attest import authorship

    from .verify_authorship import _collect_all, _is_enforced, _resolve_commit

    events = _collect_all(tracker)
    commit_map = authorship.build_introducing_commit_map(repo_root=args.root)

    ticket_ids = _gate_ticket_ids(events, tracker)

    report: list[dict] = []
    problems: list[tuple[str, str, bool]] = []  # (ticket_id, reason, grandfathered)
    in_scope = 0
    satisfied = 0
    enforced_not_satisfied = 0

    for ticket_id in ticket_ids:
        # IN SCOPE for the completion-verifier lane iff the ticket is CLOSED.
        state, unreadable = _scoped_state(rebar, ticket_id, args.root, tracker)
        if state is None:
            continue
        in_scope += 1
        # Resolve the terminal close STATUS as the enforcement anchor. A compacted or missing
        # event leaves no proven grandfathering commit, so ``_is_enforced`` fails closed.
        anchor = _close_anchor_event(events, ticket_id)
        close_commit = (
            _resolve_commit(anchor, args.root, commit_map) if anchor is not None else None
        )

        reason = unreadable or (
            _completion_cert_problem(state, ticket_id, args.root, required_env, trusted_pairs)
            if required_env and trusted_pairs is not None
            else "no required environment configured"
        )
        ok = not reason
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
