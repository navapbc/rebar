"""``rebar audit`` — the audit read-layer CLI (story 46f0).

A thin front-end over :func:`rebar.audit.read.audit_trail`. It owns its own ``--help`` (like
the ``reconcile`` / ``review-plan`` intercepts) and exposes one subcommand:

    rebar audit show <ticket> [--output json|text]

``--output json`` (the default) prints the full ``AuditTrail`` dict as JSON to stdout; ``text``
prints a short human-readable summary. An unknown/missing subcommand prints usage to stderr and
returns a nonzero exit.
"""

from __future__ import annotations

import json
import sys

_USAGE = "Usage: rebar audit show <ticket> [--output json|text]\n"


def audit_cli(rest: list[str]) -> int:
    """Entry point for ``rebar audit …``. Returns the process exit code."""
    if not rest or rest[0] in ("--help", "-h", "help"):
        # `rebar audit` / `rebar audit --help` → usage to stdout, exit 0.
        sys.stdout.write(_USAGE)
        return 0

    sub, args = rest[0], rest[1:]
    if sub != "show":
        sys.stderr.write(f"Error: unknown audit subcommand '{sub}'\n")
        sys.stderr.write(_USAGE)
        return 2

    ticket: str | None = None
    output = "json"
    i = 0
    while i < len(args):
        tok = args[i]
        if tok in ("--help", "-h"):
            sys.stdout.write(_USAGE)
            return 0
        if tok == "--output":
            if i + 1 >= len(args):
                sys.stderr.write("Error: --output requires a value (json|text)\n")
                return 2
            output = args[i + 1]
            i += 2
            continue
        if tok.startswith("--output="):
            output = tok[len("--output=") :]
            i += 1
            continue
        if tok.startswith("-"):
            sys.stderr.write(f"Error: unknown option '{tok}'\n")
            sys.stderr.write(_USAGE)
            return 2
        if ticket is None:
            ticket = tok
        else:
            sys.stderr.write(f"Error: unexpected argument '{tok}'\n")
            sys.stderr.write(_USAGE)
            return 2
        i += 1

    if ticket is None:
        sys.stderr.write("Error: 'audit show' requires a <ticket> argument\n")
        sys.stderr.write(_USAGE)
        return 2
    if output not in ("json", "text"):
        sys.stderr.write(f"Error: --output must be json|text (got '{output}')\n")
        return 2

    from rebar.audit.read import audit_trail

    trail = audit_trail(ticket)

    if output == "json":
        sys.stdout.write(json.dumps(trail, indent=2, default=str, ensure_ascii=False) + "\n")
        return 0

    _render_text(trail)
    return 0


def _render_text(trail: dict) -> None:
    """A compact, readable summary of an ``AuditTrail`` to stdout."""
    ticket = trail.get("ticket") or {}
    tid = ticket.get("ticket_id") or ticket.get("id") or "?"
    title = ticket.get("title") or ""
    sys.stdout.write(f"audit: {tid} {title}\n")

    plan = trail.get("plan_reviews") or []
    sys.stdout.write(f"  plan_reviews: {len(plan)} (newest-first)\n")
    for pr in plan:
        sys.stdout.write(
            f"    - verdict={pr.get('verdict')} material={pr.get('material_fingerprint')}\n"
        )

    comp = trail.get("completion")
    if comp is None:
        sys.stdout.write("  completion: (none)\n")
    else:
        att = "yes" if comp.get("attestation") else "no"
        side = comp.get("sidecar") or {}
        sys.stdout.write(
            f"  completion: attestation={att} sidecar_verdict={side.get('verdict') or '-'}\n"
        )

    crs = trail.get("code_reviews") or []
    sys.stdout.write(f"  code_reviews: {len(crs)}\n")
    for cr in crs:
        sys.stdout.write(
            f"    - {cr.get('ticket_id')}: {len(cr.get('sidecars') or [])} sidecar(s)\n"
        )
