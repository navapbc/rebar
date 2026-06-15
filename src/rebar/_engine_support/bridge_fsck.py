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
import os
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
    orphaned: list[dict] = []
    duplicates: list[dict] = []
    stale: list[dict] = []

    # jira_key -> list of ticket_ids that claim it via SYNC events
    jira_key_to_tickets: dict[str, list[str]] = {}

    if now_ts is None:
        now_ts = time.time_ns()

    if not tickets_tracker.is_dir():
        return {"orphaned": orphaned, "duplicates": duplicates, "stale": stale}

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

    return {"orphaned": orphaned, "duplicates": duplicates, "stale": stale}


def enumerate_stale_anomalies(
    tickets_dir: Path,
    now: int | None = None,
) -> list[dict]:
    """Return enriched stale-SYNC anomaly records from the bridge audit.

    Delegates to ``audit_bridge_mappings`` internally and enriches each stale
    record with the fields required by the anomaly contract:

    * ``class_label``          — always ``"stale"``
    * ``proposed_remediation`` — ``"re-sync or close"``

    Each raw stale record from ``audit_bridge_mappings`` has the shape:
    ``{ticket_id, jira_key, last_sync_ts}``.  The enriched records returned
    here carry those same fields plus the two enrichment fields above.

    The existing ``audit_bridge_mappings`` return shape is preserved and its
    CLI behaviour is unaffected.

    Args:
        tickets_dir: Path to the ticket tracker directory.
        now: Optional reference timestamp (UTC epoch nanoseconds) to use as
            'now' for stale-detection calculations. Passed through to
            ``audit_bridge_mappings`` as ``now_ts``. Defaults to
            ``time.time_ns()``.

    Returns:
        A list of dicts, each containing at minimum:
        ``ticket_id``, ``jira_key``, ``last_sync_ts``, ``class_label``,
        ``proposed_remediation``.
    """
    findings = audit_bridge_mappings(tickets_dir, now_ts=now)
    raw_stale = findings.get("stale", [])
    enriched: list[dict] = []
    for record in raw_stale:
        entry = dict(record)
        entry.setdefault("class_label", "stale")
        entry.setdefault("proposed_remediation", "re-sync or close")
        enriched.append(entry)
    return enriched


def enumerate_duplicate_anomalies(tickets_dir: Path) -> list[dict]:
    """Return enriched duplicate-mapping anomaly records from the bridge audit.

    Delegates to ``audit_bridge_mappings`` internally and enriches each
    duplicate record with the fields required by the anomaly contract:

    * ``class_label``          — always ``"duplicate"``
    * ``proposed_remediation`` — ``"close newer duplicates"``
    * ``keeper``               — ``ticket_ids[0]`` (first = oldest by Jira
                                 created_at ordering, preserved by the audit)
    * ``closees``              — ``ticket_ids[1:]`` (all IDs after the keeper)

    Each raw duplicate record from ``audit_bridge_mappings`` has the shape:
    ``{jira_key, ticket_ids: [...]}``.  The enriched records returned here
    carry those same fields plus the four enrichment fields above.

    The existing ``audit_bridge_mappings`` return shape is preserved and its
    CLI behaviour is unaffected.

    Args:
        tickets_dir: Path to the ticket tracker directory.

    Returns:
        A list of dicts, each containing at minimum:
        ``jira_key``, ``ticket_ids``, ``class_label``, ``proposed_remediation``,
        ``keeper``, ``closees``.
    """
    findings = audit_bridge_mappings(tickets_dir)
    raw_duplicates = findings.get("duplicates", [])
    enriched: list[dict] = []
    for record in raw_duplicates:
        entry = dict(record)
        entry.setdefault("class_label", "duplicate")
        entry.setdefault("proposed_remediation", "close newer duplicates")
        ticket_ids = record.get("ticket_ids", [])
        # TODO(bug TBD — F11): docstring claims ticket_ids[0] is the oldest by
        # Jira created_at, but audit_bridge_mappings preserves filesystem
        # iteration order (not created_at). Either re-sort here by oldest
        # CREATE event timestamp, or weaken the docstring claim. Tracked as
        # a follow-up because the fix touches the audit ordering contract,
        # not just band code.
        entry.setdefault("keeper", ticket_ids[0] if ticket_ids else None)
        entry.setdefault("closees", ticket_ids[1:] if len(ticket_ids) > 1 else [])
        enriched.append(entry)
    return enriched


