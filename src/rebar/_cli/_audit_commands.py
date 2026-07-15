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
    """Entry point for ``rebar audit …``. Returns the process exit code."""
    if not rest or rest[0] in ("--help", "-h", "help"):
        # `rebar audit` / `rebar audit --help` → usage to stdout, exit 0.
        sys.stdout.write(_USAGE)
        return 0

    sub, args = rest[0], rest[1:]
    if sub == "serve":
        return _audit_serve(args)
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


def _audit_serve(args: list[str]) -> int:
    """``rebar audit serve`` — start the optional, disabled-by-default read-only audit web
    UI. Gated by ``[ui] enabled`` (default false) and the ``nava-rebar[ui]`` extra; binds
    loopback by default. Returns the process exit code."""
    host = "127.0.0.1"
    port = 8765
    i = 0
    while i < len(args):
        tok = args[i]
        if tok in ("--help", "-h"):
            sys.stdout.write(_SERVE_USAGE)
            return 0
        if tok == "--host":
            if i + 1 >= len(args):
                sys.stderr.write("Error: --host requires a value\n")
                return 2
            host = args[i + 1]
            i += 2
            continue
        if tok.startswith("--host="):
            host = tok[len("--host=") :]
            i += 1
            continue
        if tok == "--port":
            if i + 1 >= len(args):
                sys.stderr.write("Error: --port requires a value\n")
                return 2
            port_raw = args[i + 1]
            i += 2
            parsed = _parse_port(port_raw)
            if parsed is None:
                return 2
            port = parsed
            continue
        if tok.startswith("--port="):
            parsed = _parse_port(tok[len("--port=") :])
            if parsed is None:
                return 2
            port = parsed
            i += 1
            continue
        sys.stderr.write(f"Error: unknown option '{tok}'\n")
        sys.stderr.write(_SERVE_USAGE)
        return 2

    # Resolve the gate flag from config (honors REBAR_ROOT / REBAR_UI_ENABLED / -c).
    from rebar import config

    if not config.load_config().ui.enabled:
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


def _parse_port(raw: str) -> int | None:
    """Parse a ``--port`` value; write an error to stderr and return ``None`` if invalid."""
    try:
        return int(raw)
    except ValueError:
        sys.stderr.write(f"Error: --port must be an integer (got '{raw}')\n")
        return None


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
