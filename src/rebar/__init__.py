"""rebar — event-sourced ticket system with a Jira reconciler.

Three interfaces over one implementation:
  * CLI:     the ``rebar`` console script (rebar.cli)
  * Library: this package — in-process reads and writes over the git-backed store
  * MCP:     the ``rebar-mcp`` console script (rebar.mcp_server)

Ticket reads and writes run in-process against the event-sourced store (the Jira
reconciler runs as a subprocess). The reducer and graph APIs (``rebar.reducer`` /
``rebar.graph``) are re-exported for callers that want in-process bulk reads.
"""

from __future__ import annotations

import importlib.metadata
import json
import subprocess
import sys
from typing import Any

from rebar import config
from rebar._engine import engine_dir, engine_env

try:
    # Single source of truth: derive the version from the installed package
    # metadata so it can never drift from the distribution version.
    __version__ = importlib.metadata.version("nava-rebar")
except importlib.metadata.PackageNotFoundError:  # pragma: no cover - dev checkout
    # Not installed (e.g. running straight from a source tree without an editable
    # install). Fall back to a sentinel rather than crashing import.
    __version__ = "0+unknown"


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


# ── Initialization ───────────────────────────────────────────────────────────
def init_repo(*, repo_root=None) -> None:
    """Initialize the ticket system (orphan ``tickets`` branch + worktree).

    This is the explicit library init path (Tier E E4, in-process): it always
    bootstraps and never prompts. Other library calls do NOT auto-init — they
    require this to have run first (or ``rebar init`` interactively)."""
    from rebar._commands import init as _init_cmd

    rc = _init_cmd.init_core(repo_root, silent=True)
    if rc != 0:
        raise RebarError(f"rebar init failed (exit {rc})", returncode=rc)


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
    source: dict | None = None,
    return_alias: bool = False,
    repo_root=None,
):
    """Create a ticket.

    Returns the canonical 16-hex ticket id (default). With ``return_alias=True``,
    returns ``{"id": <16-hex>, "alias": <human alias>}`` so agents don't need a
    second ``show`` to learn the alias (WS5e).

    ``source`` (P1.2 import): optional provenance dict — keys ``source_id``,
    ``source_created_at``, ``source_author``, ``source_env`` are recorded on the
    CREATE event and surfaced in compiled state, so an imported ticket preserves
    where it came from while still getting a fresh local id + HLC timestamp.
    """
    # Composed in-process via the shared create_core (validation/alias/CREATE
    # event); the bash create path was retired with the Tier B cutover.
    from rebar._commands import composer
    from rebar._commands._seam import CommandError

    try:
        res = composer.create_core(
            ticket_type,
            title,
            parent=parent,
            priority=priority,
            assignee=assignee,
            description=description,
            tags=tags,
            source=source,
            repo_root=repo_root,
        )
    except CommandError as exc:
        raise RebarError(
            f"rebar create failed (exit {exc.returncode}): {exc.message}",
            returncode=exc.returncode,
            stderr=exc.message,
        ) from None
    if not return_alias:
        return res["id"]
    return {"id": res["id"], "alias": res["alias"] or ""}


def transition(ticket_id: str, current_status: str, target_status: str, *, repo_root=None) -> dict:
    """Transition a ticket's status with optimistic concurrency.

    Raises :class:`ConcurrencyError` if the ticket's actual status no longer
    matches ``current_status`` (engine exit code 10), and :class:`RebarError`
    for other failures.
    """
    # In-process (Tier E E3): resolve the id, then run the shared transition core
    # (ticket-transition.sh was retired from this path). The structured result
    # {ticket_id, from, to, newly_unblocked[]} is the single source of truth.
    from rebar._commands import transition as _transition
    from rebar._commands._seam import CommandError
    from rebar._commands.txn import ConcurrencyMismatch
    from rebar._engine_support.resolver import resolve_ticket_id

    tracker = str(config.tracker_dir(repo_root))
    resolved = resolve_ticket_id(ticket_id, tracker)
    if resolved is None:
        raise RebarError(
            f"rebar transition failed (exit 1): Error: ticket '{ticket_id}' not found",
            returncode=1,
            stderr=f"Error: ticket '{ticket_id}' not found\n",
        )
    try:
        result = _transition.transition_compute(
            resolved, current_status, target_status, repo_root=repo_root
        )
    except ConcurrencyMismatch as exc:
        raise ConcurrencyError(
            f"transition rejected: {ticket_id} is no longer '{current_status}'. {exc.message}",
            returncode=10,
            stderr=exc.message,
        ) from None
    except CommandError as exc:
        raise RebarError(
            f"rebar transition failed (exit {exc.returncode}): {exc.message}",
            returncode=exc.returncode,
            stderr=exc.message,
        ) from None
    return {
        "ticket_id": result["ticket_id"],
        "from": result["from"],
        "to": result["to"],
        "newly_unblocked": result["newly_unblocked"],
    }


