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
from rebar._engine_support.output import OutputFormatError, error_envelope, parse_output
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
from rebar.reducer.llm_format import to_llm

# The bundled engine dir holds the read packages' supporting assets. This is the
# in-process read implementation, so ``rebar`` is always importable here and the
# read packages import as real ``rebar.*`` subpackages.
_SCRIPTS_DIR = _engine_dir()


# ───────────────────────────── tracker resolution ────────────────────────────
def tracker_dir(repo_root: str | os.PathLike[str] | None = None) -> str:
    """Resolve the tracker dir: TICKETS_TRACKER_DIR, else <repo_root>/.tickets-tracker.

    repo_root precedence: explicit arg > REBAR_ROOT > git toplevel
    of cwd. Mirrors the shims' resolution so the CLI/library agree.
    """
    env_dir = os.environ.get("TICKETS_TRACKER_DIR")
    if env_dir:
        # Explicit tracker dir — read it directly, no git precondition (test
        # fixtures and tooling point this at a hand-built tracker on purpose).
        return env_dir
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
    return os.path.join(root, ".tickets-tracker")


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
        tracker_abs = os.path.realpath(tracker)
        if not os.path.isdir(tracker_abs):
            return
        # Only sync a tracker with a real tickets branch (matches _ensure_initialized).
        r = subprocess.run(
            ["git", "-C", tracker_abs, "rev-parse", "--verify", "tickets"],
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
    results = reduce_all_tickets(
        tracker, exclude_archived=not include_archived, exclude_deleted=exclude_deleted
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
    return out


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


def ready_states(tracker: str, *, epic: str | None = None) -> list[dict]:
    if epic:
        epic = resolve_ticket_id(epic, tracker) or epic
    return [public_state(s) for s in find_ready_tickets(tracker, epic_filter=epic)]


def search_state(
    tracker: str,
    query: str,
    *,
    status: str | None = None,
    ticket_type: str | None = None,
    has_tag: str | None = None,
    include_archived: bool = False,
) -> list[dict]:
    states = reduce_all_tickets(
        tracker, exclude_archived=not include_archived, exclude_deleted=True
    )
    results = search_states(states, query, status=status, ticket_type=ticket_type, has_tag=has_tag)
    return [public_state(t) for t in results]


# ───────────────────────────── CLI command handlers ──────────────────────────
def _bridge_alert_warning(states: list[dict]) -> str | None:
    alerted = sum(
        1 for t in states if any(not a.get("resolved", False) for a in t.get("bridge_alerts", []))
    )
    if alerted > 0:
        return (
            f"WARNING: {alerted} ticket(s) have unresolved bridge alerts. "
            "Run: ticket bridge-status for details."
        )
    return None


def _cmd_show(argv: list[str], tracker: str) -> int:
    usage = "Usage: ticket show [--output llm] [--include-scratch] <ticket_id> [<ticket_id> ...]"
    try:
        fmt, rest = parse_output(argv, "reader")
    except OutputFormatError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    include_scratch = False
    ids: list[str] = []
    for arg in rest:
        if arg == "--include-scratch":
            include_scratch = True
        elif arg.startswith("-"):
            print(f"Error: unknown option '{arg}'", file=sys.stderr)
            print(usage, file=sys.stderr)
            # Unrecognized option is a usage error (2), not a runtime error (1) —
            # matching deps/ready/search (the canonical contract in exit-codes.md).
            return 2
        else:
            ids.append(arg)
    if not ids:
        print(usage, file=sys.stderr)
        return 1

    overall_rc = 0
    for idx, raw_id in enumerate(ids):
        if idx > 0 and fmt != "llm":
            print()
        try:
            state = show_state(raw_id, tracker, include_scratch=include_scratch)
        except ReadError as exc:
            # show emits a parseable JSON error to stdout AND a free-form stderr
            # line (preserving the historical contract callers depend on).
            print(
                json.dumps(
                    error_envelope("ticket_not_found", raw_id, f"Ticket '{raw_id}' not found", 1),
                    ensure_ascii=False,
                )
                if "not found" in exc.message
                else json.dumps(
                    error_envelope("show_failed", raw_id, exc.message, 1), ensure_ascii=False
                )
            )
            print(f"Error: {exc.message}", file=sys.stderr)
            overall_rc = 1
            continue
        if fmt == "llm":
            print(json.dumps(to_llm(state), ensure_ascii=False, separators=(",", ":")))
        else:
            print(json.dumps(state, indent=2, ensure_ascii=False))
            unresolved = sum(
                1 for a in state.get("bridge_alerts", []) if not a.get("resolved", False)
            )
            if unresolved > 0:
                print(
                    f"WARNING: ticket {state.get('ticket_id')} has {unresolved} "
                    "unresolved bridge alert(s). Run: ticket bridge-status for details.",
                    file=sys.stderr,
                )
    return overall_rc


def _cmd_list(argv: list[str], tracker: str) -> int:
    usage = (
        "Usage: ticket list [--output llm] [--include-archived] [--exclude-deleted] "
        "[--type=<type>] [--status=<status>] [--priority=<n>] [--parent=<id>] "
        "[--has-tag=<tag>] [--without-tag=<tag>] [--min-children=<n>] "
        "[--unblocked|--blocked] [--with-children-count]"
    )
    try:
        fmt, rest = parse_output(argv, "reader")
    except OutputFormatError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    opts = {
        "include_archived": False,
        "exclude_deleted": False,
        "ticket_type": "",
        "status": "",
        "parent": "",
        "has_tag": "",
        "priority": "",
        "without_tag": "",
        "min_children": None,
        "blocking_state": "",
        "with_children_count": False,
    }
    for arg in rest:
        if arg == "--include-archived":
            opts["include_archived"] = True
        elif arg == "--exclude-deleted":
            opts["exclude_deleted"] = True
        elif arg.startswith("--type="):
            opts["ticket_type"] = arg[len("--type=") :]
        elif arg.startswith("--status="):
            opts["status"] = arg[len("--status=") :]
        elif arg.startswith("--parent="):
            opts["parent"] = arg[len("--parent=") :]
        elif arg.startswith("--has-tag="):
            opts["has_tag"] = arg[len("--has-tag=") :]
        elif arg.startswith("--priority="):
            opts["priority"] = arg[len("--priority=") :]
        elif arg.startswith("--without-tag="):
            opts["without_tag"] = arg[len("--without-tag=") :]
        elif arg.startswith("--min-children="):
            raw = arg[len("--min-children=") :]
            if not raw.isdigit():
                print(
                    f"Error: --min-children expects a non-negative integer, got '{raw}'",
                    file=sys.stderr,
                )
                return 2
            opts["min_children"] = int(raw)
        elif arg == "--unblocked":
            opts["blocking_state"] = "unblocked"
        elif arg == "--blocked":
            opts["blocking_state"] = "blocked"
        elif arg == "--with-children-count":
            opts["with_children_count"] = True
        elif arg in ("--help", "-h"):
            print(usage, file=sys.stderr)
            return 0
        else:
            print(f"Error: unknown option '{arg}'", file=sys.stderr)
            print(
                "Valid filters: --type --status --priority --parent --has-tag "
                "--without-tag --min-children --unblocked --blocked --with-children-count "
                "--include-archived --exclude-deleted --output llm",
                file=sys.stderr,
            )
            # Unrecognized option is a usage error (2), not a runtime error (1) —
            # matching deps/ready/search (the canonical contract in exit-codes.md).
            return 2

    # --priority: integers 0-4 (comma-separated for OR).
    pri = opts["priority"]
    if pri:
        if any(c not in "0123456789," for c in pri):
            print(
                f"Error: --priority expects integer values 0-4 "
                f"(comma-separated for OR), got '{pri}'",
                file=sys.stderr,
            )
            return 1
        for p in pri.split(","):
            if p not in ("", "0", "1", "2", "3", "4"):
                print(
                    f"Error: --priority value '{p}' out of range (expected 0-4)",
                    file=sys.stderr,
                )
                return 1

    if not os.path.isdir(tracker):
        print("Error: ticket system not initialized. Run 'ticket init' first.", file=sys.stderr)
        return 1

    results = list_states(tracker, **opts)
    if fmt == "llm":
        for t in results:
            print(json.dumps(to_llm(t), ensure_ascii=False, separators=(",", ":")))
    else:
        print(json.dumps(results, ensure_ascii=False))
        warning = _bridge_alert_warning(results)
        if warning:
            print(warning, file=sys.stderr)
    return 0


def _cmd_deps(argv: list[str], tracker: str) -> int:
    include_archived = "--include-archived" in argv
    for arg in argv:
        if arg.startswith("-") and arg != "--include-archived":
            # Reject unknown options instead of silently dropping them
            # (matching list/show/ready/search).
            print(f"Error: unknown option '{arg}'", file=sys.stderr)
            print("Usage: ticket deps <ticket_id> [--include-archived]", file=sys.stderr)
            return 2
    pos = [a for a in argv if not a.startswith("-")]
    if not pos:
        print("Usage: ticket deps <ticket_id>", file=sys.stderr)
        return 1
    try:
        result = deps_state(pos[0], tracker, include_archived=include_archived)
    except ReadError as exc:
        # deps is a reader (always-JSON): emit a machine-readable error_envelope on
        # stdout (like show) so callers' json.load succeeds, plus prose on stderr.
        code = "ticket_not_found" if "not found" in exc.message else "deps_failed"
        print(json.dumps(error_envelope(code, pos[0], exc.message, 1), ensure_ascii=False))
        print(f"Error: {exc.message}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _cmd_ready(argv: list[str], tracker: str) -> int:
    try:
        fmt, rest = parse_output(argv, "ready")
    except OutputFormatError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    epic = None
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg == "--epic":
            epic = rest[i + 1] if i + 1 < len(rest) else None
            i += 2
            continue
        if arg.startswith("--epic="):
            epic = arg[len("--epic=") :]
            i += 1
            continue
        if arg.startswith("-"):
            # Reject unknown options, including the removed legacy `--json`
            # (use `--output json`). Mirrors the old ready arm's exit 2.
            print(f"Error: unknown option '{arg}'", file=sys.stderr)
            print(
                "Usage: ticket ready [--output json|llm] [--epic <id>]",
                file=sys.stderr,
            )
            return 2
        i += 1
    states = ready_states(tracker, epic=epic)
    if fmt == "json":
        print(json.dumps(states, ensure_ascii=False))
    elif fmt == "llm":
        for s in states:
            print(json.dumps(to_llm(s), ensure_ascii=False))
    else:  # text: one id per line
        for s in states:
            tid = s.get("ticket_id")
            if tid:
                print(tid)
    return 0


def _cmd_search(argv: list[str], tracker: str) -> int:
    query = None
    status = ticket_type = has_tag = None
    include_archived = False
    for arg in argv:
        if arg.startswith("--status="):
            status = arg[len("--status=") :]
        elif arg.startswith("--type="):
            ticket_type = arg[len("--type=") :]
        elif arg.startswith("--has-tag="):
            has_tag = arg[len("--has-tag=") :]
        elif arg == "--include-archived":
            include_archived = True
        elif arg.startswith("-"):
            # Reject unknown options (e.g. the removed legacy `--json`) instead
            # of silently ignoring them — matching list/show/ready. The old shim
            # tolerated them; post-collapse there is no reason to.
            print(f"Error: unknown option '{arg}'", file=sys.stderr)
            print(
                "Usage: ticket search <query> [--status=S] [--type=T] "
                "[--has-tag=TAG] [--include-archived]",
                file=sys.stderr,
            )
            return 2
        elif query is None:
            query = arg
    if query is None:
        print(
            "Usage: ticket search <query> [--status=S] [--type=T] [--has-tag=TAG]",
            file=sys.stderr,
        )
        return 2
    results = search_state(
        tracker,
        query,
        status=status,
        ticket_type=ticket_type,
        has_tag=has_tag,
        include_archived=include_archived,
    )
    print(json.dumps(results, ensure_ascii=False))
    return 0


def _cmd_list_epics(argv: list[str], tracker: str) -> int:
    """DEPRECATED thin wrapper over the generic list: a deprecation warning, then
    exactly TWO generic calls — one for epics, one for P0 bugs — assembled into
    ``{p0_bugs, epics}``. Replaces the retired bespoke list-epics reduction.
    Blocking-awareness is the generic blocking_state filter (default: unblocked)."""
    print(
        "WARNING: 'list-epics' is deprecated and will be removed in a future "
        "release. Use 'rebar list --type=epic --status=open,in_progress --unblocked "
        "[--min-children=N]' and 'rebar list --type=bug --priority=0'.",
        file=sys.stderr,
    )
    try:
        fmt, rest = parse_output(argv, "report")
    except OutputFormatError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    include_blocked = False
    has_tag = ""
    min_children: int | None = None
    for arg in rest:
        if arg == "--all":
            include_blocked = True
        elif arg.startswith("--has-tag="):
            has_tag = arg[len("--has-tag=") :]
        elif arg.startswith("--min-children="):
            raw = arg[len("--min-children=") :]
            if not raw.isdigit():
                print(
                    f"Error: --min-children expects a non-negative integer, got '{raw}'",
                    file=sys.stderr,
                )
                return 2
            min_children = int(raw)
        elif arg in ("--help", "-h"):
            print(
                "Usage: ticket list-epics [--all] [--has-tag=<tag>] [--min-children=<n>] "
                "[--output json]   (DEPRECATED — use 'list --type=epic ...')",
                file=sys.stderr,
            )
            return 0
        else:
            print(f"Error: unknown option '{arg}'", file=sys.stderr)
            return 2
    if not os.path.isdir(tracker):
        print("Error: ticket system not initialized. Run 'ticket init' first.", file=sys.stderr)
        return 1
    epics = list_states(
        tracker,
        ticket_type="epic",
        status="open,in_progress",
        blocking_state="" if include_blocked else "unblocked",
        has_tag=has_tag,
        min_children=min_children,
        with_children_count=True,
    )
    p0_bugs = list_states(tracker, ticket_type="bug", priority="0")
    if fmt == "json":
        print(json.dumps({"p0_bugs": p0_bugs, "epics": epics}, ensure_ascii=False))
    else:
        for e in epics:
            print(
                f"{e.get('alias') or e['ticket_id']}\tP{e.get('priority', '')}\t"
                f"{e.get('title', '')}\t{e.get('children_count', 0)}"
            )
    return 0


def _cmd_next_batch(argv: list[str], tracker: str) -> int:
    """Compute-heavy read (Tier C): the conflict-aware parallel batch selector.
    Delegates to the faithful port; rendering/exit codes live there."""
    from rebar._engine_support import next_batch

    return next_batch.run(argv, tracker)


def _cmd_validate(argv: list[str], tracker: str) -> int:
    """Compute-heavy read (Tier C): repo-wide tracker-health check (NO ticket id;
    score-encoded exit). Self-manages freshness, so ``main`` skips the global
    pre-fetch for it (see ``_NO_PREFETCH``)."""
    from rebar._engine_support import validate

    return validate.run(argv, tracker)


_COMMANDS = {
    "show": _cmd_show,
    "list": _cmd_list,
    "deps": _cmd_deps,
    "ready": _cmd_ready,
    "search": _cmd_search,
    "list-epics": _cmd_list_epics,
    "next-batch": _cmd_next_batch,
    "validate": _cmd_validate,
}

# Subcommands that own their freshness policy (so main does not pre-fetch).
# ``validate`` reads tickets in-process (``validate._raw_tickets`` → ``list_states``)
# and applies freshness there itself.
_NO_PREFETCH = {"validate"}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(
            "Usage: rebar <show|list|deps|ready|search> [args...]",
            file=sys.stderr,
        )
        return 1
    sub, rest = argv[0], argv[1:]
    handler = _COMMANDS.get(sub)
    if handler is None:
        print(f"Error: unknown read subcommand '{sub}'", file=sys.stderr)
        return 1
    # Freshness: strip --no-pull (canonical; --no-sync kept as a deprecated alias)
    # before the subcommand parses its own flags, so all read arms share one policy.
    no_pull = "--no-pull" in rest or "--no-sync" in rest
    rest = [a for a in rest if a not in ("--no-pull", "--no-sync")]
    tracker = tracker_dir()
    if sub not in _NO_PREFETCH:
        ensure_fresh(tracker, no_sync=no_pull)
    return handler(rest, tracker)


if __name__ == "__main__":
    sys.exit(main())