def enumerate_open_count_skew_anomalies(tickets_dir: Path) -> list[dict]:
    """Return open-count skew anomalies between local ticket store and Jira.

    Makes 4 serial AcliClient.search_issues calls (one per type: epic/story/task/bug)
    and walks the local ticket store to count local open items per type.
    Returns list of {type, local_open, jira_open, delta} dicts for non-zero deltas only.

    Args:
        tickets_dir: Path to the ticket tracker directory.

    Returns:
        A list of dicts, each containing: type, local_open, jira_open, delta.
        Only includes types where delta != 0.
    """
    # Lazy acli transport. This helper is NOT on any live path (CLI bridge-fsck
    # uses audit_bridge_mappings only); kept for the anomaly contract / tests. It
    # requires the reconciler importable as the top-level ``rebar_reconciler``.
    from rebar_reconciler import acli as acli_mod

    # F1 fix: AcliClient() with no args raises TypeError — read credentials
    # from JIRA_* env vars. Operators running fsck without these set will
    # get an actionable RuntimeError instead of a cryptic constructor crash.
    _required = ("JIRA_URL", "JIRA_USER", "JIRA_API_TOKEN")
    _missing = [name for name in _required if not os.environ.get(name)]
    if _missing:
        raise RuntimeError(
            f"missing JIRA_* environment variables: {', '.join(_missing)} "
            "(required for enumerate_open_count_skew_anomalies)"
        )
    client = acli_mod.AcliClient(
        jira_url=os.environ["JIRA_URL"],
        user=os.environ["JIRA_USER"],
        api_token=os.environ["JIRA_API_TOKEN"],
        jira_project=os.environ.get("JIRA_PROJECT", "DIG"),
    )

    ticket_types = ["epic", "story", "task", "bug"]
    results = []

    for ttype in ticket_types:
        # Count Jira open issues of this type
        jira_issues = client.search_issues(
            f"project = DIG AND issuetype = {ttype} AND status != Closed"
        )
        jira_open = len(jira_issues) if jira_issues is not None else 0

        # Count local open tickets of this type
        local_open = 0
        if tickets_dir.is_dir():
            for ticket_dir in tickets_dir.iterdir():
                if not ticket_dir.is_dir():
                    continue
                event_files = sorted(ticket_dir.glob("*.json"))
                ticket_type = None
                ticket_status = None
                for ef in event_files:
                    data = _read_json(ef)
                    if data is None:
                        continue
                    if data.get("event_type") == "CREATE":
                        ticket_type = data.get("type", "")
                    elif data.get("event_type") == "STATUS":
                        ticket_status = data.get("status", "")
                if ticket_type == ttype and ticket_status == "open":
                    local_open += 1

        delta = local_open - jira_open
        if delta != 0:
            results.append(
                {
                    "type": ttype,
                    "local_open": local_open,
                    "jira_open": jira_open,
                    "delta": delta,
                }
            )

    return results


def enumerate_orphan_anomalies(tickets_dir: Path) -> list[dict]:
    """Return enriched orphan anomaly records from the bridge audit.

    Calls ``audit_bridge_mappings`` internally and enriches each orphaned
    record with the fields required by the anomaly contract:

    * ``class_label``          — always ``"orphan"``
    * ``side``                 — ``"local-only"`` when a local ticket exists but
                                 has no Jira counterpart (SYNC without CREATE),
                                 ``"jira-only"`` for Jira-sourced entries with
                                 no local ticket.  The current audit model only
                                 produces local-only orphans, so all records
                                 emitted today carry ``"local-only"``.
    * ``proposed_remediation`` — ``"delete orphan mapping"``

    The existing ``audit_bridge_mappings`` return shape is preserved and its
    CLI behaviour is unaffected.

    Args:
        tickets_dir: Path to the ticket tracker directory.

    Returns:
        A list of dicts, each containing at minimum:
        ``ticket_id``, ``jira_key``, ``class_label``, ``side``,
        ``proposed_remediation``.
    """
    findings = audit_bridge_mappings(tickets_dir)
    raw_orphans = findings.get("orphaned", [])
    enriched: list[dict] = []
    for record in raw_orphans:
        entry = dict(record)
        entry.setdefault("class_label", "orphan")
        entry.setdefault("side", "local-only")
        entry.setdefault("proposed_remediation", "delete orphan mapping")
        enriched.append(entry)
    return enriched


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def _format_report(findings: dict) -> str:
    """Format the audit findings as a human-readable report."""
    orphaned = findings.get("orphaned", [])
    duplicates = findings.get("duplicates", [])
    stale = findings.get("stale", [])

    lines: list[str] = ["=== Bridge FSck Report ==="]
    lines.append(f"Orphans: {len(orphaned)}" if orphaned else "Orphans: none found")
    lines.append(f"Duplicates: {len(duplicates)}" if duplicates else "Duplicates: none found")
    lines.append(f"Stale SYNCs: {len(stale)}" if stale else "Stale SYNCs: none found")

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
            "Defaults to TICKETS_TRACKER_DIR env var or "
            "<repo-root>/.tickets-tracker."
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

    # Resolve tracker path: explicit arg > env var > repo root default
    if args.tickets_tracker:
        tracker_path = Path(args.tickets_tracker)
    elif "TICKETS_TRACKER_DIR" in os.environ:
        tracker_path = Path(os.environ["TICKETS_TRACKER_DIR"])
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
        print(json.dumps({k: findings.get(k, []) for k in ("orphaned", "duplicates", "stale")}))
    else:
        print(_format_report(findings))

    has_issues = any(findings.get(k) for k in ("orphaned", "duplicates", "stale"))
    return 1 if has_issues else 0


if __name__ == "__main__":
    sys.exit(main())