def claim(ticket_id: str, *, assignee=None, repo_root=None) -> dict:
    """Atomically claim an OPEN ticket: move it to ``in_progress`` and set its
    assignee in one locked critical section.

    Raises :class:`ConcurrencyError` (engine exit code 10) if the ticket is not
    ``open`` — i.e. someone else already claimed it — and :class:`RebarError` for
    other failures. This is the optimistic-concurrency primitive parallel agents
    use to grab work without double-assignment.
    """
    # In-process (Tier E E3): resolve the id, then run the shared claim core
    # (ticket-claim.sh was retired from this path). Returns the structured result
    # {ticket_id, status, assignee}.
    from rebar._commands import transition as _transition
    from rebar._commands._seam import CommandError
    from rebar._commands.txn import ConcurrencyMismatch
    from rebar._engine_support.resolver import resolve_ticket_id

    tracker = str(config.tracker_dir(repo_root))
    resolved = resolve_ticket_id(ticket_id, tracker)
    if resolved is None:
        raise RebarError(
            f"rebar claim failed (exit 1): Error: ticket '{ticket_id}' not found",
            returncode=1,
            stderr=f"Error: ticket '{ticket_id}' not found\n",
        )
    try:
        return _transition.claim_compute(resolved, assignee=assignee or "", repo_root=repo_root)
    except ConcurrencyMismatch as exc:
        raise ConcurrencyError(
            f"claim rejected: {ticket_id} is not open (already claimed). {exc.message}",
            returncode=10,
            stderr=exc.message,
        ) from None
    except CommandError as exc:
        raise RebarError(
            f"rebar claim failed (exit {exc.returncode}): {exc.message}",
            returncode=exc.returncode,
            stderr=exc.message,
        ) from None


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
    import os as _os

    from rebar._engine_support import gates, reads
    from rebar._engine_support.reads import ReadError

    tracker = reads.tracker_dir(repo_root)
    reads.ensure_fresh(tracker)
    try:
        state = reads.show_state(ticket_id, tracker)
    except ReadError as exc:
        # Schema-conformant structured failure (threshold 0 == "not evaluated").
        return {"score": 0, "verdict": "fail", "threshold": 0, "reason": str(exc), "passed": False}
    threshold = gates._clarity_threshold(_os.path.dirname(tracker), None)
    data, code = gates.clarity_check_compute(
        (state.get("ticket_type") or "").strip(), state.get("description") or "", threshold
    )
    data["passed"] = code == 0
    return data


def check_ac(ticket_id: str, *, repo_root=None) -> dict:
    """Check a ticket has an Acceptance Criteria block.

    Returns the engine's structured gate result {verdict, criteria_count, reason}
    plus a convenience ``passed`` boolean (verdict == 'pass')."""
    from rebar._engine_support import gates, reads

    tracker = reads.tracker_dir(repo_root)
    reads.ensure_fresh(tracker)
    data, code = gates.check_ac_compute(ticket_id, tracker)
    data["passed"] = code == 0
    return data


def quality_check(ticket_id: str, *, repo_root=None) -> dict:
    """Check ticket dispatch readiness.

    Returns the engine's structured gate result {verdict, line_count,
    keyword_count, ac_items, file_impact, reason} plus a convenience ``passed``
    boolean (verdict == 'pass')."""
    from rebar._engine_support import gates, reads

    tracker = reads.tracker_dir(repo_root)
    reads.ensure_fresh(tracker)
    data, code, _warn = gates.quality_check_compute(ticket_id, tracker)
    data["passed"] = code == 0
    return data


