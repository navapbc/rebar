"""Shared CLI, library, and MCP implementations for show/list/ready/search/deps.

CLI dispatches through ``reads_cli.main``. Library and MCP callers use the
``*_state`` helpers. Keeping both paths here prevents read-contract drift.

Every interface performs the same best-effort, at-most-once-per-minute fetch and
reconverge through ``rebar._store.sync``. ``REBAR_SYNC_PULL=off`` and the CLI
``--no-pull`` flag disable it.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, cast

from rebar._engine_support.resolver import resolve_ticket_id
from rebar._engine_support.ticket_query import TicketQuery
from rebar._errors import TrackerRootError
from rebar._ids import binding_jira_key_map
from rebar.graph._graph import build_dep_graph
from rebar.graph._ready import find_ready_tickets
from rebar.reducer import (
    apply_ticket_filters,
    find_inbound_relationships,
    reduce_all_tickets,
    reduce_ticket,
    search_states,
)
from rebar.reducer._present import public_state
from rebar.reducer.search import project_search_result

# ───────────────────────────── result sorting (P1.1) ─────────────────────────
# Caller-facing sort key -> reduced/public-state field. Default (no --sort) keeps
# the historical reduce_all_tickets order (ticket-id directory order) byte-for-byte.
_SORT_FIELD = {
    "priority": "priority",
    "created": "created_at",
    "updated": "updated_at",
    "id": "ticket_id",
    "status": "status",
}


def sort_key_valid(sort: str) -> bool:
    """True if ``sort`` is empty (no-op) or a known key, optionally ``-``-prefixed."""
    return not sort or sort.lstrip("-") in _SORT_FIELD


def sort_states(states: list[dict], sort: str) -> list[dict]:
    """Order states by an optional ``key``/``-key`` sort.

    Missing values remain last in either direction, ties use ascending
    ``ticket_id``, and an empty or unknown key preserves input order."""
    if not sort or sort.lstrip("-") not in _SORT_FIELD:
        return states
    desc = sort.startswith("-")
    field = _SORT_FIELD[sort.lstrip("-")]
    present = [t for t in states if t.get(field) is not None]
    missing = [t for t in states if t.get(field) is None]
    # Stable: ticket_id-ascending first pass is preserved within equal primary keys.
    present.sort(key=lambda t: t.get("ticket_id") or "")
    # ``present`` already excludes rows whose ``field`` is None (filtered above),
    # so the key is always a comparable value; cast documents that for the checker.
    present.sort(key=lambda t: cast(Any, t.get(field)), reverse=desc)
    missing.sort(key=lambda t: t.get("ticket_id") or "")
    return present + missing


# ───────────────────────────── tracker resolution ────────────────────────────
# The single wording of the uninitialized-root failure. The CLI prints it as
# f"Error: {exc}", preserving the exact stderr line this path always emitted.
_NOT_A_REPO = "not inside a git repository (set REBAR_ROOT or run inside the repo)"


def tracker_dir(repo_root: str | os.PathLike[str] | None = None) -> str:
    """Resolve the read-path tracker directory.

    Explicit ``REBAR_TRACKER_DIR`` and absolute ``tracker.dir`` values bypass the
    Git precondition. Otherwise resolve the root from the argument, ``REBAR_ROOT``,
    or Git, append the configured relative directory, and raise
    :class:`TrackerRootError` outside a worktree.
    """
    from rebar.config import ConfigError, compose_config, repo_root_env, tracker_dir_override

    env_dir = tracker_dir_override()
    if env_dir:
        return env_dir
    try:
        name = compose_config(root=repo_root).tracker.dir
    except ConfigError:
        # A relocated store never reaches this branch: REBAR_TRACKER_DIR returned above
        # and an absolute tracker.dir returns verbatim below — only an unparseable
        # config lands here, and then no configured directory exists to honour.
        # tickets-boundary-ok: the ConfigError-only default INSIDE the read-path resolver
        name = ".tickets-tracker"  # malformed config never breaks a read's path resolution
    if os.path.isabs(name):
        # An absolute configured dir relocates the store (EV-3b) — like the env
        # override, return it verbatim with no git precondition on the repo root.
        return name
    root = str(repo_root) if repo_root is not None else repo_root_env()
    if not root:
        try:
            root = (
                subprocess.check_output(
                    ["git", "rev-parse", "--show-toplevel"], stderr=subprocess.DEVNULL
                )
                .decode()
                .strip()
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            raise TrackerRootError(_NOT_A_REPO) from None
    else:
        # Explicit roots must still be Git worktrees. The reconciler relies on a
        # failed read from an uninitialized root meaning "no local tickets".
        _r = subprocess.run(
            ["git", "-C", root, "rev-parse", "--is-inside-work-tree"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if _r.returncode != 0:
            raise TrackerRootError(_NOT_A_REPO) from None
    return os.path.join(root, name)


# ───────────────────────────── freshness policy ──────────────────────────────
# Reads wait briefly for the write lock, then use their consistent local snapshot.
# Writers retain the longer default.
_RECONVERGE_LOCK_TIMEOUT = 2
_LOCAL_READ_CONTEXT = ContextVar("rebar_local_read_context", default=False)
_TICKET_VIEW_CONTEXT: ContextVar[object | None] = ContextVar(
    "rebar_completion_ticket_view", default=None
)


@contextmanager
def local_read_context() -> Iterator[None]:
    """Suppress freshness sync for a bounded, explicitly local read operation."""
    token = _LOCAL_READ_CONTEXT.set(True)
    try:
        yield
    finally:
        _LOCAL_READ_CONTEXT.reset(token)


def current_ticket_view() -> Any | None:
    """The active immutable completion view, or ``None`` for ordinary live reads."""
    return _TICKET_VIEW_CONTEXT.get()


def reraise_pinned_read_failure(exc: Exception) -> None:
    """Prevent best-effort callers from downgrading an immutable-view read failure."""
    if current_ticket_view() is None:
        return
    from rebar._snapshot.ticket_view import PinnedTicketViewError

    if isinstance(exc, PinnedTicketViewError) or getattr(exc, "error_code", "") == (
        "pinned_ticket_read_failed"
    ):
        raise exc


@contextmanager
def use_ticket_view(view: object | None) -> Iterator[None]:
    """Bind one immutable completion view for deterministic in-process ticket reads."""
    token = _TICKET_VIEW_CONTEXT.set(view)
    try:
        yield
    finally:
        _TICKET_VIEW_CONTEXT.reset(token)


def _sync_disabled(root: str | None = None) -> bool:
    """Return whether typed ``sync.pull`` policy disables inbound freshness.

    Resolve from ``root`` without a Git lookup. Malformed config leaves the
    best-effort sync enabled."""
    from rebar.config import ConfigError, compose_config

    try:
        return compose_config(root=root).sync.pull == "off"
    except ConfigError:
        return False


def ensure_fresh(tracker: str, *, no_sync: bool = False) -> None:
    """Best-effort, at-most-once-per-minute fetch and in-process reconvergence.

    All read surfaces share the marker throttle and ``rebar._store.sync``
    implementation. Every failure is swallowed so remote freshness never breaks
    a read.
    """
    if _LOCAL_READ_CONTEXT.get() or no_sync:
        return
    # Resolve ``sync.pull`` and ``tickets.branch`` from the code checkout, not a
    # relocated tracker's parent, after the cheap local/no-sync exits.
    from rebar.config import repo_root_or_none

    cfg_root = repo_root_or_none()
    if _sync_disabled(cfg_root):
        return
    try:
        from rebar.config import tickets_branch

        tracker_abs = os.path.realpath(tracker)
        if not os.path.isdir(tracker_abs):
            return
        # Branch resolved from the CODE repo config (same cfg_root as _sync_disabled above);
        # a ConfigError is swallowed by the outer best-effort guard.
        branch = tickets_branch(cfg_root)
        # Only sync a tracker with a real tickets branch.
        r = subprocess.run(
            ["git", "-C", tracker_abs, "rev-parse", "--verify", branch],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if r.returncode != 0:
            return
        md5_12 = hashlib.md5(tracker_abs.encode()).hexdigest()[:12]
        marker = f"/tmp/.ticket-sync-{md5_12}"
        now = int(time.time())
        marker_age = 9999
        try:
            with open(marker) as fh:
                marker_age = now - int(fh.read().strip() or 0)
        except (OSError, ValueError):
            marker_age = 9999
        if marker_age < 60:
            return
        # Write the marker before reconverging so only the first read in a burst
        # waits on the lock. Later reads immediately use consistent snapshots.
        try:
            with open(marker, "w") as fh:
                fh.write(str(now))
        except OSError:
            pass
        # The marker owns throttling. Reconverge uses the short read-lock budget so
        # a concurrent writer cannot stall this best-effort path.
        from rebar._store import sync as _store_sync

        try:
            _store_sync.reconverge(tracker_abs, lock_timeout=_RECONVERGE_LOCK_TIMEOUT)
        except Exception:  # noqa: BLE001 — best-effort reconverge: a sync failure never breaks a read (fsck surfaces PUSH_PENDING)
            pass
    except Exception:  # noqa: BLE001 — freshness is best-effort; never let it break a read
        # Freshness is best-effort; never let it break a read.
        return


# ───────────────────────────── library-facing state helpers ──────────────────
# These return parsed Python objects (the SAME shapes rebar/_reads.py exposed),
# with no formatting and no freshness — the caller decides whether to sync.
class ReadError(Exception):
    """A read failed (missing/unresolvable id, archived target, …). Carries the
    exact stderr message the CLI emits, so the library can mirror it."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class TicketNotFoundError(ReadError):
    """A ticket-not-found failure (ticket 8a31). A ReadError subclass so every
    existing ``except ReadError`` still catches it, but with a TYPE identity so the
    error classifier can map it to ``ticket_not_found`` without message sniffing."""


