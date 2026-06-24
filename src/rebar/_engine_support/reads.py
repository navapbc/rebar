"""Single-source read implementation for CLI, library, and MCP (story 23d2-e0f3).

Before this module, CLI reads went ``bash ticket-show.sh/ticket-list.sh ->
python3 heredoc -> ticket_reducer`` while library/MCP reads went
``rebar/_reads.py -> ticket_reducer`` in-process, so arg parsing, id-resolution
dispatch, and error-message STRINGS were written twice and pinned only by a
parity test. This module collapses the two: it is the ONE implementation of the
five read commands (show / list / ready / search / deps), consumed by:

  * the CLI dispatcher arms, via ``ticket-reads.py <sub> ...`` (this module's
    ``main``), which formats output + emits the historical CLI text/JSON/errors;
  * ``rebar/_reads.py`` (library + MCP), which calls the ``*_state`` helpers for
    the parsed-object return shapes.

It lives in the engine package dir (alongside ``ticket-ready.py`` /
``ticket-search.py``) so it is importable both by the engine's bare ``python3``
(via PYTHONPATH) and by the library (via ``rebar._native``'s sys.path insert) —
the engine never imports the ``rebar`` package, so the single implementation must
live here, not in ``rebar/_reads.py``.

Read-freshness policy (uniform across interfaces): each read first runs a
best-effort, throttled (<=1/min) ``git fetch origin tickets`` + reconverge via
the shared ``ticket-sync.sh`` ``_reconverge_tickets`` (the SAME mechanism and the
SAME ``/tmp/.ticket-sync-<md5>`` throttle marker the dispatcher's
``_ensure_initialized`` used) so all three interfaces share one contract. Opt out
with ``REBAR_SYNC_PULL=off`` (deprecated alias ``REBAR_NO_SYNC=1``) or the
``--no-pull`` CLI flag (deprecated alias ``--no-sync``).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from typing import Any

from rebar._engine import engine_dir as _engine_dir
from rebar._engine_support.resolver import resolve_ticket_id
from rebar.graph._graph import build_dep_graph
from rebar.graph._ready import find_ready_tickets
from rebar.reducer import (
    apply_ticket_filters,
    reduce_all_tickets,
    reduce_ticket,
    search_states,
)
from rebar.reducer._present import public_state

# The bundled engine dir holds the read packages' supporting assets. This is the
# in-process read implementation, so ``rebar`` is always importable here and the
# read packages import as real ``rebar.*`` subpackages.
_SCRIPTS_DIR = _engine_dir()


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
    """Return ``states`` ordered by ``sort`` (``key`` asc, ``-key`` desc).

    Unset values always sort LAST in both directions (a ``(is_none, value)``
    discipline implemented by partitioning, NOT ``key or 0`` — which would sort
    an unset priority as 0 and raise on mixed None/int). Ties break by
    ``ticket_id`` ascending regardless of direction (stable two-stage sort).
    An empty/unknown ``sort`` returns the input list unchanged (default order)."""
    if not sort or sort.lstrip("-") not in _SORT_FIELD:
        return states
    desc = sort.startswith("-")
    field = _SORT_FIELD[sort.lstrip("-")]
    present = [t for t in states if t.get(field) is not None]
    missing = [t for t in states if t.get(field) is None]
    # Stable: ticket_id-ascending first pass is preserved within equal primary keys.
    present.sort(key=lambda t: t.get("ticket_id") or "")
    present.sort(key=lambda t: t.get(field), reverse=desc)
    missing.sort(key=lambda t: t.get("ticket_id") or "")
    return present + missing


# ───────────────────────────── tracker resolution ────────────────────────────
def tracker_dir(repo_root: str | os.PathLike[str] | None = None) -> str:
    """Resolve the tracker dir for the read path. The configurable dir NAME comes from
    the single source of truth (``rebar.config``: the ``REBAR_TRACKER_DIR`` override or
    the ``tracker.dir`` config key, default ``.tickets-tracker``); this function adds the
    read-path-specific git precondition + ``sys.exit`` on an uninitialized root.

    Resolution: an explicit override / absolute configured dir is returned verbatim with
    NO git precondition (test fixtures and tooling point this at a hand-built tracker on
    purpose); otherwise the relative dir name is joined under the resolved repo root
    (explicit arg > REBAR_ROOT > git toplevel of cwd), which must be a git work tree.
    """
    from rebar.config import ConfigError, load_config, tracker_dir_override

    env_dir = tracker_dir_override()
    if env_dir:
        return env_dir
    try:
        name = load_config(root=repo_root).tracker.dir
    except ConfigError:
        name = ".tickets-tracker"  # malformed config never breaks a read's path resolution
    if os.path.isabs(name):
        # An absolute configured dir relocates the store (EV-3b) — like the env
        # override, return it verbatim with no git precondition on the repo root.
        return name
    root = str(repo_root) if repo_root is not None else os.environ.get("REBAR_ROOT")
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
            print(
                "Error: not inside a git repository (set REBAR_ROOT or run inside the repo)",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        # A repo-root was supplied (arg / REBAR_ROOT). The rebar
        # store is git-backed, so require root to be a git work tree — matching
        # the pre-collapse bash read path, which errored ("not inside a git
        # repository") on an uninitialized dir. This is the precondition
        # rebar_reconciler._read_local_tickets relies on: against a minimal /
        # uninitialized environment `rebar list` must fail so the reconciler
        # treats it as "no local tickets" rather than reading a half-built,
        # uncommitted working tree. (Bug: see ticket below.)
        _r = subprocess.run(
            ["git", "-C", root, "rev-parse", "--is-inside-work-tree"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if _r.returncode != 0:
            print(
                "Error: not inside a git repository (set REBAR_ROOT or run inside the repo)",
                file=sys.stderr,
            )
            sys.exit(1)
    return os.path.join(root, name)


# ───────────────────────────── freshness policy ──────────────────────────────
def _sync_disabled(root: str | None = None) -> bool:
    """Whether inbound freshness (fetch/reconverge) is turned off — the ``sync.pull``
    policy resolved via the typed config (env ``REBAR_SYNC_PULL=off``, deprecated
    alias ``REBAR_NO_SYNC``, or a config file). ``root`` (the repo dir holding the
    tracker) is passed explicitly so resolution is pure stat-based discovery — no
    ``git`` subprocess for root detection. Best-effort: a malformed config leaves
    sync enabled (every fetch failure is swallowed downstream anyway)."""
    from rebar.config import ConfigError, load_config

    try:
        return load_config(root=root).sync.pull == "off"
    except ConfigError:
        return False


def ensure_fresh(tracker: str, *, no_sync: bool = False) -> None:
    """Best-effort, throttled (<=1/min) fetch + reconverge of the local tickets
    branch with origin/tickets. Shared by CLI/library/MCP so all three observe
    the same freshness contract.

    Reuses the dispatcher's exact mechanism: the ``/tmp/.ticket-sync-<md5>``
    throttle marker AND the ``_reconverge_tickets`` function in ``ticket-sync.sh``
    (HEAD-based local-ahead detection, merge-as-union, lock-guarded reset) — so
    there is ONE sync implementation, not a reinvented one. Every failure path is
    swallowed: a read must never fail because a fetch could not run.
    """
    if no_sync or _sync_disabled(os.path.dirname(os.path.realpath(tracker))):
        return
    try:
        from rebar.config import tickets_branch

        tracker_abs = os.path.realpath(tracker)
        if not os.path.isdir(tracker_abs):
            return
        # Branch resolved from the MAIN repo config (parent of the tracker), matching
        # _sync_disabled above; a ConfigError is swallowed by the outer best-effort guard.
        branch = tickets_branch(os.path.dirname(tracker_abs))
        # Only sync a tracker with a real tickets branch (matches _ensure_initialized).
        r = subprocess.run(
            ["git", "-C", tracker_abs, "rev-parse", "--verify", branch],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
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
        # Reconverge in-process (Tier D retired the bash helper; rebar._store.sync is
        # the sole impl). The throttle/marker above is the single owner — reconverge
        # itself is throttle-free.
        from rebar._store import sync as _store_sync

        try:
            _store_sync.reconverge(tracker_abs)
        except Exception:
            pass
        try:
            with open(marker, "w") as fh:
                fh.write(str(now))
        except OSError:
            pass
    except Exception:
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


def show_state(ticket_id: str, tracker: str, *, include_scratch: bool = False) -> dict:
    resolved = resolve_ticket_id(ticket_id, tracker)
    if resolved is None:
        raise ReadError(f"Ticket '{ticket_id}' not found")
    ticket_path = os.path.join(tracker, resolved)
    if not os.path.isdir(ticket_path):
        raise ReadError(f"Ticket '{ticket_id}' not found")
    state = reduce_ticket(ticket_path)
    if state is None:
        raise ReadError(f'ticket "{resolved}" has no CREATE or SNAPSHOT event')
    if state.get("status") in ("error", "fsck_needed"):
        raise ReadError(f'ticket "{resolved}" has status "{state["status"]}"')
    # NOTE: show emits the SAME compiled-state shape as list/search (the
    # production dispatcher's `show` arm never augmented with inbound_links /
    # children — only the now-deleted standalone ticket-show.sh shim did, and it
    # was never wired into the dispatcher). Collapsing the dual read path keeps
    # the production show==list==search contract (test_reducer_single_source).
    state = public_state(state)
    if not state.get("ticket_type"):
        raise ReadError(f'ticket "{resolved}" has no CREATE or SNAPSHOT event')
    if include_scratch:
        state["scratch"] = _load_scratch(resolved, tracker)
    return state


def _load_scratch(ticket_id: str, tracker: str) -> dict:
    repo_root = os.path.dirname(os.path.abspath(tracker))
    # scratch.base_dir via the typed config (env REBAR_SCRATCH_BASE_DIR, deprecated
    # alias SCRATCH_BASE_DIR, or a config file). Explicit root → pure stat discovery
    # (no git subprocess); a malformed config falls back to the default (display path).
    from rebar.config import ConfigError, load_config

    try:
        scratch_base = load_config(root=repo_root).scratch.base_dir.strip()
    except ConfigError:
        scratch_base = ""
    if not scratch_base:
        scratch_base = os.path.join(repo_root, ".rebar", "scratch")
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


def list_states(
    tracker: str,
    *,
    status: str = "",
    ticket_type: str = "",
    priority: str = "",
    parent: str = "",
    has_tag: str = "",
    without_tag: str = "",
    include_archived: bool = False,
    exclude_deleted: bool = False,
    min_children: int | None = None,
    blocking_state: str = "",
    with_children_count: bool = False,
    sort: str = "",
) -> list[dict]:
    """List ticket states. Two universal cross-ticket filters reuse the same
    reducer/graph the bespoke ``list-epics`` used: ``min_children`` (keep tickets
    with ≥ N direct children) and ``blocking_state`` ("unblocked" = all blockers
    closed via ``find_ready_tickets``; "blocked" = active with an open blocker).
    ``with_children_count`` additionally surfaces a ``children_count`` field — kept
    OPT-IN so the default list shape stays identical to show/search (the
    single-reducer invariant, bug f026). These generalize what ``list-epics``
    filtered by, so it becomes a thin wrapper over ``list``."""
    # detected_by:* tags are bug-only — auto-intersect with --type=bug.
    if has_tag.startswith("detected_by:") and not ticket_type:
        ticket_type = "bug"
    parent_filter = parent
    if parent_filter:
        parent_filter = resolve_ticket_id(parent_filter, tracker) or parent_filter
    # session_log tickets are hidden from default `list` (they are searchable via
    # `search`/`show` only) — surface them ONLY when the type filter explicitly
    # selects them (`list --type=session_log`). `validate` reaches list_states with
    # no type filter, so it inherits the exclusion (logs are never health-flagged).
    results = reduce_all_tickets(
        tracker,
        exclude_archived=not include_archived,
        exclude_deleted=exclude_deleted,
        exclude_session_logs=(ticket_type != "session_log"),
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
        cc = child_counts.get(t.get("ticket_id"), 0)
        if min_children is not None and cc < min_children:
            continue
        ps = public_state(t)
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
        except Exception:
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
) -> list[dict]:
    states = reduce_all_tickets(
        tracker, exclude_archived=not include_archived, exclude_deleted=True
    )
    results = search_states(
        states,
        query,
        status=status,
        ticket_type=ticket_type,
        has_tag=has_tag,
        parent_resolver=lambda v: resolve_ticket_id(v, tracker) or v,
    )
    return sort_states([public_state(t) for t in results], sort)


def recent_session_logs_state(tracker: str, *, limit: int = 5) -> list[dict]:
    """The ``limit`` newest ``session_log`` tickets, ordered by ``created_at``
    (ns) descending. session_logs are hidden from default ``list`` but are the
    sole subject here, so this is the one read that includes them by type — the
    counterpart to ``search``/``show``. Archived/deleted logs are excluded; a
    ``limit`` <= 0 returns an empty list."""
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
