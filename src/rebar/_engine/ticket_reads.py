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
with the ``REBAR_NO_SYNC=1`` env var or the ``--no-sync`` CLI flag.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from ticket_reducer import (  # noqa: E402
    apply_ticket_filters,
    reduce_all_tickets,
    reduce_ticket,
    search_states,
)
from ticket_reducer._present import public_state  # noqa: E402
from ticket_reducer.llm_format import to_llm  # noqa: E402
from ticket_graph._graph import build_dep_graph  # noqa: E402
from ticket_graph._ready import find_ready_tickets  # noqa: E402
from ticket_resolver import resolve_ticket_id  # noqa: E402
from ticket_output import OutputFormatError, parse_output  # noqa: E402


# ───────────────────────────── tracker resolution ────────────────────────────
def tracker_dir(repo_root: str | os.PathLike[str] | None = None) -> str:
    """Resolve the tracker dir: TICKETS_TRACKER_DIR, else <repo_root>/.tickets-tracker.

    repo_root precedence: explicit arg > PROJECT_ROOT > REBAR_ROOT > git toplevel
    of cwd. Mirrors the shims' resolution so the CLI/library agree.
    """
    env_dir = os.environ.get("TICKETS_TRACKER_DIR")
    if env_dir:
        # Explicit tracker dir — read it directly, no git precondition (test
        # fixtures and tooling point this at a hand-built tracker on purpose).
        return env_dir
    root = (
        str(repo_root)
        if repo_root is not None
        else (os.environ.get("PROJECT_ROOT") or os.environ.get("REBAR_ROOT"))
    )
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
        # A repo-root was supplied (arg / PROJECT_ROOT / REBAR_ROOT). The rebar
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
def _sync_disabled() -> bool:
    val = os.environ.get("REBAR_NO_SYNC", "")
    if val and val != "0":
        return True
    # The test harnesses set _TICKET_TEST_NO_SYNC=1 for temp repos with no remote
    # (the dispatcher's _ensure_initialized honored the same flag); preserve it so
    # moving freshness into the native path does not start wasting I/O in tests.
    if os.environ.get("_TICKET_TEST_NO_SYNC", "") == "1":
        return True
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
    if no_sync or _sync_disabled():
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
        # Delegate the actual fetch/merge to the shared bash helper so the
        # reconvergence policy (the I1-I9 doctrine) has exactly one home.
        sync_sh = str(_SCRIPTS_DIR / "ticket-sync.sh")
        subprocess.run(
            ["bash", "-c", f'source "$1"; _reconverge_tickets "$2"', "_", sync_sh, tracker_abs],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
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
    scratch_base = os.environ.get("SCRATCH_BASE_DIR", "").strip()
    if not scratch_base:
        repo_root = os.path.dirname(os.path.abspath(tracker))
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
) -> list[dict]:
    # detected_by:* tags are bug-only — auto-intersect with --type=bug.
    if has_tag.startswith("detected_by:") and not ticket_type:
        ticket_type = "bug"
    parent_filter = parent
    if parent_filter:
        parent_filter = resolve_ticket_id(parent_filter, tracker) or parent_filter
    results = reduce_all_tickets(
        tracker, exclude_archived=not include_archived, exclude_deleted=exclude_deleted
    )
    results = apply_ticket_filters(
        results,
        type_filter=ticket_type,
        status_filter=status,
        parent_filter=parent_filter,
        tag_filter=has_tag,
        priority_filter=priority,
        without_tag_filter=without_tag,
    )
    return [public_state(t) for t in results]


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
    results = search_states(
        states, query, status=status, ticket_type=ticket_type, has_tag=has_tag
    )
    return [public_state(t) for t in results]


# ───────────────────────────── CLI command handlers ──────────────────────────
def _bridge_alert_warning(states: list[dict]) -> str | None:
    alerted = sum(
        1
        for t in states
        if any(not a.get("resolved", False) for a in t.get("bridge_alerts", []))
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
            return 1
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
                    {
                        "error": "ticket_not_found",
                        "input": raw_id,
                        "message": f"Ticket '{raw_id}' not found",
                    },
                    ensure_ascii=False,
                )
                if "not found" in exc.message
                else json.dumps({"error": "show_failed", "message": exc.message})
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
        "[--has-tag=<tag>] [--without-tag=<tag>]"
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
    }
    for arg in rest:
        if arg == "--include-archived":
            opts["include_archived"] = True
        elif arg == "--exclude-deleted":
            opts["exclude_deleted"] = True
        elif arg.startswith("--type="):
            opts["ticket_type"] = arg[len("--type="):]
        elif arg.startswith("--status="):
            opts["status"] = arg[len("--status="):]
        elif arg.startswith("--parent="):
            opts["parent"] = arg[len("--parent="):]
        elif arg.startswith("--has-tag="):
            opts["has_tag"] = arg[len("--has-tag="):]
        elif arg.startswith("--priority="):
            opts["priority"] = arg[len("--priority="):]
        elif arg.startswith("--without-tag="):
            opts["without_tag"] = arg[len("--without-tag="):]
        elif arg in ("--help", "-h"):
            print(usage, file=sys.stderr)
            return 0
        else:
            print(f"Error: unknown option '{arg}'", file=sys.stderr)
            print(
                "Valid filters: --type --status --priority --parent --has-tag "
                "--without-tag --include-archived --exclude-deleted --output llm",
                file=sys.stderr,
            )
            return 1

    # --priority: integers 0-4 (comma-separated for OR).
    pri = opts["priority"]
    if pri:
        if any(c not in "0123456789," for c in pri):
            print(
                f"Error: --priority expects integer values 0-4 (comma-separated for OR), got '{pri}'",
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
            epic = arg[len("--epic="):]
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
            status = arg[len("--status="):]
        elif arg.startswith("--type="):
            ticket_type = arg[len("--type="):]
        elif arg.startswith("--has-tag="):
            has_tag = arg[len("--has-tag="):]
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


_COMMANDS = {
    "show": _cmd_show,
    "list": _cmd_list,
    "deps": _cmd_deps,
    "ready": _cmd_ready,
    "search": _cmd_search,
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(
            "Usage: ticket-reads.py <show|list|deps|ready|search> [args...]",
            file=sys.stderr,
        )
        return 1
    sub, rest = argv[0], argv[1:]
    handler = _COMMANDS.get(sub)
    if handler is None:
        print(f"Error: unknown read subcommand '{sub}'", file=sys.stderr)
        return 1
    # Freshness: strip --no-sync (an opt-out alongside REBAR_NO_SYNC) before the
    # subcommand parses its own flags, so all read arms share one policy.
    no_sync = "--no-sync" in rest
    rest = [a for a in rest if a != "--no-sync"]
    tracker = tracker_dir()
    ensure_fresh(tracker, no_sync=no_sync)
    return handler(rest, tracker)


if __name__ == "__main__":
    sys.exit(main())