def validate(*, repo_root=None) -> dict:
    """Repo-wide quality health check (JSON report).

    ``validate`` is repo-wide and takes no ticket id. Its exit code is
    score-encoded (exit == 5 - score), so a nonzero exit is NORMAL — not a
    failure. We use the non-raising :func:`_run` and json-parse stdout,
    returning {score, critical_issues, major_issues, minor_issues, warnings,
    suggestions}.
    """
    from rebar._engine_support import validate as _validate

    tracker = str(config.tracker_dir(repo_root))
    return _validate.validate_state(tracker)


def get_file_impact(ticket_id: str, *, repo_root=None) -> list:
    """Get the current file-impact array for a ticket ([] on a miss)."""
    from rebar._engine_support import field_reads, reads

    tracker = reads.tracker_dir(repo_root)
    reads.ensure_fresh(tracker)
    return field_reads.file_impact(ticket_id, tracker)


def set_file_impact(ticket_id: str, impact, *, repo_root=None) -> None:
    """Record file impact (list of {path, reason} dicts, or a JSON string)."""
    import json as _json

    payload = impact if isinstance(impact, str) else _json.dumps(impact)
    from rebar._commands import leaf

    _python_leaf(
        leaf.set_file_impact, ticket_id, payload, repo_root=repo_root, what="set-file-impact"
    )


def get_verify_commands(ticket_id: str, *, repo_root=None) -> list:
    """Get the current DD-level verify-commands array for a ticket.

    A missing ticket raises ``RebarError`` (the dispatcher's exit-1 contract),
    unlike :func:`get_file_impact` which returns ``[]`` on a miss.
    """
    from rebar._engine_support import field_reads, reads
    from rebar._engine_support.reads import ReadError

    tracker = reads.tracker_dir(repo_root)
    reads.ensure_fresh(tracker)
    try:
        return field_reads.verify_commands(ticket_id, tracker)
    except ReadError as exc:
        raise RebarError(
            f"get-verify-commands failed (exit 1): {exc}", returncode=1, stderr=str(exc)
        ) from None


def set_verify_commands(ticket_id: str, commands, *, repo_root=None) -> None:
    """Record DD-level verify commands (list of {dd_id, dd_text, command} dicts,
    or a JSON string)."""
    import json as _json

    payload = commands if isinstance(commands, str) else _json.dumps(commands)
    from rebar._commands import leaf

    _python_leaf(
        leaf.set_verify_commands,
        ticket_id,
        payload,
        repo_root=repo_root,
        what="set-verify-commands",
    )


def _python_leaf(fn, *args, repo_root, what: str, **kwargs) -> None:
    """Run a Tier B leaf write in-process — the sole path since the cutover.

    Tier B retired its kill-switch after the soak (docs/bash-migration.md §4); the
    library/MCP write surface now calls ``rebar._commands`` directly. A command
    failure is mapped onto RebarError so the exit-code contract is unchanged.
    Extra keyword arguments are forwarded verbatim to ``fn`` (e.g. ``source=`` for
    comment provenance).
    """
    from rebar._commands._seam import CommandError

    try:
        fn(*args, repo_root=repo_root, **kwargs)
    except CommandError as exc:
        raise RebarError(
            f"rebar {what} failed (exit {exc.returncode}): {exc.message}",
            returncode=exc.returncode,
            stderr=exc.message,
        ) from None


def comment(ticket_id: str, body: str, *, source: dict | None = None, repo_root=None) -> None:
    """Append a comment. ``source`` (P1.2 import): optional per-comment provenance
    (``source_author``/``source_created_at``) preserved on the imported comment."""
    from rebar._commands import leaf

    _python_leaf(leaf.comment, ticket_id, body, source=source, repo_root=repo_root, what="comment")