def inbound_deps_state(ticket_id: str, tracker: str) -> list[dict]:
    """Return sorted inbound links with source status for ``show``.

    LINK records store only outgoing edges. Deriving their reverse view through
    ``find_inbound_relationships`` avoids a drift-prone mirror and makes blocker
    status available in one response. Each row is
    ``{"from_id", "relation", "status"}``.
    """
    entries: list[dict] = []
    for link in find_inbound_relationships(ticket_id, tracker)["inbound_links"]:
        src = reduce_ticket(os.path.join(tracker, link["from_id"]))
        status = src.get("status", "") if isinstance(src, dict) else ""
        entries.append({"from_id": link["from_id"], "relation": link["relation"], "status": status})
    return entries


def show_state(
    ticket_id: str,
    tracker: str,
    *,
    include_scratch: bool = False,
    include_inbound: bool = False,
) -> dict:
    resolved = resolve_ticket_id(ticket_id, tracker)
    if resolved is None:
        raise TicketNotFoundError(f"Ticket '{ticket_id}' not found")
    ticket_path = os.path.join(tracker, resolved)
    if not os.path.isdir(ticket_path):
        raise TicketNotFoundError(f"Ticket '{ticket_id}' not found")
    state = reduce_ticket(ticket_path)
    if state is None:
        raise ReadError(f'ticket "{resolved}" has no CREATE or SNAPSHOT event')
    if state.get("status") in ("error", "fsck_needed"):
        raise ReadError(f'ticket "{resolved}" has status "{state["status"]}"')
    # Keep the default compiled-state shape shared with list/search.
    # ``include_inbound`` adds the edge view only for per-ticket surfaces.
    state = public_state(state)
    if not state.get("ticket_type"):
        raise ReadError(f'ticket "{resolved}" has no CREATE or SNAPSHOT event')
    if include_inbound:
        # Additive computed key — the stored (outgoing) `deps` list and every
        # other existing key are untouched. Opt-in so the hot internal callers
        # (gates, field reads) never pay the corpus scan.
        state["inbound_deps"] = inbound_deps_state(resolved, tracker)
    if include_scratch:
        state["scratch"] = _load_scratch(resolved)
    return state


