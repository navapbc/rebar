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

__version__ = "0.3.0"


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
    return_alias: bool = False,
    repo_root=None,
):
    """Create a ticket.

    Returns the canonical 16-hex ticket id (default). With ``return_alias=True``,
    returns ``{"id": <16-hex>, "alias": <human alias>}`` so agents don't need a
    second ``show`` to learn the alias (WS5e).
    """
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
    ticket_id = lines[-1].strip() if lines else ""
    if not return_alias:
        return ticket_id
    alias = ""
    try:
        alias = (show_ticket(ticket_id, repo_root=repo_root) or {}).get("alias") or ""
    except RebarError:
        alias = ""
    return {"id": ticket_id, "alias": alias}


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


def claim(ticket_id: str, *, assignee=None, repo_root=None) -> dict:
    """Atomically claim an OPEN ticket: move it to ``in_progress`` and set its
    assignee in one locked critical section.

    Raises :class:`ConcurrencyError` (engine exit code 10) if the ticket is not
    ``open`` — i.e. someone else already claimed it — and :class:`RebarError` for
    other failures. This is the optimistic-concurrency primitive parallel agents
    use to grab work without double-assignment.
    """
    args = ["claim", ticket_id]
    if assignee:
        args += [f"--assignee={assignee}"]
    cp = _run(args, repo_root=repo_root)
    if cp.returncode == 10:
        raise ConcurrencyError(
            f"claim rejected: {ticket_id} is not open (already claimed). "
            f"{cp.stderr.strip()}",
            returncode=10,
            stderr=cp.stderr,
        )
    _ok(cp, what="claim")
    return {"id": ticket_id, "status": "in_progress", "assignee": assignee}


def reopen(ticket_id: str, *, repo_root=None) -> dict:
    """Reopen a closed ticket (closed -> open) — a thin convenience over
    :func:`transition`, still optimistic-concurrency (raises ConcurrencyError if
    the ticket is not currently ``closed``)."""
    return transition(ticket_id, "closed", "open", repo_root=repo_root)


# ── Quality gates + file-impact (WS5d; CLI-parity + MCP surface) ──────────────
# Quality checks exit 0=pass / 1=fail (not an error), so they use the
# non-raising _run and report a `passed` boolean rather than raising.
def _json_or(out: str, default):
    import json as _json
    try:
        return _json.loads(out)
    except Exception:
        return default


def clarity_check(ticket_id: str, *, repo_root=None) -> dict:
    """Score ticket clarity → {score, verdict, threshold, passed}."""
    cp = _run(["clarity-check", ticket_id], repo_root=repo_root)
    data = _json_or(cp.stdout, {"output": (cp.stdout or cp.stderr).strip()})
    data["passed"] = cp.returncode == 0
    return data


def check_ac(ticket_id: str, *, repo_root=None) -> dict:
    """Check a ticket has an Acceptance Criteria block → {passed, output}."""
    cp = _run(["check-ac", ticket_id], repo_root=repo_root)
    return {"passed": cp.returncode == 0, "output": (cp.stdout + cp.stderr).strip()}


def quality_check(ticket_id: str, *, repo_root=None) -> dict:
    """Check ticket dispatch readiness → {passed, output}."""
    cp = _run(["quality-check", ticket_id], repo_root=repo_root)
    return {"passed": cp.returncode == 0, "output": (cp.stdout + cp.stderr).strip()}


def validate(*, repo_root=None) -> dict:
    """Repo-wide quality health check (JSON report).

    ``validate`` is repo-wide and takes no ticket id. Its exit code is
    score-encoded (exit == 5 - score), so a nonzero exit is NORMAL — not a
    failure. We use the non-raising :func:`_run` and json-parse stdout,
    returning {score, critical_issues, major_issues, minor_issues, warnings,
    suggestions}.
    """
    cp = _run(["validate", "--output", "json"], repo_root=repo_root)
    return _json_or(cp.stdout, {"output": (cp.stdout or cp.stderr).strip()})


