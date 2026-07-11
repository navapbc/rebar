"""In-process ``delete``.

``delete`` is a destructive soft-delete: it requires ``--user-approved``, refuses
when the ticket has non-deleted children, then writes — in ONE atomic commit — an
UNLINK event for every net-active LINK referencing the ticket, a STATUS(deleted)
event, an ARCHIVED event, and a ``.tombstone.json`` marker; afterwards it drops the
``.archived`` filesystem marker, cleans per-ticket scratch, and reports
``newly_unblocked``. Idempotent: a re-invocation on an already-tombstoned ticket
just commits any straggler UNLINKs and exits 0 silently.

Event bytes go through the single canonical serializer
``rebar._store.canonical.canonical_str`` (sorted keys, P1.0). Reuses
``rebar.reducer`` (reduce_all_tickets / write_marker), ``rebar._alias.compute_alias``,
``rebar.graph._unblock`` and the resolver.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

from rebar import config
from rebar._alias import compute_alias, compute_genesis_alias
from rebar._commands import scratch
from rebar._engine_support.output import OutputFormatError, error_envelope, parse_output
from rebar._engine_support.resolver import resolve_ticket_id
from rebar._store import hlc
from rebar._store.canonical import canonical_str
from rebar._store.gitutil import run_git_write
from rebar.graph._unblock import batch_close_operations
from rebar.reducer import reduce_all_tickets
from rebar.reducer.marker import write_marker

logger = logging.getLogger(__name__)


# ── UNLINK scan (port of ticket-delete-unlink-scan.py) ───────────────────────
def _has_any_link_refs(tracker_path: Path, deleted_id: str) -> bool:
    """Conservative fast-path: False only when no LINK/SNAPSHOT anywhere references
    the deleted ticket (so the O(N) reduce can be skipped)."""
    deleted_dir = tracker_path / deleted_id
    if deleted_dir.is_dir():
        for _ in deleted_dir.glob("*-LINK.json"):
            return True
        for snap_path in deleted_dir.glob("*-SNAPSHOT.json"):
            try:
                snap = json.loads(snap_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            data = snap.get("data", {}) or {}
            deps = (
                data.get("compiled_state", {}).get("deps")
                or data.get("deps")
                or snap.get("state", {}).get("deps")
                or []
            )
            if deps:
                return True

    search_terms = [deleted_id]
    # Add both alias formats as extra search terms: the legacy adj-noun-noun
    # (compute_alias, used by pre-v2 tickets) AND the v2 adj-adj-animal
    # (compute_genesis_alias, used by tickets created after the format switch).
    # We can't tell which era the deleted ticket is from without a reduce, so we
    # scan for both; unrelated terms simply don't match.
    for _alias_fn in (compute_alias, compute_genesis_alias):
        try:
            alias = _alias_fn(deleted_id)
        except Exception:  # noqa: BLE001 — alias is an extra search term; fall open to id-only matching
            alias = None
        if alias and alias not in search_terms:
            search_terms.append(alias)
    pattern = "|".join(re.escape(t) for t in search_terms)
    try:
        result = subprocess.run(
            [
                "grep",
                "-rlE",
                pattern,
                str(tracker_path),
                "--include=*-LINK.json",
                "--include=*-SNAPSHOT.json",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    if not result.stdout.strip():
        return False
    deleted_prefix = str(deleted_dir) + os.sep
    for matched in result.stdout.strip().split("\n"):
        if not matched.startswith(deleted_prefix):
            return True
    return False


def _write_unlink(
    source_dir: Path, target_id: str, link_uuid: str, env_id: str, author: str
) -> str | None:
    if not source_dir.is_dir():
        return None
    ts = hlc.next_tick(source_dir.parent, source_dir.name)
    ev_uuid = str(uuid.uuid4())
    event = {
        "event_type": "UNLINK",
        "timestamp": ts,
        "uuid": ev_uuid,
        "env_id": env_id,
        "author": author,
        "data": {"link_uuid": link_uuid, "target_id": target_id},
    }
    dest = source_dir / f"{ts}-{ev_uuid}-UNLINK.json"
    dest.write_text(canonical_str(event), encoding="utf-8")
    return str(dest)


def scan_and_write_unlinks(tracker: str, deleted_id: str, env_id: str, author: str) -> list[str]:
    """Write UNLINK events for every net-active LINK referencing ``deleted_id``;
    return the written file paths (port of ticket-delete-unlink-scan.py)."""
    tracker_path = Path(tracker)
    if not _has_any_link_refs(tracker_path, deleted_id):
        return []
    written: list[str] = []
    for state in reduce_all_tickets(tracker):
        source_id = state.get("ticket_id", "")
        if not source_id:
            continue
        source_dir = tracker_path / source_id
        if source_id == deleted_id:
            for dep in state.get("deps", []):
                link_uuid = dep.get("link_uuid", "")
                target_id = dep.get("target_id", "")
                if link_uuid and target_id:
                    p = _write_unlink(source_dir, target_id, link_uuid, env_id, author)
                    if p:
                        written.append(p)
        else:
            for dep in state.get("deps", []):
                if dep.get("target_id") == deleted_id:
                    link_uuid = dep.get("link_uuid", "")
                    if link_uuid:
                        p = _write_unlink(source_dir, deleted_id, link_uuid, env_id, author)
                        if p:
                            written.append(p)
    return written


# ── delete orchestration ─────────────────────────────────────────────────────
def _git(tracker: str, *args: str):
    """Run a git op in the tracker, raising :class:`CommandError` (exit 2) on
    failure — so a failed DELETE add/commit aborts loudly instead of reporting
    success on an uncommitted store (bash ran under ``set -e``)."""
    from rebar._commands._seam import CommandError

    cp = run_git_write(tracker, *args, check=False)
    if cp.returncode != 0:
        raise CommandError(
            f"Error: git operation failed during delete: {cp.stderr.strip()}", returncode=2
        )
    return cp


def _children(tracker: str, parent_id: str) -> list[str]:
    """Non-deleted children via the EFFECTIVE current parent_id (full event history,
    not just the CREATE event), so a child REPARENTED onto ``parent_id`` via
    ``edit --parent`` is still caught — otherwise a soft delete would orphan it.
    Mirrors the transition open-children guard fix (535bee1), which closed the same
    CREATE-only blind spot. Tombstoned/archived children are already-deleted /
    excluded and don't block (unchanged)."""
    from rebar.reducer import reduce_ticket

    children: list[str] = []
    for entry in sorted(Path(tracker).iterdir()):
        if not entry.is_dir():
            continue
        tid = entry.name
        if tid.startswith(".") or tid == parent_id:
            continue
        if (entry / ".tombstone.json").is_file() or (entry / ".archived").is_file():
            continue
        try:
            state = reduce_ticket(str(entry))
        except Exception:  # noqa: BLE001 — unreadable/corrupt ticket — fsck's job, don't block delete here
            continue
        if state and state.get("status") not in ("error", "fsck_needed"):
            if state.get("parent_id") == parent_id:
                children.append(tid)
    return children


