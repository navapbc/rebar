"""The audit web UI's read-only FastAPI server (story a3d7).

A tiny, disabled-by-default local surface: an INDEX page listing the tickets that carry any
audit data (a plan-review, a completion verdict, or a code review), each linking to a
per-ticket page ``/ticket/<id>`` (that route is a SIBLING story — the index only needs to
LINK there). The server binds loopback by default and never mutates the store.

IMPORTABILITY CONTRACT. ``fastapi`` (in :func:`create_app`) and ``uvicorn`` (in
:func:`serve`) are imported lazily, inside the functions that need them — so even
``import rebar.audit.server`` stays free of the web stack, and building/running the app is
the only thing that needs the ``nava-rebar[ui]`` extra. Nothing in core imports this module
either (``rebar.audit.__init__`` does NOT import it, and the ``rebar audit serve`` CLI arm
imports it lazily), so ``import rebar`` stays dependency-free. When the extra is absent the
CLI arm turns the resulting ``ModuleNotFoundError`` (raised when it calls ``serve`` /
``create_app``) into an actionable message naming ``nava-rebar[ui]`` rather than a traceback.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING, Any

import rebar
from rebar.audit.read import audit_trail

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastapi import FastAPI


def _has_audit_data(trail: dict[str, Any]) -> bool:
    """True when a ticket's audit trail carries ANY evidence: a retained plan-review, a
    non-``None`` completion record, or a code review. Mirrors the ``audit_trail`` shape."""
    if trail.get("plan_reviews"):
        return True
    if trail.get("completion") is not None:
        return True
    if trail.get("code_reviews"):
        return True
    return False


def _audited_tickets(repo_root: str | None = None) -> list[dict[str, Any]]:
    """Enumerate the tickets that have audit data, newest-first as ``list_tickets`` returns
    them. Each entry is ``{"id": str, "title": str}``. Best-effort: ``audit_trail`` never
    raises, so a single ticket's read failure degrades to "no audit data" (excluded)."""
    out: list[dict[str, Any]] = []
    for ticket in rebar.list_tickets(repo_root=repo_root):
        raw_id = ticket.get("ticket_id") or ticket.get("id")
        if not raw_id:
            continue
        tid = str(raw_id)
        trail = audit_trail(tid, repo_root=repo_root)
        if _has_audit_data(trail):
            out.append({"id": tid, "title": ticket.get("title") or ""})
    return out


def _render_index(tickets: list[dict[str, Any]]) -> str:
    """Render the minimal, read-only index HTML: one linked row per audited ticket."""
    rows: list[str] = []
    for t in tickets:
        tid = html.escape(str(t["id"]))
        title = html.escape(str(t.get("title") or ""))
        rows.append(f'<li><a href="/ticket/{tid}">{tid}</a> {title}</li>')
    body = "\n".join(rows) if rows else "<li>(no tickets with audit data)</li>"
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        "<title>rebar audit</title></head><body>"
        "<h1>rebar audit</h1>"
        "<p>Read-only. Tickets with audit data:</p>"
        f"<ul>\n{body}\n</ul>"
        "</body></html>"
    )


def create_app(repo_root: str | None = None) -> FastAPI:
    """Build the read-only audit FastAPI app. ``GET /`` renders the index of tickets that
    have audit data, each linking to ``/ticket/<id>``."""
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse

    app = FastAPI(title="rebar audit", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:  # pragma: no cover - exercised via TestClient in tests
        return _render_index(_audited_tickets(repo_root=repo_root))

    return app


def serve(*, host: str = "127.0.0.1", port: int = 8765, repo_root: str | None = None) -> None:
    """Run the read-only audit server (uvicorn) over :func:`create_app`. Blocks until the
    server is stopped. Keyword-only so callers/tests can pass ``host``/``port`` by name."""
    import uvicorn

    uvicorn.run(create_app(repo_root), host=host, port=port)