def _load_scratch(ticket_id: str) -> dict:
    # Resolve ``scratch.base_dir`` against the code checkout, not a relocated
    # tracker's parent. Explicit roots avoid Git discovery. Malformed config falls
    # back to the display path.
    from rebar.config import ConfigError, compose_config, repo_root_or_none

    repo_root = repo_root_or_none()
    try:
        scratch_base = compose_config(root=repo_root).scratch.base_dir.strip()
    except ConfigError:
        scratch_base = ""
    if not scratch_base:
        base_root = repo_root or os.getcwd()
        scratch_base = os.path.join(base_root, ".rebar", "scratch")
    scratch_dir = os.path.join(scratch_base, ticket_id)
    data: dict[str, Any] = {}
    if os.path.isdir(scratch_dir):
        for entry in sorted(os.listdir(scratch_dir)):
            path = os.path.join(scratch_dir, entry)
            if not os.path.isfile(path):
                continue
            if entry.startswith(".") or ".tmp." in entry:
                continue
            try:
                with open(path, encoding="utf-8") as fh:
                    envelope = json.load(fh)
                data[entry] = {
                    "ts": envelope.get("ts", ""),
                    "value": envelope.get("value", ""),
                }
            except (OSError, json.JSONDecodeError):
                pass
    return data


