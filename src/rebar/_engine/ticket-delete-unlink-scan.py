#!/usr/bin/env python3
"""Write UNLINK events for all net-active LINKs referencing a deleted ticket.

Usage:
    python3 ticket-delete-unlink-scan.py <tracker_dir> <deleted_id> <env_id> <author>

Output:
    Prints one absolute path per line for each UNLINK event file written.
    Exits 0 on success.

Replaces the inline heredoc in ticket-lib-api.sh ticket_delete() (bugs
0071-a28d, 3932-5199).  Uses reduce_all_tickets() so SNAPSHOT-compressed
events are respected and the scan is O(N) across tickets.

Fast path (bug 071c-24fe-d4e5-4370): when the deleted ticket has no
outbound LINKs of its own AND no other ticket's LINK/SNAPSHOT files
mention the deleted ID or any of its aliases, skip the O(N)
reduce_all_tickets() pass and emit zero UNLINKs. This is conservative
(false-positive-friendly): any string match falls through to the full
reducer, which correctly handles LINK+UNLINK pairs and SNAPSHOT-compacted
state.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from ticket_reducer import reduce_all_tickets  # noqa: E402
from ticket_reducer._alias import compute_alias  # noqa: E402


def _has_any_link_refs(tracker_path: Path, deleted_id: str) -> bool:
    """Conservative check: return False only when no LINK references can exist.

    Fast-path optimization for the common case where the deleted ticket has
    never been linked from anywhere (e.g., a freshly-created test ticket).

    Returns True if any of these hold:
      - Deleted ticket's own dir has any *-LINK.json file (outbound).
      - Deleted ticket's own SNAPSHOT has non-empty deps (outbound, compacted).
      - Any other ticket's *-LINK.json or *-SNAPSHOT.json file contains the
        deleted ticket's UUID or its computed alias (inbound — by UUID or alias).

    Returns False only when no LINK or SNAPSHOT files anywhere reference the
    deleted ticket. In that case, caller may skip the O(N) reduce_all_tickets()
    pass: there is nothing for it to find.

    The check is conservative: false-positive matches (e.g., a LINK that was
    subsequently UNLINKed) cause fall-through to the full reducer, which
    correctly emits zero UNLINKs for already-unlinked pairs. The fast-path
    only optimizes the obvious-no-refs case.
    """
    deleted_dir = tracker_path / deleted_id

    # ── Outbound check: deleted ticket's own dir ──────────────────────────
    if deleted_dir.is_dir():
        for _ in deleted_dir.glob("*-LINK.json"):
            return True
        # Canonical SNAPSHOT structure: data.compiled_state.deps.
        # Older/test formats may carry deps at state.deps or data.deps — check
        # all three so this guard remains conservative.
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

    # ── Inbound check: any other ticket's events reference deleted_id ─────
    # Search terms: UUID + computed alias (LINK data may reference either form).
    search_terms = [deleted_id]
    try:
        alias = compute_alias(deleted_id)
    except Exception:
        alias = None
    if alias:
        search_terms.append(alias)

    pattern = "|".join(re.escape(t) for t in search_terms)
    # grep over LINK and SNAPSHOT files only. Use -l (list filenames) since we
    # only need a binary yes/no for the optimization.
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
        # grep unavailable or failed — fall through to full reducer (safe).
        return True

    if not result.stdout.strip():
        return False

    # Matches found. Exclude matches inside the deleted ticket's own dir
    # (those are outbound and were already checked above).
    deleted_prefix = str(deleted_dir) + os.sep
    for matched_path in result.stdout.strip().split("\n"):
        if not matched_path.startswith(deleted_prefix):
            return True

    return False


def _write_unlink(
    source_dir: Path, target_id: str, link_uuid_val: str, env_id: str, author: str
) -> str | None:
    """Write an UNLINK event file; return the dest path or None on error."""
    if not source_dir.is_dir():
        return None
    ts = str(time.time_ns())
    ev_uuid = str(uuid.uuid4())
    event = {
        "event_type": "UNLINK",
        "timestamp": int(ts),
        "uuid": ev_uuid,
        "env_id": env_id,
        "author": author,
        "data": {"link_uuid": link_uuid_val, "target_id": target_id},
    }
    dest = source_dir / f"{ts}-{ev_uuid}-UNLINK.json"
    dest.write_text(json.dumps(event, ensure_ascii=False), encoding="utf-8")
    return str(dest)


def main() -> None:
    if len(sys.argv) != 5:
        print(
            "Usage: ticket-delete-unlink-scan.py "
            "<tracker_dir> <deleted_id> <env_id> <author>",
            file=sys.stderr,
        )
        sys.exit(1)

    tracker_dir, raw_deleted_id, env_id, author = sys.argv[1:]
    tracker_path = Path(tracker_dir)

    from ticket_resolver import resolve_ticket_id  # noqa: PLC0415

    deleted_id = resolve_ticket_id(raw_deleted_id, tracker_dir)
    if deleted_id is None:
        print(f"Error: ticket '{raw_deleted_id}' does not exist", file=sys.stderr)
        sys.exit(1)

    # Fast path: skip the O(N) reduce_all_tickets() when no LINK or SNAPSHOT
    # references the deleted ticket anywhere in the tracker.
    if not _has_any_link_refs(tracker_path, deleted_id):
        return

    all_states = reduce_all_tickets(tracker_dir)

    for state in all_states:
        source_id = state.get("ticket_id", "")
        if not source_id:
            continue
        source_dir = tracker_path / source_id

        if source_id == deleted_id:
            # Outbound links: write UNLINK in deleted ticket's own dir
            for dep in state.get("deps", []):
                link_uuid_val = dep.get("link_uuid", "")
                target_id = dep.get("target_id", "")
                if link_uuid_val and target_id:
                    path = _write_unlink(
                        source_dir, target_id, link_uuid_val, env_id, author
                    )
                    if path:
                        print(path)
        else:
            # Inbound links: other tickets pointing at deleted_id
            for dep in state.get("deps", []):
                if dep.get("target_id") == deleted_id:
                    link_uuid_val = dep.get("link_uuid", "")
                    if link_uuid_val:
                        path = _write_unlink(
                            source_dir, deleted_id, link_uuid_val, env_id, author
                        )
                        if path:
                            print(path)


if __name__ == "__main__":
    main()