def append_session_log(
    entry: str, *, summary=None, relates_to=None, discovered_from=None, repo_root=None
) -> dict:
    """Append ``entry`` to the current session_log, creating one on first use.

    A convenience over ``create`` + ``comment``: the first call creates a
    ``session_log`` (titled ``summary`` or a default) and records it as the
    current log via a local pointer; subsequent calls append to that same log.
    Optional ``relates_to`` / ``discovered_from`` link the log to the work it
    documents (blocking links remain refused). Returns
    ``{"id", "alias", "created"}``."""
    from rebar._commands import session_log
    from rebar._commands._seam import CommandError

    try:
        return session_log.append(
            entry,
            summary=summary,
            relates_to=relates_to,
            discovered_from=discovered_from,
            repo_root=repo_root,
        )
    except CommandError as exc:
        raise RebarError(exc.message, returncode=exc.returncode, stderr=exc.message) from None


def start_session_log(
    *, summary=None, relates_to=None, discovered_from=None, repo_root=None
) -> dict:
    """Explicitly create a NEW session_log and make it the current one (rotating
    away from any prior log). Returns ``{"id", "alias"}``."""
    from rebar._commands import session_log
    from rebar._commands._seam import CommandError

    try:
        return session_log.start(
            summary=summary,
            relates_to=relates_to,
            discovered_from=discovered_from,
            repo_root=repo_root,
        )
    except CommandError as exc:
        raise RebarError(exc.message, returncode=exc.returncode, stderr=exc.message) from None


def edit_ticket(ticket_id: str, *, repo_root=None, **fields) -> None:
    """Edit ticket fields: title, priority, assignee, ticket_type, description, tags."""
    normalized = {}
    for key, value in fields.items():
        if value is None:
            continue
        if key == "tags" and isinstance(value, (list, tuple)):
            value = ",".join(value)
        normalized[key] = str(value)
    from rebar._commands import composer

    _python_leaf(composer.edit_core, ticket_id, normalized, repo_root=repo_root, what="edit")


def link(id1: str, id2: str, relation: str, *, repo_root=None) -> None:
    """Link two tickets.

    ``relation`` must be one of the six canonical relations: blocks, depends_on,
    relates_to, duplicates, supersedes, discovered_from.
    """
    from rebar._commands import composer

    def _link(i, j, rel, *, repo_root):
        composer.link_core(i, j, rel, repo_root=repo_root, quiet=True)

    _python_leaf(_link, id1, id2, relation, repo_root=repo_root, what="link")


def unlink(id1: str, id2: str, *, repo_root=None) -> None:
    from rebar._commands import unlink as _unlink_cmd

    _python_leaf(_unlink_cmd.unlink_core, id1, id2, repo_root=repo_root, what="unlink")


def tag(ticket_id: str, tag: str, *, repo_root=None) -> None:
    from rebar._commands import leaf

    _python_leaf(leaf.tag, ticket_id, tag, repo_root=repo_root, what="tag")


def untag(ticket_id: str, tag: str, *, repo_root=None) -> None:
    from rebar._commands import leaf

    _python_leaf(leaf.untag, ticket_id, tag, repo_root=repo_root, what="untag")


def archive(ticket_id: str, *, repo_root=None) -> None:
    from rebar._commands import leaf

    _python_leaf(leaf.archive, ticket_id, repo_root=repo_root, what="archive")


def compact(ticket_id: str | None = None, *, repo_root=None) -> None:
    # In-process (Tier E E3): compact-on-id via the shared compaction core
    # (ticket-compact.sh retired from this path). Output is captured (the bash
    # library wrapper captured it too); failures raise RebarError.
    import contextlib
    import io

    from rebar._commands import compact as _compact

    out, err = io.StringIO(), io.StringIO()
    argv = [ticket_id] if ticket_id else []
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = _compact.compact_cli(argv, repo_root=repo_root)
    if rc != 0:
        raise RebarError(
            f"rebar compact failed (exit {rc}): {err.getvalue().strip()}",
            returncode=rc,
            stderr=err.getvalue(),
        )