#: Fields excluded from discovery lists when bodies are omitted.
#:
#: Lists use these rows only for selection. Ticket bodies and signature material
#: remain available through ``show`` and verification surfaces.
LEAN_OMITTED_FIELDS: tuple[str, ...] = (
    "description",
    "comments",
    "authorship_ledger",
    "attestations",
    "signature",
    "keyring",
)


def lean_projection(state: dict) -> dict:
    """Copy ``state`` without :data:`LEAN_OMITTED_FIELDS`.

    Event-log and snapshot list backends share this definition, keeping lean row
    shapes identical.
    """
    return {k: v for k, v in state.items() if k not in LEAN_OMITTED_FIELDS}


def list_states(tracker: str, query: TicketQuery | None = None) -> list[dict]:
    """List states filtered by ``query``.

    ``min_children`` and ``blocking_state`` reuse reducer and graph data.
    ``with_children_count`` is opt-in to preserve the default show/list/search
    shape. When ``include_body`` is false, agent-facing lists omit bodies and
    signature material. Internal callers retain them. A missing query selects
    every ticket."""
    if query is None:
        query = TicketQuery()
    # Unpack once into locals; the filter body below is unchanged. ``ticket_type``
    # is a local (it is reassigned for the detected_by:* auto-intersect below).
    status = query.status
    ticket_type = query.ticket_type
    priority = query.priority
    parent = query.parent
    has_tag = query.has_tag
    without_tag = query.without_tag
    include_archived = query.include_archived
    exclude_deleted = query.exclude_deleted
    min_children = query.min_children
    blocking_state = query.blocking_state
    with_children_count = query.with_children_count
    sort = query.sort
    include_body = query.include_body
    # detected_by:* tags are bug-only — auto-intersect with --type=bug.
    if has_tag.startswith("detected_by:") and not ticket_type:
        ticket_type = "bug"
    parent_filter = parent
    if parent_filter:
        parent_filter = resolve_ticket_id(parent_filter, tracker) or parent_filter
    # Non-graph artifacts are hidden from default `list` (searchable via
    # `search`/`show` only) — surface them ONLY when the type filter explicitly
    # selects one, including within a comma-separated OR list. `validate` reaches
    # list_states with no type filter, so it inherits the exclusion (artifacts are
    # never health-flagged).
    from rebar.reducer._api import _NON_GRAPH_ARTIFACT_TYPES

    requested_types = {value.strip() for value in ticket_type.split(",") if value.strip()}
    # A lean list drops LEAN_OMITTED_FIELDS *during* the reduce, not after it: nothing
    # between here and the return consumes those six fields (the filters key off
    # type/status/parent/tag/priority, the child_counts pass off ``parent_id``,
    # ``blocking_state`` off the readiness graph, and ``sort_states`` off
    # priority/created/updated/id/status), so projecting inline is behaviour-identical
    # while keeping the whole store's bodies + signature material from ever being
    # simultaneously live. ``lean_projection`` below stays as the one spelling of the
    # row shape (now a no-op for these keys).
    results = reduce_all_tickets(
        tracker,
        exclude_archived=not include_archived,
        exclude_deleted=exclude_deleted,
        exclude_session_logs=requested_types.isdisjoint(_NON_GRAPH_ARTIFACT_TYPES),
        omit_fields=() if include_body else LEAN_OMITTED_FIELDS,
    )
    # children_count: direct non-deleted children per ticket, counted over the
    # reduced set BEFORE the narrowing filters (a closed child still counts).
    child_counts: dict[str, int] = {}
    for t in results:
        pid = t.get("parent_id")
        if pid:
            child_counts[pid] = child_counts.get(pid, 0) + 1
    results = apply_ticket_filters(
        results,
        type_filter=ticket_type,
        status_filter=status,
        parent_filter=parent_filter,
        tag_filter=has_tag,
        priority_filter=priority,
        without_tag_filter=without_tag,
    )
    if blocking_state in ("unblocked", "blocked"):
        ready_ids = {s.get("ticket_id") for s in find_ready_tickets(tracker)}
        if blocking_state == "unblocked":
            results = [t for t in results if t.get("ticket_id") in ready_ids]
        else:  # "blocked": active (open/in_progress) ticket with an unclosed blocker
            results = [
                t
                for t in results
                if t.get("ticket_id") not in ready_ids
                and t.get("status") in ("open", "in_progress")
            ]
    out = []
    for t in results:
        cc = child_counts.get(t.get("ticket_id", ""), 0)
        if min_children is not None and cc < min_children:
            continue
        ps = public_state(t)
        if not include_body:
            ps = lean_projection(ps)
        if with_children_count:
            ps["children_count"] = cc
        out.append(ps)
    return sort_states(out, sort)


