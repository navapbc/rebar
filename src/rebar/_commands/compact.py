"""In-process ``compact`` / ``compact-all``.

Compaction squashes a ticket's event log into ONE SNAPSHOT event under the unified
write lock: re-list events inside the lock, partition out forward-compat
unknown-type events (never absorbed/deleted), re-check the threshold, reduce the
current state, write the SNAPSHOT, delete the originals, invalidate the reducer
cache, and ``git add -A`` + commit atomically.

Reuses ``rebar._store.lock`` (the fcntl+mkdir dual-leg lock),
``rebar.reducer.reduce_ticket`` (in-process), and ``event_append.event_filename``.
SNAPSHOT bytes use ``json.dump(ensure_ascii=False)`` (unsorted).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from rebar import config
from rebar._commands import _seam
from rebar._engine_support.resolver import resolve_ticket_id
from rebar._store import event_append, lock
from rebar.reducer import KNOWN_EVENT_TYPES, reduce_ticket


def _usage() -> int:
    sys.stderr.write(
        "Usage: ticket-compact.sh <ticket_id> [--threshold=N]\n"
        "  Default threshold: REBAR_COMPACT_THRESHOLD env / compact.threshold config or 10\n"
    )
    return 1


def _git(tracker: str, *args: str):
    return subprocess.run(["git", "-C", tracker, *args], capture_output=True, text=True)


def _sync_before_compact(tracker: str) -> None:
    """Pull the latest tickets before compacting (best-effort, in-process) so a
    remote SNAPSHOT written by another agent is visible and local compaction can
    defer to it. Honors the ``sync.pull`` policy and is fully best-effort (every
    fetch failure is swallowed). Replaces the former dead ``ticket sync`` shell-out
    (no such subcommand existed; ``shell=True`` injection smell)."""
    from rebar._engine_support import reads

    reads.ensure_fresh(tracker)


def _compact_locked(
    tracker: str, ticket_id: str, ticket_dir: str, threshold: int, no_commit: bool
) -> int:
    """The locked compaction critical section. Returns 0 on success (prints
    EVENT_COUNT + the compacted line), 0 on below-threshold-inside-lock (prints the
    skip line), 1 on lock timeout / reducer / state / git failure."""
    _git(tracker, "config", "gc.auto", "0")
    try:
        handle = lock.acquire(tracker, timeout=30, attempts=2, dual_window=True)
    except lock.LockTimeout as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    try:
        # Re-list event files inside the lock (authoritative). Exclude -SYNC.json
        # (bridge metadata that must survive compaction).
        candidates = sorted(
            os.path.join(ticket_dir, f)
            for f in os.listdir(ticket_dir)
            if f.endswith(".json") and not f.startswith(".") and not f.endswith("-SYNC.json")
        )
        # Forward-compat: unknown-type events (written by a newer clone) are
        # preserved untouched — never snapshotted or deleted.
        event_files = []
        for fp in candidates:
            try:
                with open(fp, encoding="utf-8") as f:
                    etype = json.load(f).get("event_type", "")
            except (json.JSONDecodeError, OSError):
                etype = ""
            if etype and etype not in KNOWN_EVENT_TYPES:
                continue
            event_files.append(fp)
        event_count = len(event_files)

        if event_count <= threshold:
            sys.stdout.write("below threshold (re-checked inside flock) — skipping compaction\n")
            return 0

        compiled_state = reduce_ticket(ticket_dir)
        if compiled_state is None:
            sys.stderr.write(
                f"Error: reducer failed for ticket {ticket_id} (corrupt or ghost ticket)\n"
            )
            return 1
        status = compiled_state.get("status", "")
        if status in ("error", "fsck_needed"):
            sys.stderr.write(f"Error: ticket {ticket_id} has status '{status}' — cannot compact\n")
            return 1

        source_uuids = []
        for fp in event_files:
            try:
                with open(fp, encoding="utf-8") as f:
                    source_uuids.append(json.load(f).get("uuid", os.path.basename(fp)))
            except (json.JSONDecodeError, OSError):
                source_uuids.append(os.path.basename(fp))

        env_id = _seam.env_id(Path(tracker))
        author = _git_author()

        snapshot_uuid = str(uuid.uuid4())
        snapshot_ts = time.time_ns()
        snapshot_event = {
            "event_type": "SNAPSHOT",
            "timestamp": snapshot_ts,
            "uuid": snapshot_uuid,
            "env_id": env_id,
            "author": author,
            "data": {
                "compiled_state": compiled_state,
                "source_event_uuids": source_uuids,
                "compacted_at": snapshot_ts,
            },
        }
        final_path = os.path.join(
            ticket_dir, event_append.event_filename(snapshot_ts, snapshot_uuid, "SNAPSHOT")
        )
        staging = final_path + ".tmp"
        with open(staging, "w", encoding="utf-8") as f:
            json.dump(snapshot_event, f, ensure_ascii=False)
        os.rename(staging, final_path)

        for fp in event_files:
            try:
                os.remove(fp)
            except OSError:
                pass
        try:
            os.remove(os.path.join(ticket_dir, ".cache.json"))
        except OSError:
            pass

        if not no_commit:
            add = _git(tracker, "add", "-A", f"{ticket_id}/")
            if add.returncode != 0:
                sys.stderr.write("Error: git operation failed while holding lock\n")
                return 1
            staged = _git(tracker, "diff", "--cached", "--quiet")
            if staged.returncode != 0:
                commit = _git(
                    tracker, "commit", "-q", "--no-verify", "-m", f"ticket: COMPACT {ticket_id}"
                )
                if commit.returncode != 0:
                    sys.stderr.write("Error: git operation failed while holding lock\n")
                    return 1

        sys.stdout.write(f"EVENT_COUNT={event_count}\n")
        sys.stdout.write(f"compacted events into SNAPSHOT for {ticket_id}\n")
        return 0
    finally:
        handle.release()


def _git_author() -> str:
    cp = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True)
    if cp.returncode != 0:
        return "system"
    return cp.stdout.strip()


def compact_cli(argv: list[str], *, repo_root=None) -> int:
    """``rebar compact <id>`` entry."""
    if len(argv) < 1:
        return _usage()
    tracker = str(config.tracker_dir(repo_root))
    raw = argv[0]
    ticket_id = resolve_ticket_id(raw, tracker)
    if ticket_id is None:
        sys.stderr.write(f"Error: ticket '{raw}' not found\n")
        return 1

    # Default threshold from the typed config (compact.threshold; env
    # REBAR_COMPACT_THRESHOLD, deprecated alias COMPACT_THRESHOLD, or a config file).
    # A --threshold= flag below still overrides.
    threshold = config.load_config(repo_root).compact.threshold
    skip_sync = False
    no_commit = False
    for a in argv[1:]:
        if a.startswith("--threshold="):
            threshold = int(a[len("--threshold=") :])
        elif a == "--skip-sync":
            skip_sync = True
        elif a == "--no-commit":
            no_commit = True
        else:
            sys.stderr.write(f"Error: unknown argument '{a}'\n")
            return _usage()

    if not (
        os.path.isdir(tracker)
        and (
            os.path.isfile(os.path.join(tracker, ".git"))
            or os.path.isdir(os.path.join(tracker, ".git"))
        )
    ):
        sys.stderr.write("Error: ticket system not initialized. Run 'ticket init' first.\n")
        return 1
    ticket_dir = os.path.join(tracker, ticket_id)
    if not os.path.isdir(ticket_dir):
        sys.stderr.write(f"Error: ticket directory not found: {ticket_dir}\n")
        return 1

    if not skip_sync:
        _sync_before_compact(tracker)
        if any(
            f.endswith("-SNAPSHOT.json") and not f.startswith(".") for f in os.listdir(ticket_dir)
        ):
            sys.stdout.write(f"skipping compaction for {ticket_id} — remote SNAPSHOT exists\n")
            return 0

    preflock = sum(
        1 for f in os.listdir(ticket_dir) if f.endswith(".json") and not f.startswith(".")
    )
    if preflock <= threshold:
        sys.stdout.write(f"below threshold ({preflock} <= {threshold}) — skipping compaction\n")
        return 0

    return _compact_locked(tracker, ticket_id, ticket_dir, threshold, no_commit)


# ── compact-all ──────────────────────────────────────────────────────────────
def _scan_snapshot_state(tracker: str) -> tuple[list[str], int]:
    """Return (ticket ids lacking a SNAPSHOT, count already having one), scanning
    ticket dirs (those with at least one event JSON), sorted by name."""
    needs: list[str] = []
    already = 0
    try:
        entries = sorted(os.scandir(tracker), key=lambda e: e.name)
    except OSError:
        return [], 0
    for entry in entries:
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        names = os.listdir(entry.path)
        if not any(n.endswith(".json") for n in names):
            continue
        if any(n.endswith("-SNAPSHOT.json") for n in names):
            already += 1
        else:
            needs.append(entry.name)
    return needs, already


def compact_all_cli(argv: list[str], *, repo_root=None) -> int:
    """``rebar compact-all`` entry — backfill SNAPSHOTs for tickets lacking one."""
    import contextlib
    import io

    dry_run = False
    limit = 0
    no_commit = False
    for a in argv:
        if a == "--dry-run":
            dry_run = True
        elif a.startswith("--limit="):
            limit = int(a[len("--limit=") :])
        elif a == "--no-commit":
            no_commit = True
        elif a in ("--help", "-h"):
            sys.stdout.write("Usage: ticket compact-all [--dry-run] [--limit=N] [--no-commit]\n")
            return 0
        else:
            sys.stderr.write(f"Error: unknown option '{a}'\n")
            return 1

    tracker = str(config.tracker_dir(repo_root))
    if not os.path.isdir(tracker):
        sys.stderr.write(f"Error: tracker dir not found: {tracker}\n")
        return 1

    needs, already = _scan_snapshot_state(tracker)
    total_needs = len(needs)
    sys.stdout.write(f"Tickets already with SNAPSHOT : {already}\n")
    sys.stdout.write(f"Tickets needing compaction     : {total_needs}\n")
    if total_needs == 0:
        sys.stdout.write("Nothing to do.\n")
        return 0

    if dry_run:
        sys.stdout.write("\nDry-run — would compact:\n")
        for tid in needs:
            sys.stdout.write(f"  {tid}\n")
        return 0

    if limit > 0 and total_needs > limit:
        sys.stdout.write(f"Applying --limit={limit} (will stop after {limit} tickets).\n")
        needs = needs[:limit]
        total_needs = limit

    compacted = 0
    error_ids: list[str] = []
    sys.stdout.write(f"\nCompacting {total_needs} tickets...\n")
    sys.stdout.write("(each dot = 1 ticket; E = error)\n")
    for tid in needs:
        with contextlib.redirect_stderr(io.StringIO()):  # bash 2>/dev/null
            rc = compact_cli(
                [tid, "--threshold=0", "--skip-sync", "--no-commit"], repo_root=repo_root
            )
        if rc == 0:
            compacted += 1
            sys.stdout.write(".")
        else:
            error_ids.append(tid)
            sys.stdout.write("E")
        sys.stdout.flush()

    sys.stdout.write("\n\n")
    sys.stdout.write(
        f"Done: {compacted} compacted, {len(error_ids)} errors (of {total_needs} attempted)\n"
    )
    if error_ids:
        sys.stderr.write("Errored tickets:\n")
        for tid in error_ids:
            sys.stderr.write(f"  {tid}\n")

    if compacted > 0 and not no_commit:
        sys.stdout.write(f"Staging and committing {compacted} new SNAPSHOT files...\n")
        _git(tracker, "add", "-A")
        if _git(tracker, "diff", "--cached", "--quiet").returncode == 0:
            sys.stdout.write("No staged changes (SNAPSHOTs may already have been committed).\n")
        else:
            _git(
                tracker,
                "commit",
                "-q",
                "--no-verify",
                "-m",
                f"chore: backfill SNAPSHOT files for {compacted} tickets (ticket-compact-all)",
            )
            sys.stdout.write("Committed.\n")

    return 2 if error_ids else 0
