"""rebar — event-sourced ticket system with a Jira reconciler.

Three interfaces over one engine:
  * CLI:     the ``rebar`` console script (rebar.cli)
  * Library: this package (write-path subprocess wrappers + native reads)
  * MCP:     the ``rebar-mcp`` console script (rebar.mcp_server)

The write path wraps the bundled bash dispatcher; reads return parsed JSON.
The stdlib-only native read API (ticket_reducer / ticket_graph) is re-exported
for callers that want in-process bulk reads without subprocess overhead.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

from rebar import config
from rebar._engine import dispatcher, engine_dir, engine_env, run

__version__ = "0.1.0"


# ── Exceptions ───────────────────────────────────────────────────────────────
class RebarError(RuntimeError):
    """A rebar engine command failed."""

    def __init__(self, message: str, *, returncode: int = 1, stderr: str = ""):
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


class ConcurrencyError(RebarError):
    """Optimistic-concurrency rejection (the ticket changed since it was read).

    Raised by :func:`transition` when the engine reports exit code 10.
    """


# ── Internals ────────────────────────────────────────────────────────────────
def _run(args, *, repo_root=None, check=True, input=None):
    return run(args, repo_root=repo_root, input=input, check=False, capture=True)


def _ok(cp: subprocess.CompletedProcess, *, what: str) -> str:
    if cp.returncode != 0:
        raise RebarError(
            f"rebar {what} failed (exit {cp.returncode}): {cp.stderr.strip()}",
            returncode=cp.returncode,
            stderr=cp.stderr,
        )
    return cp.stdout


def _json(cp: subprocess.CompletedProcess, *, what: str) -> Any:
    out = _ok(cp, what=what)
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise RebarError(f"rebar {what}: could not parse JSON output: {exc}") from exc


# ── Initialization ───────────────────────────────────────────────────────────
def init_repo(*, repo_root=None) -> None:
    """Initialize the ticket system (orphan ``tickets`` branch + worktree)."""
    _ok(_run(["init"], repo_root=repo_root), what="init")


# ── Write path (subprocess → dispatcher) ─────────────────────────────────────
def create_ticket(
    ticket_type: str,
    title: str,
    *,
    parent: str | None = None,
    priority: int | None = None,
    assignee: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
    repo_root=None,
) -> str:
    """Create a ticket; returns the canonical 16-hex ticket id."""
    args = ["create", ticket_type, title]
    if parent:
        args += ["--parent", parent]
    if priority is not None:
        args += ["--priority", str(priority)]
    if assignee:
        args += ["--assignee", assignee]
    if description is not None:
        args += ["--description", description]
    if tags:
        args += ["--tags", ",".join(tags)]
    out = _ok(_run(args, repo_root=repo_root), what="create")
    lines = [ln for ln in out.splitlines() if ln.strip()]
    return lines[-1].strip() if lines else ""


def transition(
    ticket_id: str, current_status: str, target_status: str, *, repo_root=None
) -> dict:
    """Transition a ticket's status with optimistic concurrency.

    Raises :class:`ConcurrencyError` if the ticket's actual status no longer
    matches ``current_status`` (engine exit code 10), and :class:`RebarError`
    for other failures.
    """
    cp = _run(["transition", ticket_id, current_status, target_status], repo_root=repo_root)
    if cp.returncode == 10:
        raise ConcurrencyError(
            f"transition rejected: {ticket_id} is no longer '{current_status}'. "
            f"{cp.stderr.strip()}",
            returncode=10,
            stderr=cp.stderr,
        )
    _ok(cp, what="transition")
    return {"id": ticket_id, "status": target_status}


def comment(ticket_id: str, body: str, *, repo_root=None) -> None:
    _ok(_run(["comment", ticket_id, body], repo_root=repo_root), what="comment")


def edit_ticket(ticket_id: str, *, repo_root=None, **fields) -> None:
    """Edit ticket fields: title, priority, assignee, ticket_type, description, tags."""
    args = ["edit", ticket_id]
    for key, value in fields.items():
        if value is None:
            continue
        if key == "tags" and isinstance(value, (list, tuple)):
            value = ",".join(value)
        args += [f"--{key}", str(value)]
    _ok(_run(args, repo_root=repo_root), what="edit")


def link(id1: str, id2: str, relation: str, *, repo_root=None) -> None:
    """Link two tickets (relation: blocks | depends_on | relates_to)."""
    _ok(_run(["link", id1, id2, relation], repo_root=repo_root), what="link")


def unlink(id1: str, id2: str, *, repo_root=None) -> None:
    _ok(_run(["unlink", id1, id2], repo_root=repo_root), what="unlink")


def tag(ticket_id: str, tag: str, *, repo_root=None) -> None:
    _ok(_run(["tag", ticket_id, tag], repo_root=repo_root), what="tag")


def untag(ticket_id: str, tag: str, *, repo_root=None) -> None:
    _ok(_run(["untag", ticket_id, tag], repo_root=repo_root), what="untag")


def archive(ticket_id: str, *, repo_root=None) -> None:
    _ok(_run(["archive", ticket_id], repo_root=repo_root), what="archive")


def compact(ticket_id: str | None = None, *, repo_root=None) -> None:
    args = ["compact"] + ([ticket_id] if ticket_id else [])
    _ok(_run(args, repo_root=repo_root), what="compact")


# ── Read path (subprocess → dispatcher; alias-aware, returns parsed JSON) ─────
def show_ticket(ticket_id: str, *, repo_root=None) -> dict:
    """Compiled ticket state as a dict (alias/short-id aware)."""
    return _json(_run(["show", ticket_id], repo_root=repo_root), what="show")


def list_tickets(
    *,
    status: str | None = None,
    ticket_type: str | None = None,
    priority: int | str | None = None,
    parent: str | None = None,
    has_tag: str | None = None,
    without_tag: str | None = None,
    include_archived: bool = False,
    repo_root=None,
) -> list[dict]:
    """List tickets as a list of dicts, with optional filters."""
    args = ["list"]
    if status:
        args.append(f"--status={status}")
    if ticket_type:
        args.append(f"--type={ticket_type}")
    if priority is not None:
        args.append(f"--priority={priority}")
    if parent:
        args.append(f"--parent={parent}")
    if has_tag:
        args.append(f"--has-tag={has_tag}")
    if without_tag:
        args.append(f"--without-tag={without_tag}")
    if include_archived:
        args.append("--include-archived")
    return _json(_run(args, repo_root=repo_root), what="list")


def deps(ticket_id: str, *, repo_root=None) -> dict:
    """Dependency graph for a ticket (JSON)."""
    return _json(_run(["deps", ticket_id], repo_root=repo_root), what="deps")


def ready(*, repo_root=None) -> Any:
    """Tickets ready to work (all blockers closed)."""
    return _json(_run(["ready", "--json"], repo_root=repo_root), what="ready")


def next_batch(epic_id: str, *, repo_root=None) -> dict:
    """Next parallel batch of unblocked tickets under an epic's hierarchy (JSON)."""
    return _json(_run(["next-batch", epic_id, "--json"], repo_root=repo_root), what="next-batch")