# ── Cryptographic manifest signing (environment-bound) ────────────────────────
def sign_manifest(ticket_id: str, manifest, *, repo_root=None) -> dict:
    """Sign a manifest of verified steps for a ticket with the environment key.

    ``manifest`` is a list of verified-step strings (or a JSON-array string).
    Computes an HMAC-SHA256 signature with the environment-specific signing key
    (``REBAR_SIGNING_KEY`` or the gitignored ``.signing-key``), persists it as a
    SIGNATURE event, and returns the record
    ``{ticket_id, manifest, algorithm, signature, key_id, head_sha, signed_at}``.
    """
    from rebar import signing
    from rebar.signing import SigningError

    try:
        return signing.sign_manifest(ticket_id, manifest, repo_root=repo_root)
    except SigningError as exc:
        raise RebarError(
            f"rebar sign failed (exit {exc.returncode}): {exc.message}",
            returncode=exc.returncode,
            stderr=exc.message,
        ) from None


def verify_signature(ticket_id: str, *, repo_root=None) -> dict:
    """Certify a ticket's recorded verified steps against its signature.

    Returns a verdict dict ``{ticket_id, verified, verdict, reason, manifest,
    ...}``. ``verdict`` is ``certified`` (steps match the signature under this
    environment's key), ``mismatch`` (steps altered / signature invalid),
    ``foreign_key`` (signed by a different environment), or ``unsigned``. Raises
    :class:`RebarError` only when the ticket id cannot be resolved.
    """
    from rebar import signing
    from rebar.signing import SigningError

    try:
        return signing.verify_signature(ticket_id, repo_root=repo_root)
    except SigningError as exc:
        raise RebarError(
            f"rebar verify-signature failed (exit {exc.returncode}): {exc.message}",
            returncode=exc.returncode,
            stderr=exc.message,
        ) from None


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
    exclude_deleted: bool = False,
    min_children: int | None = None,
    blocking_state: str = "",
    with_children_count: bool = False,
    sort: str | None = None,
    repo_root=None,
) -> list[dict]:
    """List tickets as a list of dicts, with optional filters.

    ``exclude_deleted`` drops tickets whose reduced status is ``deleted``. Note
    delete writes STATUS(deleted)+ARCHIVED, so the default list already hides
    tombstones via archived-exclusion; ``exclude_deleted`` only changes results
    when combined with ``include_archived=True``. ``min_children`` keeps tickets
    with ≥ N direct children and ``blocking_state`` ("unblocked"/"blocked") filters
    by readiness. ``with_children_count`` adds a ``children_count`` field (opt-in,
    so the default shape matches show/search — the single-reducer invariant).
    ``sort`` orders the result by ``priority|created|updated|id|status`` (prefix
    ``-`` for descending; unset values sort last); the default keeps store order.
    """
    from rebar import _reads

    return _reads.list_tickets(
        status=status,
        ticket_type=ticket_type,
        priority=priority,
        parent=parent,
        has_tag=has_tag,
        without_tag=without_tag,
        include_archived=include_archived,
        exclude_deleted=exclude_deleted,
        min_children=min_children,
        blocking_state=blocking_state,
        with_children_count=with_children_count,
        sort=sort,
        repo_root=repo_root,
    )


def deps(ticket_id: str, *, repo_root=None) -> dict:
    """Dependency graph for a ticket (JSON)."""
    from rebar import _reads

    return _reads.deps(ticket_id, repo_root=repo_root)


def ready(*, sort: str | None = None, repo_root=None) -> Any:
    """Tickets ready to work (all blockers closed).

    ``sort`` orders by ``priority|created|updated|id|status`` (``-`` prefix =
    descending; unset values last); the default keeps ready-order."""
    from rebar import _reads

    return _reads.ready(sort=sort, repo_root=repo_root)


def next_batch(epic_id: str, *, repo_root=None) -> dict:
    """Next parallel batch of unblocked tickets under an epic's hierarchy (JSON).

    Runs in-process via the shared read plumbing, like every other read (Tier C
    retired the bash orchestrator)."""
    from rebar import _reads

    return _reads.next_batch(epic_id, repo_root=repo_root)