def _write_event(ticket_dir: str, event_type: str, env_id: str, author: str, data: dict) -> str:
    ts = hlc.next_tick(os.path.dirname(ticket_dir), os.path.basename(ticket_dir))
    ev = str(uuid.uuid4())
    event = {
        "timestamp": ts,
        "uuid": ev,
        "event_type": event_type,
        "env_id": env_id,
        "author": author,
        "data": data,
    }
    path = os.path.join(ticket_dir, f"{ts}-{ev}-{event_type}.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write(canonical_str(event))
    return path


def _rel(tracker: str, path: str) -> str:
    prefix = tracker.rstrip("/") + "/"
    return path[len(prefix) :] if path.startswith(prefix) else path


def delete_cli(argv: list[str], *, repo_root=None) -> int:
    """``rebar delete <id> --user-approved`` entry."""
    try:
        fmt, rest = parse_output(argv, "report")
    except OutputFormatError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2

    user_approved = False
    positional: list[str] = []
    for a in rest:
        if a == "--user-approved":
            user_approved = True
        else:
            positional.append(a)

    if len(positional) != 1:
        sys.stderr.write("Usage: ticket delete <ticket_id> --user-approved\n")
        return 1
    raw_id = positional[0]
    if not raw_id:
        sys.stderr.write("Error: ticket_id must be non-empty\n")
        return 1
    if not user_approved:
        sys.stderr.write(
            "Error: ticket delete requires --user-approved flag (this is a destructive operation)\n"
            "Usage: ticket delete <ticket_id> --user-approved\n"
        )
        return 1

    tracker = str(config.tracker_dir(repo_root))
    ticket_id = resolve_ticket_id(raw_id, tracker)
    if ticket_id is None:
        if fmt == "json":
            sys.stdout.write(
                json.dumps(
                    error_envelope("ticket_not_found", raw_id, f"Ticket '{raw_id}' not found", 1)
                )
                + "\n"
            )
        sys.stderr.write(f"Error: ticket '{raw_id}' not found\n")
        return 1

    ticket_dir = os.path.join(tracker, ticket_id)
    already_tombstoned = os.path.isfile(os.path.join(ticket_dir, ".tombstone.json"))

    if not already_tombstoned:
        children = _children(tracker, ticket_id)
        if children:
            sys.stderr.write(
                f"Cannot delete ticket '{ticket_id}': has non-deleted children: "
                + " ".join(children)
                + "\n"
            )
            return 1

    from rebar._commands import _seam
    from rebar._commands._seam import CommandError

    env_id = _seam.env_id(Path(tracker))
    author = _seam.author("Unknown")

    # The atomic write+commit aborts loudly on any git failure (a failed commit must
    # NOT report success and leave the store half-deleted). On failure, roll back the
    # specific files this delete wrote — targeted, since delete holds no write lock
    # (a `git reset --hard` could clobber a concurrent writer's uncommitted work).
    unlink_paths: list[str] = []
    written: list[str] = []
    try:
        unlink_paths = scan_and_write_unlinks(tracker, ticket_id, env_id, author)
        written.extend(p for p in unlink_paths if p)

        if already_tombstoned:
            # Re-invocation: commit any straggler UNLINKs, then exit 0 silently.
            staged = [_rel(tracker, p) for p in unlink_paths if p]
            if staged:
                _git(tracker, "add", *staged)
                _git(
                    tracker,
                    "commit",
                    "-q",
                    "--no-verify",
                    "-m",
                    f"ticket: UNLINK cleanup for already-deleted {ticket_id}",
                )
                from rebar._store import push

                push.push_after_commit(tracker)
            return 0

        status_path = _write_event(ticket_dir, "STATUS", env_id, author, {"status": "deleted"})
        written.append(status_path)
        archived_path = _write_event(ticket_dir, "ARCHIVED", env_id, author, {})
        written.append(archived_path)
        tombstone_path = os.path.join(ticket_dir, ".tombstone.json")
        with open(tombstone_path, "w", encoding="utf-8") as f:
            json.dump({"status": "deleted"}, f, ensure_ascii=False)
        written.append(tombstone_path)

        stage = [_rel(tracker, p) for p in written]
        _git(tracker, "add", *stage)
        _git(tracker, "commit", "-q", "--no-verify", "-m", f"ticket: DELETE {ticket_id}")
    except CommandError as exc:
        # Roll back: unstage + remove every file this (failed) delete wrote, so the
        # store is left exactly as before — no half-deleted, wedged-on-rerun state.
        rels = [_rel(tracker, p) for p in written]
        if rels:
            subprocess.run(
                ["git", "-C", tracker, "reset", "-q", "--", *rels], capture_output=True, text=True
            )
        for p in written:
            try:
                os.remove(p)
            except OSError:
                pass
        sys.stderr.write(exc.message + "\n")
        return exc.returncode

    try:
        write_marker(ticket_dir)
    except Exception:  # noqa: BLE001 — best-effort tombstone marker write; broad-but-logged, delete still proceeds
        logger.warning(
            "could not write tombstone marker for %s; continuing", ticket_id, exc_info=True
        )

    scratch.cleanup_for_ticket(os.path.dirname(tracker), ticket_id)

    batch = batch_close_operations(ticket_ids=[ticket_id], tracker_dir=tracker)
    unblocked = batch["newly_unblocked"]

    if fmt == "json":
        sys.stdout.write(
            json.dumps({"ticket_id": ticket_id, "deleted": True, "newly_unblocked": unblocked})
            + "\n"
        )
    else:
        sys.stdout.write(f"Deleted ticket '{ticket_id}'\n")
        sys.stdout.write(f"UNBLOCKED: {','.join(unblocked) if unblocked else 'none'}\n")

    # The DELETE (STATUS(deleted)+ARCHIVED, plus any UNLINK cascade) committed
    # inline; delete holds no write lock, so push best-effort here (bug
    # prone-octet-cheek) so a delete that isn't followed by an append_event write
    # still reaches origin.
    from rebar._store import push

    push.push_after_commit(tracker)
    return 0
