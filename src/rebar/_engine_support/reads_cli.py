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
from typing import Any

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
from rebar._engine_support.ticket_query import TicketQuery
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


def _coalesce_value_opts(args: list[str], value_opts: frozenset[str]) -> list[str]:
    """Normalize the space form ``--opt value`` into the equals form ``--opt=value``
    for the listed value-taking options, so the read-CLI accepts BOTH forms — matching
    ``--output``/``ready --epic``, the write/composer commands (``claim --assignee
    <you>``), and the documented ``--opt <value>`` convention. Options already in
    equals form, bare flags, and positionals pass through untouched. A ``--opt`` whose
    next token is missing or itself looks like an option (starts with ``-``) is left
    as-is, so the command's own handler still reports the right usage error and a
    ``-``-prefixed value (e.g. the descending ``--sort=-priority``) is passed via the
    equals form rather than ambiguously consuming the following flag."""
    out: list[str] = []
    i, n = 0, len(args)
    while i < n:
        arg = args[i]
        if arg in value_opts and i + 1 < n and not args[i + 1].startswith("-"):
            out.append(f"{arg}={args[i + 1]}")
            i += 2
        else:
            out.append(arg)
            i += 1
    return out


# The full set of ticket statuses that may appear in a compiled ticket's `status`
# field and are therefore valid `--status` filter values: the pre-work `idea` parking
# lot plus the four lifecycle states, archived/deleted tombstones, and the reducer's
# error/fsck_needed sentinels. `idea` is fully listable/searchable even though it is
# carved out of the dispatch surfaces (ready/next-batch) — see graph/_ready.py.
_VALID_LIST_STATUSES = frozenset(
    {
        "idea",
        "open",
        "in_progress",
        "closed",
        "blocked",
        "archived",
        "deleted",
        "error",
        "fsck_needed",
    }
)

_LIST_VALUE_OPTS = frozenset(
    {
        "--sort",
        "--type",
        "--status",
        "--parent",
        "--has-tag",
        "--priority",
        "--without-tag",
        "--min-children",
    }
)
_SEARCH_VALUE_OPTS = frozenset({"--status", "--type", "--has-tag", "--sort"})
_SESSION_LOGS_VALUE_OPTS = frozenset({"--limit"})
_READY_VALUE_OPTS = frozenset({"--epic", "--sort"})


def _cmd_show(argv: list[str], tracker: str) -> int:
    usage = "Usage: ticket show [--output llm] [--include-scratch] <ticket_id> [<ticket_id> ...]"
    try:
        fmt, rest = parse_output(argv, "reader")
    except OutputFormatError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    include_scratch = False
    # ``managed_refs`` is the reducer's strictly-monotonic removal-sync projection
    # (see reducer._managed_refs): it retains every ref the ticket EVER managed, so
    # a link removed via ``unlink`` still appears there. Surfacing it in the default
    # human view makes a removed link read as live and ``unlink`` look broken. So it
    # is stripped from the default view and gated behind ``--include-provenance`` — an
    # internal, intentionally-undocumented flag the Jira reconciler always passes (it
    # reads this dict via ``rebar show`` stdout to drive removal-propagation). Keeping
    # the flag out of ``usage`` keeps it reconciler-only in practice.
    include_provenance = False
    ids: list[str] = []
    for arg in rest:
        if arg == "--include-scratch":
            include_scratch = True
        elif arg == "--include-provenance":
            include_provenance = True
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
            if not include_provenance and fmt != "llm":
                # Hide the monotonic removal-sync projection from the default view so
                # unlinked/removed refs don't read as live links. Scoped to the default
                # view only: the reconciler reads this dict via the default format and
                # passes ``--include-provenance`` to keep the field; the ``llm`` view is
                # left untouched to preserve ``show``/``list`` llm-output parity.
                state.pop("managed_refs", None)
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
            # The `--output llm` arm is a machine format with a show↔list parity contract
            # (test_llm_parity_show_vs_list) — do NOT add the overlap flag here.
            print(json.dumps(to_llm(state), ensure_ascii=False, separators=(",", ":")))
        else:
            # Cross-ticket overlap (epic only-crave-art): render the digest freshness flag in
            # the human `show` output ONLY. The flag is added to the dict THIS arm prints — it
            # never enters the library `show_ticket`/`list`/`search` returns (which share one
            # shape, test_show_list_search_share_one_shape) nor the `--output llm` arm. Lazy
            # import + fail-safe so `rebar show` never breaks on it.
            out = dict(state)
            try:
                from rebar.llm.overlap import digest_sidecar

                out["digest_freshness"] = digest_sidecar.freshness(
                    raw_id, state=state, tracker=tracker
                )
            except Exception:  # noqa: BLE001 — presentation-only; never break `show` on it
                pass
            print(json.dumps(out, indent=2, ensure_ascii=False))
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
        "[--unblocked|--blocked] [--with-children-count] [--full] "
        "[--sort=<priority|created|updated|id|status>] (prefix '-' for descending)"
    )
    try:
        fmt, rest = parse_output(argv, "reader")
    except OutputFormatError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    opts: dict[str, Any] = {
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
        # Lean by default: drop the bulky description/comments bodies. `--full`
        # opts back into the full ticket shape (matching show/search).
        "include_body": False,
    }
    rest = _coalesce_value_opts(rest, _LIST_VALUE_OPTS)
    for arg in rest:
        if arg == "--include-archived":
            opts["include_archived"] = True
        elif arg == "--full":
            opts["include_body"] = True
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
                "--full --sort --include-archived --exclude-deleted --output llm",
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

    # --status: reject unknown values loudly (comma-separated for OR). A mistyped
    # status (e.g. `all`) otherwise falls through to the reducer's set-membership
    # filter, matches nothing, and returns [] with exit 0 — indistinguishable from
    # "no tickets match", which has silently corrupted real measurements
    # (bug spiny-ferry-ripen).
    if opts["status"]:
        bad = [
            s.strip() for s in opts["status"].split(",") if s.strip() not in _VALID_LIST_STATUSES
        ]
        if bad:
            print(
                f"Error: invalid --status value(s) {', '.join(repr(b) for b in bad)}. "
                f"Valid statuses: {', '.join(sorted(_VALID_LIST_STATUSES))}.",
                file=sys.stderr,
            )
            return 2

    if not os.path.isdir(tracker):
        print("Error: ticket system not initialized. Run 'ticket init' first.", file=sys.stderr)
        return 1

    results = list_states(tracker, TicketQuery(**opts))
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
    rest = _coalesce_value_opts(rest, _SESSION_LOGS_VALUE_OPTS)
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
    rest = _coalesce_value_opts(rest, _READY_VALUE_OPTS)
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
    argv = _coalesce_value_opts(argv, _SEARCH_VALUE_OPTS)
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
    # Freshness: strip --no-pull before the subcommand parses its own flags, so all
    # read arms share one freshness policy.
    no_pull = "--no-pull" in rest
    rest = [a for a in rest if a != "--no-pull"]
    tracker = tracker_dir()
    if sub not in _NO_PREFETCH:
        ensure_fresh(tracker, no_sync=no_pull)
    return handler(rest, tracker)


if __name__ == "__main__":
    sys.exit(main())