def search(
    query: str,
    *,
    status: str | None = None,
    ticket_type: str | None = None,
    has_tag: str | None = None,
    include_archived: bool = False,
    sort: str | None = None,
    repo_root=None,
) -> list:
    """Full-text search over titles/descriptions/comments/tags (replay-derived).

    Returns a JSON list of matching ticket states (same element shape as
    :func:`list_tickets`). Plain whitespace-split terms match case-insensitively
    (AND). The query also accepts field predicates — ``status:``/``type:``/
    ``priority:``/``assignee:``/``tag:``/``parent:`` (comma = OR within a field,
    ``priority`` accepts ``<``/``<=``/``>``/``>=`` and ``n..m`` ranges), with
    ``-``/``not:`` negation; an unknown ``field:`` degrades to a literal
    substring. ``sort`` orders results by ``priority|created|updated|id|status``
    (``-`` prefix = descending; unset values last)."""
    from rebar import _reads

    return _reads.search(
        query,
        status=status,
        ticket_type=ticket_type,
        has_tag=has_tag,
        include_archived=include_archived,
        sort=sort,
        repo_root=repo_root,
    )


def recent_session_logs(*, limit: int = 5, repo_root=None) -> list:
    """The ``limit`` newest ``session_log`` tickets, newest first (by created_at).

    session_log tickets are hidden from :func:`list_tickets`; this is the
    type-specific read that surfaces them (same element shape as
    :func:`list_tickets`). ``limit`` defaults to 5; a non-positive ``limit``
    returns an empty list."""
    from rebar import _reads

    return _reads.recent_session_logs(limit=limit, repo_root=repo_root)


def fsck(*, recover: bool = False, report_only: bool = False, repo_root=None) -> str:
    """Run store integrity checks. ``recover=True`` runs the destructive recovery
    path. ``report_only=True`` suppresses fsck's only mutation — removing a stale
    ``.git/index.lock`` — so a read-only surface (MCP under REBAR_MCP_READONLY)
    can run plain fsck without any git-state write (the stale lock is reported,
    not removed)."""
    if recover:
        # In-process fsck-recover (Tier E E4). report_only has no effect on the
        # recover path (it has no index.lock mutation toggle); preserved for API
        # compatibility. Output captured; exit!=0 raises (prior _ok contract).
        import contextlib as _ctx
        import io as _io

        from rebar._commands import fsck_recover as _fr

        _out, _err = _io.StringIO(), _io.StringIO()
        with _ctx.redirect_stdout(_out), _ctx.redirect_stderr(_err):
            _rc = _fr.fsck_recover_cli([], repo_root=repo_root)
        if _rc != 0:
            raise RebarError(
                f"rebar fsck failed (exit {_rc}): {(_err.getvalue() or _out.getvalue()).strip()}",
                returncode=_rc,
                stderr=_err.getvalue(),
            )
        return _out.getvalue()

    # In-process fsck (Tier E E4). Output is captured; exit!=0 (issues found) raises,
    # preserving the prior _ok(_run(...)) contract.
    import contextlib
    import io

    from rebar._commands import fsck as _fsck_mod

    # Read-only surfaces (report_only, e.g. list/show) pass no_mutate=True directly,
    # so the scan never deletes the stale .git/index.lock — no os.environ round-trip.
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = _fsck_mod.fsck_cli([], repo_root=repo_root, no_mutate=report_only)
    if rc != 0:
        raise RebarError(
            f"rebar fsck failed (exit {rc}): {(err.getvalue() or out.getvalue()).strip()}",
            returncode=rc,
            stderr=err.getvalue(),
        )
    return out.getvalue()


def summary(*ticket_ids: str, repo_root=None) -> list:
    """One-line-per-ticket summary as structured JSON: a list of
    {ticket_id, status, title, blocking_summary}."""
    from rebar._engine_support import gates, reads

    tracker = reads.tracker_dir(repo_root)
    reads.ensure_fresh(tracker)
    return [gates.summary_compute(tid, tracker) for tid in ticket_ids]