def get_file_impact(ticket_id: str, *, repo_root=None) -> list:
    """Get the current file-impact array for a ticket."""
    out = _ok(_run(["get-file-impact", ticket_id], repo_root=repo_root), what="get-file-impact")
    return _json_or(out, [])


def set_file_impact(ticket_id: str, impact, *, repo_root=None) -> None:
    """Record file impact (list of {path, reason} dicts, or a JSON string)."""
    import json as _json
    payload = impact if isinstance(impact, str) else _json.dumps(impact)
    _ok(_run(["set-file-impact", ticket_id, payload], repo_root=repo_root), what="set-file-impact")


def get_verify_commands(ticket_id: str, *, repo_root=None) -> list:
    """Get the current DD-level verify-commands array for a ticket."""
    out = _ok(_run(["get-verify-commands", ticket_id], repo_root=repo_root), what="get-verify-commands")
    return _json_or(out, [])


def set_verify_commands(ticket_id: str, commands, *, repo_root=None) -> None:
    """Record DD-level verify commands (list of {dd_id, dd_text, command} dicts,
    or a JSON string)."""
    import json as _json
    payload = commands if isinstance(commands, str) else _json.dumps(commands)
    _ok(_run(["set-verify-commands", ticket_id, payload], repo_root=repo_root), what="set-verify-commands")


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


# ── Read path (in-process via rebar._reads; alias-aware, returns parsed JSON) ──
# Reads compute from the native ticket_reducer/ticket_graph packages in-process —
# no subprocess. (next_batch is the lone exception: still the bash orchestrator.)
def show_ticket(ticket_id: str, *, repo_root=None) -> dict:
    """Compiled ticket state as a dict (alias/short-id aware)."""
    from rebar import _reads
    return _reads.show_ticket(ticket_id, repo_root=repo_root)


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
    from rebar import _reads
    return _reads.list_tickets(
        status=status,
        ticket_type=ticket_type,
        priority=priority,
        parent=parent,
        has_tag=has_tag,
        without_tag=without_tag,
        include_archived=include_archived,
        repo_root=repo_root,
    )


def deps(ticket_id: str, *, repo_root=None) -> dict:
    """Dependency graph for a ticket (JSON)."""
    from rebar import _reads
    return _reads.deps(ticket_id, repo_root=repo_root)


def ready(*, repo_root=None) -> Any:
    """Tickets ready to work (all blockers closed)."""
    from rebar import _reads
    return _reads.ready(repo_root=repo_root)


def next_batch(epic_id: str, *, repo_root=None) -> dict:
    """Next parallel batch of unblocked tickets under an epic's hierarchy (JSON).

    Still routed through the bash engine (the only read not yet in-process)."""
    return _json(_run(["next-batch", epic_id, "--output", "json"], repo_root=repo_root), what="next-batch")


def search(
    query: str,
    *,
    status: str | None = None,
    ticket_type: str | None = None,
    has_tag: str | None = None,
    include_archived: bool = False,
    repo_root=None,
) -> list:
    """Full-text search over titles/descriptions/comments/tags (replay-derived).

    Returns a JSON list of matching ticket states (same element shape as
    :func:`list_tickets`). Query terms are whitespace-split and matched
    case-insensitively (AND)."""
    from rebar import _reads
    return _reads.search(
        query,
        status=status,
        ticket_type=ticket_type,
        has_tag=has_tag,
        include_archived=include_archived,
        repo_root=repo_root,
    )


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
        "python3", "-m", "rebar_reconciler",
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
    "claim",
    "reopen",
    "comment",
    "edit_ticket",
    "link",
    "unlink",
    "tag",
    "untag",
    "archive",
    "compact",
    "fsck",
    # quality gates + file-impact
    "clarity_check",
    "check_ac",
    "quality_check",
    "validate",
    "get_file_impact",
    "set_file_impact",
    "get_verify_commands",
    "set_verify_commands",
    # read path
    "show_ticket",
    "list_tickets",
    "deps",
    "ready",
    "next_batch",
    "search",
    # reconciler
    "reconcile",
    # native re-exports
    "reduce_all_tickets",
    "reduce_ticket",
    "to_llm",
    "find_inbound_relationships",
    "apply_ticket_filters",
]