def fsck(*, recover: bool = False, repo_root=None) -> str:
    args = ["fsck-recover"] if recover else ["fsck"]
    return _ok(_run(args, repo_root=repo_root), what="fsck")


# ── Reconciler (Jira sync) ────────────────────────────────────────────────────
def reconcile(mode: str = "dry-run", *, repo_root=None) -> dict:
    """Run the Jira reconciler. Defaults to a non-mutating ``dry-run``.

    Modes: reconcile-check | dry-run | bootstrap-strict | bootstrap-throttle | live.
    ``live`` mutates Jira and requires the ``acli`` binary + credentials.
    """
    root = str(config.repo_root(repo_root))
    cmd = [
        "python3", "-m", "dso_reconciler",
        "--mode", mode, "--repo-root", root,
    ]
    cp = subprocess.run(
        cmd, env=engine_env(root), text=True, capture_output=True, check=False
    )
    if cp.returncode not in (0, 75):  # 75 == EXIT_RESCHEDULE
        raise RebarError(
            f"reconcile ({mode}) failed (exit {cp.returncode}): {cp.stderr.strip()}",
            returncode=cp.returncode,
            stderr=cp.stderr,
        )
    out = cp.stdout.strip()
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"mode": mode, "returncode": cp.returncode, "output": out, "stderr": cp.stderr}


# ── Native read re-exports (in-process, no subprocess) ────────────────────────
from rebar._native import (  # noqa: E402
    apply_ticket_filters,
    find_inbound_relationships,
    reduce_all_tickets,
    reduce_ticket,
    to_llm,
)

__all__ = [
    "__version__",
    "engine_dir",
    "dispatcher",
    "config",
    # exceptions
    "RebarError",
    "ConcurrencyError",
    # write path
    "init_repo",
    "create_ticket",
    "transition",
    "comment",
    "edit_ticket",
    "link",
    "unlink",
    "tag",
    "untag",
    "archive",
    "compact",
    "fsck",
    # read path
    "show_ticket",
    "list_tickets",
    "deps",
    "ready",
    "next_batch",
    # reconciler
    "reconcile",
    # native re-exports
    "reduce_all_tickets",
    "reduce_ticket",
    "to_llm",
    "find_inbound_relationships",
    "apply_ticket_filters",
]