def list_epics(
    *, include_blocked: bool = False, has_tag=None, min_children=None, repo_root=None
) -> dict:
    """DEPRECATED — thin wrapper over the generic ``list``. Returns
    ``{p0_bugs, epics}`` (both ``ticket_state`` arrays) by making exactly TWO
    generic calls: one for epics, one for P0 bugs. Blocking-awareness is now the
    generic ``blocking_state`` filter (``include_blocked=False`` → only unblocked
    epics). Prefer composing the primitives directly::

        rebar.list_tickets(ticket_type="epic", status="open,in_progress",
                           blocking_state="unblocked", min_children=N)
        rebar.list_tickets(ticket_type="bug", priority=0)
    """
    import warnings

    warnings.warn(
        "rebar.list_epics is deprecated; compose list_tickets(ticket_type='epic', "
        "status='open,in_progress', blocking_state='unblocked', min_children=N) and "
        "list_tickets(ticket_type='bug', priority=0).",
        DeprecationWarning,
        stacklevel=2,
    )
    epics = list_tickets(
        ticket_type="epic",
        status="open,in_progress",
        blocking_state="" if include_blocked else "unblocked",
        has_tag=has_tag,
        min_children=min_children,
        with_children_count=True,
        repo_root=repo_root,
    )
    p0_bugs = list_tickets(ticket_type="bug", priority=0, repo_root=repo_root)
    return {"p0_bugs": p0_bugs, "epics": epics}


def bridge_fsck(*, repo_root=None) -> dict:
    """Bridge-mapping audit as structured JSON: {orphaned, duplicates, stale}.
    A nonzero exit (anomalies present) is NORMAL, not an error.

    In-process (Tier E E6.5a): runs the audit via ``rebar._engine_support.
    bridge_fsck.audit_bridge_mappings`` instead of subprocessing the dispatcher.
    """
    from pathlib import Path

    from rebar._engine_support.bridge_fsck import audit_bridge_mappings

    tracker = config.tracker_dir(repo_root)
    findings = audit_bridge_mappings(Path(tracker))
    return {k: findings.get(k, []) for k in ("orphaned", "duplicates", "stale")}


# ── Reconciler (Jira sync) ────────────────────────────────────────────────────
def reconcile(mode: str = "dry-run", *, repo_root=None) -> dict:
    """Run the Jira reconciler. Defaults to a non-mutating ``dry-run``.

    Modes: reconcile-check | dry-run | bootstrap-strict | bootstrap-throttle | live.
    The Jira-mutating modes are ``bootstrap-strict``, ``bootstrap-throttle`` and
    ``live`` (each requires the ``acli`` binary + credentials); ``reconcile-check``
    and ``dry-run`` are non-mutating.
    """
    root = str(config.repo_root(repo_root))
    # Launch under THIS interpreter (sys.executable), not a bare ``python3``: Tier E
    # E5b rewired the reconciler onto in-package ``rebar.*`` imports, so it must run
    # on the rebar-capable interpreter. engine_env still puts the engine dir on
    # PYTHONPATH so the top-level ``rebar_reconciler`` package resolves.
    cmd = [
        sys.executable,
        "-m",
        "rebar_reconciler",
        "--mode",
        mode,
        "--repo-root",
        root,
    ]
    cp = subprocess.run(cmd, env=engine_env(root), text=True, capture_output=True, check=False)
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
        # No-write modes (dry-run / reconcile-check) emit the computed plan as
        # a JSON object on the FINAL stdout line; any preceding diagnostic
        # lines are informational. Fall back to parsing the last line so the
        # plan still reaches the caller (ticket yaw-plait-doe).
        lines = [ln for ln in out.splitlines() if ln.strip()]
        if lines:
            try:
                return json.loads(lines[-1])
            except json.JSONDecodeError:
                pass
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
    "append_session_log",
    "start_session_log",
    "edit_ticket",
    "link",
    "unlink",
    "tag",
    "untag",
    "archive",
    "compact",
    "fsck",
    "summary",
    "list_epics",
    "bridge_fsck",
    # quality gates + file-impact
    "clarity_check",
    "check_ac",
    "quality_check",
    "validate",
    "get_file_impact",
    "set_file_impact",
    "get_verify_commands",
    "set_verify_commands",
    # cryptographic manifest signing
    "sign_manifest",
    "verify_signature",
    # read path
    "show_ticket",
    "list_tickets",
    "deps",
    "ready",
    "next_batch",
    "search",
    "recent_session_logs",
    # reconciler
    "reconcile",
    # native re-exports
    "reduce_all_tickets",
    "reduce_ticket",
    "to_llm",
    "find_inbound_relationships",
    "apply_ticket_filters",
]
