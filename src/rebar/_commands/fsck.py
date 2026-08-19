"""The ``fsck`` COMMAND — argv parsing, scan/repair dispatch, report rendering.

Non-destructive store integrity validator. This module is the command surface; the checks
themselves live in leaves it calls, one way only:

* ``fsck_scan`` — the store walk and the per-ticket validators (checks 1–4, 5);
* ``fsck_tracker_health`` — the tracker-level checks (4.5–4.7, 4.9);
* ``fsck_authorship`` — the per-env authorship tally (4.8);
* ``fsck_repair`` — the mutating ``--repair`` surface.

The filesystem primitives the diagnostics share (``_ticket_dirs``, ``_resolve_tracker_git_dir``)
live in :mod:`rebar._store.gitutil`, not under ``repair`` (ticket b432-c9dc-c1b4-4a45).

Text mode emits tagged lines + a summary; ``--output json`` derives
``{issues:[{kind,ticket_id?,filename?,detail}], fixed[], issue_count}`` from the SAME text via a
regex transform (kept identical so text and JSON never drift). Exit 0 = no issues, 1 = issues
found.

The leaf symbols below are re-imported and re-exported so ``fsck.<symbol>`` attribute access
keeps resolving for the callers that bind to it — ``tracker_maintenance``
(``foreign_store_path_list``) and the fsck tests (``_scan``, ``_check_snapshot``,
``_tracker_sync_status``, ``_foreign_store_paths``, and ``_resolve_tracker_git_dir`` /
``_ticket_dirs``, which several store tests import from here).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys

from rebar import config
from rebar._commands._repair_pause import RepairPauseError, owned_repair_pause
from rebar._commands.fsck_repair import (  # noqa: F401
    _AUTO_RECOVER_ORPHAN_TYPES,
    _HUMAN_TRIAGE_ORPHAN_TYPES,
    _has_retired_create,
    _is_stale_channel_snapshot,
    _reconciler_in_flight,
    _repair_plan,
    _repair_run,
    _repair_ticket,
)
from rebar._commands.fsck_scan import _check_snapshot, _scan  # noqa: F401
from rebar._commands.fsck_tracker_health import (  # noqa: F401
    _branch_mismatch,
    _foreign_store_paths,
    _tracker_health,
    _tracker_sync_status,
    foreign_store_path_list,
)
from rebar._engine_support.output import OutputFormatError, parse_output
from rebar._store import compat
from rebar._store.gitutil import (  # noqa: F401
    _resolve_tracker_git_dir,
    _ticket_dirs,
)

_STRUCTURED_KINDS = {
    "corrupt",
    "corrupt_create",
    "missing_create",
    "snapshot_inconsistent",
    "snapshot_stale_channel",
    "orphan_event",
    "status_fork_resolved",
}

# The dirty-tracker wedge classes (fsck_tracker_health check 4.10). Their lines carry a
# machine-parseable head — ``KIND: <n> path(s): <paths> — <detail>`` — so the JSON items
# carry the per-class count and paths (ticket c925-7669-ded8-43a3).
_TRACKER_DIRTY_KINDS = {
    "tracker_dirty_deletion",
    "tracker_dirty_leftover",
    "tracker_dirty_tmp_event",
}

_DIRTY_HEAD_RE = re.compile(r"^(\d+) path\(s\): (.*)$")


def _dirty_json_fields(item: dict, rest: str) -> bool:
    """Parse a tracker-dirty line body into ``count`` + ``paths`` + ``detail``; False on
    mismatch (the caller then falls back to the unstructured ``detail``-only shape).

    Paths are ``shlex``-encoded by ``_dirty_tracker_lines`` so spaces survive the
    round-trip, and the declared count disambiguates the path-list/blurb boundary: the
    split lands on the first `` — `` where the head parses to exactly ``count`` paths,
    so a blurb — or a quoted path — containing the sequence cannot derail it."""
    m = _DIRTY_HEAD_RE.match(rest)
    if m is None:
        return False
    count, tail = int(m.group(1)), m.group(2)
    idx = tail.find(" — ")
    while idx != -1:
        try:
            paths = shlex.split(tail[:idx])
        except ValueError:
            paths = []
        if len(paths) == count:
            item["count"] = count
            item["paths"] = paths
            item["detail"] = tail[idx + len(" — ") :]
            return True
        idx = tail.find(" — ", idx + 1)
    return False


def _transform_json(text: str, compat_error: dict | None = None) -> str:
    """Derive the ``--output json`` shape from the text output (kept identical so
    text and JSON never drift). Story
    21dd: attach a ``{"kind","detail"}`` ``compat_error`` (incompatible/corrupt store) so
    ``jq -e '.compat_error.kind'`` detects it WITHOUT the read being blocked."""
    issues, fixed = [], []
    for line in text.splitlines():
        if not line.strip():
            continue
        if line.startswith("FIXED:"):
            fixed.append(line[len("FIXED:") :].strip())
            continue
        if line.startswith("fsck complete"):
            continue
        m = re.match(r"^([A-Z_]+):\s*(.*)$", line)
        if not m:
            continue
        kind, rest = m.group(1).lower(), m.group(2)
        item = {"kind": kind}
        if kind in _TRACKER_DIRTY_KINDS and _dirty_json_fields(item, rest):
            issues.append(item)
            continue
        head, sep, detail = rest.partition(" — ")
        if sep and kind in _STRUCTURED_KINDS:
            if "/" in head:
                tid, _, fn = head.partition("/")
                item["ticket_id"], item["filename"] = tid, fn
            else:
                item["ticket_id"] = head
            item["detail"] = detail
        else:
            item["detail"] = rest
        issues.append(item)
    payload: dict = {"issues": issues, "fixed": fixed, "issue_count": len(issues)}
    if compat_error is not None:
        payload["compat_error"] = compat_error
    return json.dumps(payload)


def _run_live_repair(
    tracker: str,
    *,
    limit: int | None,
    repo_root,
    only: str | None,
    include_archived: bool = False,
) -> tuple[list[str], list[str]]:
    """Run both mutating fsck repair phases under one durable pause."""
    with owned_repair_pause("fsck", repo_root, in_flight_probe=_reconciler_in_flight):
        repair_lines, _unresolved = _repair_run(
            tracker,
            dry_run=False,
            limit=limit,
            repo_root=repo_root,
            only=only,
            include_archived=include_archived,
            _pause_owned=True,
        )
        ensure_lines: list[str] = []
        if only is None:
            from rebar._store import ensures as _ensures

            outcomes = _ensures.run_ensures(tracker)
            changed = [outcome.id for outcome in outcomes if outcome.status == "changed"]
            failed = [outcome.id for outcome in outcomes if outcome.status == "failed"]
            ensure_lines.append(
                f"ensures: swept {len(outcomes)} unit(s); "
                f"{len(changed)} changed, {len(failed)} failed"
            )
            ensure_lines += [
                f"  ensure {outcome.id}: {outcome.status} ({outcome.detail})"
                for outcome in outcomes
                if outcome.status != "ok"
            ]
        return repair_lines, ensure_lines


def _repair_cli(
    tracker: str,
    *,
    dry_run: bool,
    limit: int | None,
    repo_root,
    only: str | None,
    no_mutate: bool,
    fmt: str,
    include_archived: bool = False,
) -> int:
    """Drive the repair surface and render its preserved report contract."""
    if no_mutate and not dry_run:
        sys.stderr.write("Error: --repair requires mutation; use --dry-run for a preview\n")
        return 2
    ensure_lines: list[str] = []
    if dry_run:
        repair_lines, _unresolved = _repair_run(
            tracker,
            dry_run=True,
            limit=limit,
            repo_root=repo_root,
            only=only,
            include_archived=include_archived,
        )
    else:
        try:
            repair_lines, ensure_lines = _run_live_repair(
                tracker,
                limit=limit,
                repo_root=repo_root,
                only=only,
                include_archived=include_archived,
            )
        except RepairPauseError as exc:
            if exc.legacy_report_line is not None:
                report = (
                    _transform_json(exc.legacy_report_line)
                    if fmt == "json"
                    else exc.legacy_report_line
                )
                sys.stdout.write(report + "\n")
            else:
                sys.stderr.write(f"{exc.message}\n")
            return exc.returncode
    scan_lines, issue_count = _scan(
        tracker, no_mutate or dry_run, repo_root, include_archived=include_archived
    )
    summary = (
        "fsck complete: no issues found"
        if issue_count == 0
        else f"fsck complete: {issue_count} issues found"
    )
    rc = 0 if issue_count == 0 else 1
    full = "\n".join(repair_lines + ensure_lines + scan_lines + [summary])
    sys.stdout.write((_transform_json(full) if fmt == "json" else full) + "\n")
    return rc


def _missing_tracker_result(tracker: str, fmt: str) -> int | None:
    """Render the existing uninitialized-store diagnostic when needed."""
    if os.path.isdir(tracker):
        return None
    # Dir-mismatch hint: the configured tracker.dir is absent, but a default-named
    # store still exists alongside → tracker.dir was changed without migrating.
    repo_guess = os.path.dirname(os.path.realpath(tracker))
    legacy = os.path.join(repo_guess, ".tickets-tracker")
    mismatch_hint = ""
    if os.path.realpath(legacy) != os.path.realpath(tracker) and os.path.isdir(legacy):
        mismatch_hint = (
            f"\nWARN: configured tracker.dir resolves to {tracker} (absent), but a "
            f"store exists at {legacy} — tracker.dir was changed without migrating."
        )
    if fmt == "json":
        sys.stdout.write(_transform_json(mismatch_hint.strip()) + "\n")
        return 1
    sys.stderr.write(
        f"Error: ticket system not initialized ({tracker} not found).\n"
        f"Run 'ticket init' first.{mismatch_hint}\n"
    )
    return 1


def fsck_cli(argv: list[str], *, repo_root=None, no_mutate: bool = False) -> int:
    # RC2b Option 1: --repair-snapshots opts into rebuilding a stale SNAPSHOT that has
    # a merged-in pre-snapshot orphan (drives the live store to fsck-zero — A3). Strip
    # it before output parsing; it is honored only when mutation is allowed.
    repair_snapshots = "--repair-snapshots" in argv
    include_archived = "--include-archived" in argv
    do_repair = "--repair" in argv
    dry_run = "--dry-run" in argv
    only_args = [a for a in argv if a == "--only" or a.startswith("--only=")]
    only: str | None = None
    if "--only" in only_args:
        sys.stderr.write("Error: --only requires a value\n")
        return 2
    if len(only_args) > 1:
        sys.stderr.write("Error: --only may be specified only once\n")
        return 2
    if only_args:
        if only_args[0] != "--only=stale-channel":
            value = only_args[0].split("=", 1)[1]
            sys.stderr.write(f"Error: unknown --only value '{value}'\n")
            return 2
        if not do_repair:
            sys.stderr.write("Error: --only=stale-channel requires --repair\n")
            return 2
        only = "stale-channel"
    limit: int | None = None
    for a in argv:
        if a.startswith("--limit="):
            try:
                limit = int(a[len("--limit=") :])
            except ValueError:
                sys.stderr.write(f"Error: invalid --limit value in '{a}'\n")
                return 2
    argv = [
        a
        for a in argv
        if a not in ("--repair-snapshots", "--repair", "--dry-run", "--include-archived")
        and not a.startswith("--limit=")
        and a not in only_args
    ]
    try:
        fmt, _rest = parse_output(argv, "report")
    except OutputFormatError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2

    tracker = str(config.tracker_dir(repo_root))
    missing_result = _missing_tracker_result(tracker, fmt)
    if missing_result is not None:
        return missing_result

    # ── A3 remediation (--repair): drive the store to fsck-zero ──────────────
    if do_repair:
        return _repair_cli(
            tracker,
            dry_run=dry_run,
            limit=limit,
            repo_root=repo_root,
            only=only,
            no_mutate=no_mutate,
            fmt=fmt,
            include_archived=include_archived,
        )

    # ``no_mutate`` is passed by the caller (the library's read-only fsck surface),
    # not read from the environment: read paths (list/show via rebar.fsck(report_only=
    # True)) pass no_mutate=True so they never delete the stale lock; the CLI `fsck`
    # always mutates (default False).
    lines, issue_count = _scan(
        tracker,
        no_mutate or dry_run,
        repo_root,
        repair_snapshots=repair_snapshots,
        dry_run=dry_run,
        include_archived=include_archived,
    )
    summary = (
        "fsck complete: no issues found"
        if issue_count == 0
        else f"fsck complete: {issue_count} issues found"
    )
    rc = 0 if issue_count == 0 else 1

    # Story 21dd: the read-only diagnostic surfaces an incompatible/corrupt store as a
    # structured `compat_error` (JSON) + WARNING, WITHOUT blocking (repair is gated via
    # lock.acquire() instead); the exit code is unchanged.
    compat_error = compat.describe_store_compat(tracker)
    if compat_error is not None:
        sys.stderr.write(f"WARNING: {compat_error['detail']}\n")

    if fmt == "json":
        full = "\n".join([*lines, summary])
        sys.stdout.write(_transform_json(full, compat_error) + "\n")
        return rc
    sys.stdout.write("\n".join([*lines, summary]) + "\n")
    return rc
