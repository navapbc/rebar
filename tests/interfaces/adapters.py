"""Interface adapters: one common protocol, three implementations.

Each adapter drives the same ticket operations through a different rebar
interface — the Python library (in-process), the CLI (`python -m rebar.cli`
subprocess), and the MCP server (FastMCP tools) — and normalizes results to
plain Python values so test_parity.py can assert identical behavior without
per-interface branching.

All adapters target the repo named by the REBAR_ROOT env var (set by the
rebar_repo fixture), so the three operate on the SAME git-backed store.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any


class Adapter:
    """Common interface protocol. Methods return normalized Python values.

    transition() returns True on success, False on a rejected/failed transition
    (so parity tests assert error-presence + store-invariance uniformly).
    """

    name: str

    def create(self, ticket_type: str, title: str, **kw: Any) -> str: ...
    def show(self, tid: str) -> dict: ...
    def list(self, **filters: Any) -> list[dict]: ...
    def transition(self, tid: str, current: str, target: str) -> bool: ...
    def claim(self, tid: str, assignee: str | None = None) -> bool: ...
    def comment(self, tid: str, body: str) -> None: ...
    def tag(self, tid: str, tag: str) -> None: ...
    def link(self, a: str, b: str, relation: str) -> None: ...
    def deps(self, tid: str) -> dict: ...
    def ready(self) -> Any: ...
    def next_batch(self, epic_id: str) -> dict: ...
    def search(self, query: str, **filters: Any) -> list[dict]: ...


# ── Library ────────────────────────────────────────────────────────────────
class LibraryAdapter(Adapter):
    name = "library"

    def __init__(self) -> None:
        import rebar

        self._r = rebar

    def create(self, ticket_type: str, title: str, **kw: Any) -> str:
        return self._r.create_ticket(ticket_type, title, **kw)

    def show(self, tid: str) -> dict:
        return self._r.show_ticket(tid)

    def list(self, **filters: Any) -> list[dict]:
        return self._r.list_tickets(**filters)

    def transition(self, tid: str, current: str, target: str) -> bool:
        try:
            self._r.transition(tid, current, target)
            return True
        except self._r.RebarError:
            return False

    def claim(self, tid: str, assignee: str | None = None) -> bool:
        try:
            self._r.claim(tid, assignee=assignee)
            return True
        except self._r.RebarError:
            return False

    def comment(self, tid: str, body: str) -> None:
        self._r.comment(tid, body)

    def tag(self, tid: str, tag: str) -> None:
        self._r.tag(tid, tag)

    def link(self, a: str, b: str, relation: str) -> None:
        self._r.link(a, b, relation)

    def deps(self, tid: str) -> dict:
        return self._r.deps(tid)

    def ready(self) -> Any:
        return self._r.ready()

    def next_batch(self, epic_id: str) -> dict:
        return self._r.next_batch(epic_id)

    def search(self, query: str, **filters: Any) -> list[dict]:
        return self._r.search(query, **filters)


# ── CLI ──────────────────────────────────────────────────────────────────────
class CliAdapter(Adapter):
    name = "cli"

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "rebar.cli", *args],
            capture_output=True,
            text=True,
        )

    def _ok_json(self, *args: str) -> Any:
        cp = self._run(*args)
        assert cp.returncode == 0, f"cli {args} failed: {cp.stderr}"
        return json.loads(cp.stdout)

    def create(self, ticket_type: str, title: str, **kw: Any) -> str:
        args = ["create", ticket_type, title]
        for key, val in kw.items():
            if val is None:
                continue
            flag = "--" + key
            if key == "tags" and isinstance(val, (list, tuple)):
                val = ",".join(val)
            args += [flag, str(val)]
        cp = self._run(*args)
        assert cp.returncode == 0, f"cli create failed: {cp.stderr}"
        lines = [ln for ln in cp.stdout.splitlines() if ln.strip()]
        return lines[-1].strip()

    def show(self, tid: str) -> dict:
        return self._ok_json("show", tid)

    # Library kwarg -> CLI list flag (ticket_type maps to --type).
    _LIST_FLAGS = {
        "status": "--status",
        "ticket_type": "--type",
        "priority": "--priority",
        "parent": "--parent",
        "has_tag": "--has-tag",
        "without_tag": "--without-tag",
        "include_archived": "--include-archived",
    }

    def list(self, **filters: Any) -> list[dict]:
        args = ["list"]
        for key, val in filters.items():
            if val is None or val is False:
                continue
            flag = self._LIST_FLAGS[key]
            if val is True:
                args.append(flag)
            else:
                args.append(f"{flag}={val}")
        return self._ok_json(*args)

    def transition(self, tid: str, current: str, target: str) -> bool:
        return self._run("transition", tid, current, target).returncode == 0

    def claim(self, tid: str, assignee: str | None = None) -> bool:
        args = ["claim", tid]
        if assignee:
            args.append(f"--assignee={assignee}")
        return self._run(*args).returncode == 0

    def comment(self, tid: str, body: str) -> None:
        assert self._run("comment", tid, body).returncode == 0

    def tag(self, tid: str, tag: str) -> None:
        assert self._run("tag", tid, tag).returncode == 0

    def link(self, a: str, b: str, relation: str) -> None:
        assert self._run("link", a, b, relation).returncode == 0

    def deps(self, tid: str) -> dict:
        return self._ok_json("deps", tid)

    def ready(self) -> Any:
        return self._ok_json("ready", "--json")

    def next_batch(self, epic_id: str) -> dict:
        return self._ok_json("next-batch", epic_id, "--json")

    def search(self, query: str, **filters: Any) -> list[dict]:
        args = ["search", query]
        for k, v in filters.items():
            if v is None:
                continue
            args.append(f"--{k.replace('_', '-')}={v}")
        return self._ok_json(*args)


# ── MCP ──────────────────────────────────────────────────────────────────────
def _parse_block(block: Any) -> Any:
    text = getattr(block, "text", block)
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


def _unwrap(result: Any) -> Any:
    """Normalize a FastMCP call_tool result to a plain Python value.

    Recent FastMCP returns either a content list (for dict-returning tools) or a
    ``(content_blocks, structured)`` tuple (for list/scalar returns), where
    ``structured`` is ``{"result": <value>}``. Prefer the structured value when
    present; otherwise parse the content block(s).
    """
    if isinstance(result, tuple):
        structured = result[1] if len(result) > 1 else None
        if isinstance(structured, dict):
            if set(structured.keys()) == {"result"}:
                return structured["result"]
            return structured
        result = result[0]
    if not result:
        return None
    if len(result) == 1:
        return _parse_block(result[0])
    return [_parse_block(b) for b in result]


class McpAdapter(Adapter):
    name = "mcp"

    def __init__(self) -> None:
        import asyncio

        from rebar.mcp_server import build_server

        self._asyncio = asyncio
        self._srv = build_server()

    def _call(self, tool: str, **args: Any) -> Any:
        return _unwrap(self._asyncio.run(self._srv.call_tool(tool, args)))

    def create(self, ticket_type: str, title: str, **kw: Any) -> str:
        return self._call("create_ticket", ticket_type=ticket_type, title=title, **kw)

    def show(self, tid: str) -> dict:
        return self._call("show_ticket", ticket_id=tid)

    def list(self, **filters: Any) -> list[dict]:
        return self._call("list_tickets", **filters)

    def transition(self, tid: str, current: str, target: str) -> bool:
        try:
            self._call(
                "transition_ticket",
                ticket_id=tid,
                current_status=current,
                target_status=target,
            )
            return True
        except Exception:
            return False

    def claim(self, tid: str, assignee: str | None = None) -> bool:
        try:
            self._call("claim_ticket", ticket_id=tid, assignee=assignee)
            return True
        except Exception:
            return False

    def comment(self, tid: str, body: str) -> None:
        self._call("comment_ticket", ticket_id=tid, body=body)

    def tag(self, tid: str, tag: str) -> None:
        self._call("tag_ticket", ticket_id=tid, tag=tag)

    def link(self, a: str, b: str, relation: str) -> None:
        self._call("link_tickets", id1=a, id2=b, relation=relation)

    def deps(self, tid: str) -> dict:
        return self._call("ticket_deps", ticket_id=tid)

    def ready(self) -> Any:
        return self._call("ready_tickets")

    def next_batch(self, epic_id: str) -> dict:
        return self._call("next_batch", epic_id=epic_id)

    def search(self, query: str, **filters: Any) -> list[dict]:
        return self._call("search", query=query, **filters)
