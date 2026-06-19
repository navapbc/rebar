"""Bridge-specific fsck audit (in-process; Tier E E6.5a canonical home).

The engine ``ticket-bridge-fsck.py`` is now a thin bootstrap shim re-exporting
this module. Scans .tickets-tracker/ for bridge mapping anomalies:
  - Orphaned jira_key mappings (SYNC event exists but no CREATE event)
  - Duplicate Jira mappings (multiple tickets share the same jira_key)
  - Stale SYNC events (most recent SYNC > 30 days old, no BRIDGE_ALERT activity)
  - Unresolved BRIDGE_ALERT counts

Reached in-process via ``rebar.bridge_fsck()`` and the ``rebar bridge-fsck`` CLI
arm; ``main()`` preserves the dispatcher arm's byte output (text / --output json).

Module interface:
    audit_bridge_mappings(tickets_tracker: Path) -> dict
        Returns a findings dict with keys:
          - 'orphaned': list of {ticket_id, jira_key}
          - 'duplicates': list of {jira_key, ticket_ids}
          - 'stale': list of {ticket_id, jira_key, last_sync_ts}

Exit codes:
    0 — no issues found
    1 — one or more issues found
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_STALE_THRESHOLD_NS = 30 * 24 * 3600 * 1_000_000_000  # 30 days in nanoseconds
_NS_THRESHOLD = 1_000_000_000_000  # timestamps >= this are nanosecond-scale

# ---------------------------------------------------------------------------
# Core audit logic
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict | None:
    """Read a JSON file, returning None on any parse or IO error."""
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _to_ns(ts: int | float) -> int:
    """Normalize a timestamp to nanoseconds, handling legacy seconds-scale values."""
    ts_int = int(ts)
    return ts_int * 1_000_000_000 if ts_int < _NS_THRESHOLD else ts_int


def audit_bridge_mappings(
    tickets_tracker: Path,
    now_ts: int | None = None,
) -> dict:
    """Scan all ticket directories under tickets_tracker for bridge anomalies.

    Args:
        tickets_tracker: Path to the .tickets-tracker directory.
        now_ts: Optional reference timestamp (UTC epoch nanoseconds) to use as
            'now' for stale-detection calculations. Defaults to time.time_ns().
            Pass an explicit value in tests for deterministic results.

    Returns:
        A findings dict with keys:
          - 'orphaned': list of {ticket_id, jira_key}
          - 'duplicates': list of {jira_key, ticket_ids}
          - 'stale': list of {ticket_id, jira_key, last_sync_ts}
    """
    from rebar.reducer._version import is_unknown_newer_type

    orphaned: list[dict] = []
    duplicates: list[dict] = []
    stale: list[dict] = []
    # Forward-compat (P2.3): event types newer than this binary understands. A
    # reconcile host on an old binary would reduce without them and push stale
    # state — surface it here (the operator who runs bridge-fsck is exactly that
    # host). Informational, never a bridge "issue".
    unknown_event_types: set[str] = set()

    # jira_key -> list of ticket_ids that claim it via SYNC events
    jira_key_to_tickets: dict[str, list[str]] = {}

    if now_ts is None:
        now_ts = time.time_ns()

    if not tickets_tracker.is_dir():
        return {
            "orphaned": orphaned,
            "duplicates": duplicates,
            "stale": stale,
            "unknown_event_types": [],
        }

    for ticket_dir in sorted(tickets_tracker.iterdir()):
        if not ticket_dir.is_dir():
            continue

        ticket_id = ticket_dir.name

        # Collect all event files sorted lexicographically (= chronologically)
        event_files = sorted(ticket_dir.glob("*.json"))

        has_create = False
        sync_events: list[dict] = []
        bridge_alert_events: list[dict] = []

        for event_file in event_files:
            data = _read_json(event_file)
            if data is None:
                continue
            event_type = data.get("event_type", "")
            if is_unknown_newer_type(event_type):
                unknown_event_types.add(event_type)
            if event_type == "CREATE":
                has_create = True
            elif event_type == "SYNC":
                sync_events.append(data)
            elif event_type == "BRIDGE_ALERT":
                bridge_alert_events.append(data)

        if not sync_events:
            # No SYNC events in this directory — skip bridge checks
            continue

        # Pick the most recent SYNC event (last in sorted order)
        latest_sync = sync_events[-1]
        jira_key = latest_sync.get("jira_key", "")

        # --- Orphan check: SYNC exists but no CREATE event ---
        if not has_create and jira_key:
            orphaned.append({"ticket_id": ticket_id, "jira_key": jira_key})

        # --- Build jira_key → ticket_ids map for duplicate detection ---
        if jira_key:
            jira_key_to_tickets.setdefault(jira_key, []).append(ticket_id)

        # --- Stale SYNC check ---
        # A SYNC event is stale when:
        #   1. The latest SYNC timestamp is >30 days old.
        #   2. There are no BRIDGE_ALERT events after the latest SYNC.
        latest_sync_ts = latest_sync.get("timestamp", 0)
        if isinstance(latest_sync_ts, (int, float)) and latest_sync_ts > 0:
            # Normalize seconds-scale legacy timestamps to nanoseconds for comparison
            sync_ts_ns = int(latest_sync_ts)
            if sync_ts_ns < _NS_THRESHOLD:
                sync_ts_ns *= 1_000_000_000
            age_ns = now_ts - sync_ts_ns
            if age_ns > _STALE_THRESHOLD_NS:
                # Check for any BRIDGE_ALERT events after the latest SYNC.
                # Normalize alert timestamps to nanoseconds so mixed-precision
                # comparisons (legacy seconds-scale SYNC vs. ns-scale BRIDGE_ALERT)
                # are handled correctly.
                has_post_sync_alert = any(
                    _to_ns(alert.get("timestamp", 0)) > sync_ts_ns for alert in bridge_alert_events
                )
                if not has_post_sync_alert:
                    stale.append(
                        {
                            "ticket_id": ticket_id,
                            "jira_key": jira_key,
                            "last_sync_ts": latest_sync_ts,
                        }
                    )

    # --- Duplicate detection: jira_keys mapped to more than one ticket ---
    for jira_key, ticket_ids in jira_key_to_tickets.items():
        if len(ticket_ids) > 1:
            duplicates.append({"jira_key": jira_key, "ticket_ids": ticket_ids})

    return {
        "orphaned": orphaned,
        "duplicates": duplicates,
        "stale": stale,
        "unknown_event_types": sorted(unknown_event_types),
    }


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def _format_report(findings: dict) -> str:
    """Format the audit findings as a human-readable report."""
    orphaned = findings.get("orphaned", [])
    duplicates = findings.get("duplicates", [])
    stale = findings.get("stale", [])
    unknown_types = findings.get("unknown_event_types", [])

    lines: list[str] = ["=== Bridge FSck Report ==="]
    lines.append(f"Orphans: {len(orphaned)}" if orphaned else "Orphans: none found")
    lines.append(f"Duplicates: {len(duplicates)}" if duplicates else "Duplicates: none found")
    lines.append(f"Stale SYNCs: {len(stale)}" if stale else "Stale SYNCs: none found")
    if unknown_types:
        lines.append(
            "WARN: store contains event types newer than this rebar understands: "
            f"{', '.join(unknown_types)} — upgrade rebar. A reconcile host on an old "
            "binary reduces without them and may push stale state to Jira."
        )

    if orphaned:
        lines.append("")
        lines.append("--- Orphaned Mappings ---")
        for entry in orphaned:
            lines.append(f"  orphan: ticket={entry['ticket_id']} jira_key={entry['jira_key']}")

    if duplicates:
        lines.append("")
        lines.append("--- Duplicate Jira Mappings ---")
        for entry in duplicates:
            ticket_list = ", ".join(entry["ticket_ids"])
            lines.append(f"  duplicate: jira_key={entry['jira_key']} tickets=[{ticket_list}]")

    if stale:
        lines.append("")
        lines.append("--- Stale SYNC Events ---")
        for entry in stale:
            lines.append(
                f"  stale_sync: ticket={entry['ticket_id']}"
                f" jira_key={entry['jira_key']}"
                f" last_sync_ts={entry['last_sync_ts']}"
            )

    if not (orphaned or duplicates or stale):
        lines.append("")
        lines.append("No issues found.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns 0 on clean, 1 on issues."""
    # Canonical --output/-o flag via the single source of truth, then argparse the
    # rest. text -> human report; json -> {orphaned,duplicates,stale}.
    from rebar._engine_support.output import OutputFormatError, parse_output

    raw = list(sys.argv[1:]) if argv is None else list(argv)
    try:
        out_fmt, raw = parse_output(raw, "report")
    except OutputFormatError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    parser = argparse.ArgumentParser(
        description="Audit bridge mappings in the ticket system for anomalies."
    )
    parser.add_argument(
        "--tickets-tracker",
        default=None,
        help=(
            "Path to the .tickets-tracker directory. "
            "Defaults to REBAR_TRACKER_DIR env var (deprecated alias "
            "TICKETS_TRACKER_DIR) or <repo-root>/.tickets-tracker."
        ),
    )
    parser.add_argument(
        "--now-ts",
        type=int,
        default=None,
        help=(
            "Override current timestamp (UTC epoch seconds) for stale detection. "
            "Primarily for testing — omit in production use."
        ),
    )
    args = parser.parse_args(raw)

    # Resolve tracker path: explicit arg > env override > repo root default
    from rebar.config import tracker_dir_override

    _override = tracker_dir_override()
    if args.tickets_tracker:
        tracker_path = Path(args.tickets_tracker)
    elif _override:
        tracker_path = Path(_override)
    else:
        # Fall back to repo root detection
        try:
            import subprocess

            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                check=True,
            )
            repo_root = Path(result.stdout.strip())
        except Exception:
            repo_root = Path.cwd()
        # fsck walks the tracker directly by design.
        tracker_path = repo_root / ".tickets-tracker"  # tickets-boundary-ok

    findings = audit_bridge_mappings(tracker_path, now_ts=args.now_ts)
    if out_fmt == "json":
        print(
            json.dumps(
                {
                    k: findings.get(k, [])
                    for k in ("orphaned", "duplicates", "stale", "unknown_event_types")
                }
            )
        )
    else:
        print(_format_report(findings))

    # unknown_event_types is an informational WARN (upgrade signal), never a bridge
    # "issue" — it must not change the exit code.
    has_issues = any(findings.get(k) for k in ("orphaned", "duplicates", "stale"))
    return 1 if has_issues else 0


if __name__ == "__main__":
    sys.exit(main())
