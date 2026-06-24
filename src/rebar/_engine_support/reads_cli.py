"""CLI command arms for the read subcommands (show/list/deps/ready/search/...).

Extracted from ``rebar._engine_support.reads`` so the widely-imported ``*_state``
facades stay separate from the argv-facing ``_cmd_*`` dispatch. Imports the facades
from ``reads`` (one direction — no cycle); ``reads.main`` is a thin wrapper that
delegates here for backward compatibility.
"""

from __future__ import annotations

import json
import os
import sys

from rebar._engine_support.output import OutputFormatError, error_envelope, parse_output
from rebar._engine_support.reads import (
    ReadError,
    deps_state,
    ensure_fresh,
    list_states,
    ready_states,
    recent_session_logs_state,
    search_state,
    show_state,
    sort_key_valid,
    tracker_dir,
)
from rebar.reducer.llm_format import to_llm


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
        "[--unblocked|--blocked] [--with-children-count] "
        "[--sort=<priority|created|updated|id|status>] (prefix '-' for descending)"
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
        "sort": "",
    }
    for arg in rest:
        if arg == "--include-archived":
            opts["include_archived"] = True
        elif arg.startswith("--sort="):
            opts["sort"] = arg[len("--sort=") :]
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
                "--sort --include-archived --exclude-deleted --output llm",
                file=sys.stderr,
            )
            # Unrecognized option is a usage error (2), not a runtime error (1) —
            # matching deps/ready/search (the canonical contract in exit-codes.md).
            return 2

    if not sort_key_valid(opts["sort"]):
        print(
            f"Error: --sort expects one of priority|created|updated|id|status "
            f"(optionally '-'-prefixed for descending), got '{opts['sort']}'",
            file=sys.stderr,
        )
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


def _cmd_session_logs(argv: list[str], tracker: str) -> int:
    usage = "Usage: ticket session-logs [--output json|llm] [--limit=<n>]"
    try:
        fmt, rest = parse_output(argv, "reader")
    except OutputFormatError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    limit = 5
    for arg in rest:
        if arg.startswith("--limit="):
            raw = arg[len("--limit=") :]
            if not raw.isdigit() or int(raw) <= 0:
                print(
                    f"Error: --limit expects a positive integer, got '{raw}'",
                    file=sys.stderr,
                )
                return 2
            limit = int(raw)
        elif arg in ("--help", "-h"):
            print(usage, file=sys.stderr)
            return 0
        else:
            print(f"Error: unknown option '{arg}'", file=sys.stderr)
            print(usage, file=sys.stderr)
            return 2

    if not os.path.isdir(tracker):
        print("Error: ticket system not initialized. Run 'ticket init' first.", file=sys.stderr)
        return 1

    results = recent_session_logs_state(tracker, limit=limit)
    if fmt == "llm":
        for t in results:
            print(json.dumps(to_llm(t), ensure_ascii=False, separators=(",", ":")))
    else:
        print(json.dumps(results, ensure_ascii=False))
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
    sort = ""
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
        if arg.startswith("--sort="):
            sort = arg[len("--sort=") :]
            i += 1
            continue
        if arg.startswith("-"):
            # Reject unknown options, including the removed legacy `--json`
            # (use `--output json`). Mirrors the old ready arm's exit 2.
            print(f"Error: unknown option '{arg}'", file=sys.stderr)
            print(
                "Usage: ticket ready [--output json|llm] [--epic <id>] "
                "[--sort=<priority|created|updated|id|status>]",
                file=sys.stderr,
            )
            return 2
        i += 1
    if not sort_key_valid(sort):
        print(
            f"Error: --sort expects one of priority|created|updated|id|status "
            f"(optionally '-'-prefixed for descending), got '{sort}'",
            file=sys.stderr,
        )
        return 2
    states = ready_states(tracker, epic=epic, sort=sort)
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
    sort = ""
    for arg in argv:
        if arg.startswith("--status="):
            status = arg[len("--status=") :]
        elif arg.startswith("--type="):
            ticket_type = arg[len("--type=") :]
        elif arg.startswith("--has-tag="):
            has_tag = arg[len("--has-tag=") :]
        elif arg.startswith("--sort="):
            sort = arg[len("--sort=") :]
        elif arg == "--include-archived":
            include_archived = True
        elif arg.startswith("-"):
            # Reject unknown options (e.g. the removed legacy `--json`) instead
            # of silently ignoring them — matching list/show/ready. The old shim
            # tolerated them; post-collapse there is no reason to.
            print(f"Error: unknown option '{arg}'", file=sys.stderr)
            print(
                "Usage: ticket search <query> [--status=S] [--type=T] "
                "[--has-tag=TAG] [--include-archived] "
                "[--sort=<priority|created|updated|id|status>]",
                file=sys.stderr,
            )
            return 2
        elif query is None:
            query = arg
    if query is None:
        print(
            "Usage: ticket search <query> [--status=S] [--type=T] [--has-tag=TAG] "
            "[--sort=<priority|created|updated|id|status>]",
            file=sys.stderr,
        )
        return 2
    if not sort_key_valid(sort):
        print(
            f"Error: --sort expects one of priority|created|updated|id|status "
            f"(optionally '-'-prefixed for descending), got '{sort}'",
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
        sort=sort,
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
    "session-logs": _cmd_session_logs,
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
