"""``rebar audit`` — the audit read-layer CLI (story 46f0).

A thin front-end over :func:`rebar.audit.read.audit_trail`. It owns its own ``--help`` (like
the ``reconcile`` / ``review-plan`` intercepts) and exposes two subcommands:

    rebar audit show <ticket> [--output json|text]
    rebar audit serve [--host 127.0.0.1] [--port 8765]

``show``'s ``--output json`` (the default) prints the full ``AuditTrail`` dict as JSON to
stdout; ``text`` prints a short human-readable summary. ``serve`` starts the optional,
disabled-by-default read-only audit web UI (gated by ``[ui] enabled``; needs the
``nava-rebar[ui]`` extra). An unknown/missing subcommand prints usage to stderr and returns a
nonzero exit.
"""

from __future__ import annotations

import json
import sys

_USAGE = (
    "Usage: rebar audit show <ticket> [--output json|text]\n"
    "       rebar audit serve [--host 127.0.0.1] [--port 8765]\n"
)
_SERVE_USAGE = "Usage: rebar audit serve [--host 127.0.0.1] [--port 8765]\n"

# Loopback host spellings that do NOT warrant a non-loopback exposure warning.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def audit_cli(rest: list[str]) -> int:
    """Entry point for ``rebar audit …``. Returns the process exit code.

    The accepted-argv grammar (``show <ticket> [--output json|text]`` /
    ``serve [--host …] [--port …]``) is owned by the shared parser factory
    :func:`rebar._cli._parsers.advanced.audit.build`; this handler keeps only the
    dispatcher-style ``--help`` behavior (the factory's built-in ``--help`` would
    print a different block) and turns the factory's :class:`ParseError` into the
    historical usage-to-stderr / exit-2 contract.
    """
    if not rest or rest[0] in ("--help", "-h", "help"):
        # `rebar audit` / `rebar audit --help` → usage to stdout, exit 0.
        sys.stdout.write(_USAGE)
        return 0

    sub = rest[0]
    # Preserve the pre-factory per-subcommand `--help` (usage to stdout, exit 0).
    if sub in ("show", "serve") and ("--help" in rest[1:] or "-h" in rest[1:]):
        sys.stdout.write(_SERVE_USAGE if sub == "serve" else _USAGE)
        return 0

    from rebar._cli._parser import ParseError, render_parse_error
    from rebar._cli._parsers.advanced.audit import build

    try:
        ns = build(prog="rebar audit").parse_args(rest)
    except ParseError as exc:
        return render_parse_error(exc)

    if ns.subcommand == "serve":
        return _audit_serve(ns.host, ns.port)
    return _audit_show(ns.ticket, ns.output)


def _audit_show(ticket: str, output: str) -> int:
    """``rebar audit show`` — print a ticket's audit trail to stdout (JSON default)."""
    from rebar.audit.read import audit_trail

    trail = audit_trail(ticket)
    if output == "json":
        sys.stdout.write(json.dumps(trail, indent=2, default=str, ensure_ascii=False) + "\n")
        return 0
    _render_text(trail)
    return 0


def _audit_serve(host: str, port: int) -> int:
    """``rebar audit serve`` — start the optional, disabled-by-default read-only audit web
    UI. Gated by ``[ui] enabled`` (default false) and the ``nava-rebar[ui]`` extra; binds
    loopback by default. ``host``/``port`` come already parsed from the factory. Returns
    the process exit code."""
    # Resolve the gate flag from config (honors REBAR_ROOT / REBAR_UI_ENABLED / -c).
    from rebar import config

    if not config.compose_config().ui.enabled:
        sys.stderr.write(
            "Error: the audit web UI is disabled. Set `[ui] enabled = true` (config key "
            "`ui.enabled`) to enable `rebar audit serve`.\n"
        )
        return 2

    if host not in _LOOPBACK_HOSTS:
        sys.stderr.write(
            f"Warning: binding to non-loopback host '{host}' exposes the read-only audit "
            "UI beyond this machine.\n"
        )

    # Guard the WHOLE start-server operation: the web stack is imported lazily both at
    # `rebar.audit.server` module load (fastapi/jinja2) and inside `serve()` (uvicorn), so
    # an absent `[ui]` extra can surface at either point — catch a missing web dependency
    # anywhere on this path and turn it into the actionable install message (never a
    # traceback), while re-raising any unrelated ModuleNotFoundError.
    try:
        from rebar.audit import server

        server.serve(host=host, port=port)
    except ModuleNotFoundError as exc:
        top = (exc.name or "").split(".")[0]
        if top and top not in {"fastapi", "uvicorn", "jinja2", "starlette"}:
            raise  # a genuinely unrelated missing module — surface it, don't mask
        sys.stderr.write(
            "Error: the audit web UI requires optional dependencies. Install them with "
            "`pip install 'nava-rebar[ui]'`.\n"
        )
        return 1
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

    if trail.get("plan_review_health"):
        health = trail["plan_review_health"]
    else:
        from rebar.audit.read import unavailable_plan_review_health

        health = unavailable_plan_review_health()
    if health.get("available") is False:
        sys.stdout.write(f"  plan_review_health: unavailable ({health.get('reason')})\n")
    else:
        enforcement = health.get("enforcement_status")
        if enforcement not in ("enabled", "disabled"):
            enforcement = "enabled" if health.get("enforced") else "disabled"
        posture = (
            "advisory; enforcement disabled"
            if enforcement == "disabled" and health.get("advisory") is True
            else "enforcement disabled"
            if enforcement == "disabled"
            else "enforced"
        )
        pin_status = health.get("pin_status", "unavailable")
        related_material_status = health.get("related_material_status")
        if related_material_status == "no-related-material" or (
            related_material_status is None
            and pin_status == "current"
            and not health.get("targets")
        ):
            pin_status = "current (no related material)"
        sys.stdout.write(f"  plan_review_health: {pin_status} ({posture})\n")
        phase_line = (
            "    phase: "
            f"{health.get('signed_phase')} -> {health.get('required_phase')} "
            f"({health.get('phase_status')})"
        )
        floor = health.get("effective_execution_floor")
        if floor is not None:
            phase_line += f", floor={float(floor):.2f}"
        sys.stdout.write(phase_line + "\n")
        for target in health.get("targets") or []:
            target_line = "    {} {} {}\n".format(
                target.get("canonical_id"), target.get("role"), target.get("pin_status")
            )
            sys.stdout.write(target_line)

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