def deps_state(ticket_id: str, tracker: str, *, include_archived: bool = False) -> dict:
    resolved = resolve_ticket_id(ticket_id, tracker)
    if resolved is None:
        raise ReadError(f"ticket '{ticket_id}' does not exist")
    if not include_archived:
        try:
            target_state = reduce_ticket(os.path.join(tracker, resolved))
        except Exception:  # noqa: BLE001 — reduce_ticket fallback: an unreducible target skips the archived check
            target_state = None
        if isinstance(target_state, dict) and target_state.get("archived") is True:
            raise ReadError(
                f"ticket '{resolved}' is archived. "
                "Use --include-archived to include archived tickets."
            )
    return build_dep_graph(resolved, tracker, exclude_archived=not include_archived)


def ready_states(tracker: str, *, epic: str | None = None, sort: str = "") -> list[dict]:
    if epic:
        epic = resolve_ticket_id(epic, tracker) or epic
    states = [public_state(s) for s in find_ready_tickets(tracker, epic_filter=epic)]
    return sort_states(states, sort)


def search_state(
    tracker: str,
    query: str,
    *,
    status: str | None = None,
    ticket_type: str | None = None,
    has_tag: str | None = None,
    include_archived: bool = False,
    sort: str = "",
    full: bool = False,
) -> list[dict]:
    states = reduce_all_tickets(
        tracker, exclude_archived=not include_archived, exclude_deleted=True
    )
    # Add Jira keys once from the binding store so search matches show resolution
    # without introducing filesystem access into the reducer.
    jira_by_ticket = binding_jira_key_map(tracker)
    if jira_by_ticket:
        for st in states:
            if isinstance(st, dict):
                ticket_id = st.get("ticket_id")
                jira_key = jira_by_ticket.get(ticket_id) if isinstance(ticket_id, str) else None
                if jira_key:
                    st["jira_key"] = jira_key
    results = search_states(
        states,
        query,
        status=status,
        ticket_type=ticket_type,
        has_tag=has_tag,
        parent_resolver=lambda v: resolve_ticket_id(v, tracker) or v,
    )
    public_results = sort_states([public_state(t) for t in results], sort)
    if full:
        return public_results
    return [project_search_result(t, query) for t in public_results]


def recent_session_logs_state(tracker: str, *, limit: int = 5) -> list[dict]:
    """Return session logs in descending ``created_at`` order without archived or deleted rows.

    Session logs are hidden from normal lists. Nonpositive limits return no rows."""
    states = reduce_all_tickets(tracker, exclude_archived=True, exclude_deleted=True)
    logs = [t for t in states if t.get("ticket_type") == "session_log"]
    # created_at is the CREATE-event timestamp (ns); missing/None sorts oldest.
    logs.sort(key=lambda t: t.get("created_at") or 0, reverse=True)
    return [public_state(t) for t in logs[: max(0, limit)]]


# ───────────────────────────── CLI command handlers ──────────────────────────


def main(argv: list[str] | None = None) -> int:
    """Backward-compatible read-CLI entrypoint — the ``_cmd_*`` arms now live in
    ``rebar._engine_support.reads_cli``; delegate to it (lazy import keeps the
    facade module free of a reads_cli dependency)."""
    from rebar._engine_support.reads_cli import main as _main

    return _main(argv)


if __name__ == "__main__":
    sys.exit(main())
