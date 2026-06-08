#!/usr/bin/env python3
"""CLI for dso-reconciler health records."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path


def _load_health():
    """Load the sibling health.py module so we share count_open_by_type's
    canonical event-shape reader instead of re-implementing it (and
    re-introducing the wrong-path / closed-tickets-counted bugs)."""
    health_path = Path(__file__).parent / "health.py"
    spec = importlib.util.spec_from_file_location("health", health_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load health module from {health_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _count_open_tickets(tickets_dir: Path) -> int:
    """Count OPEN tickets only (not closed/deleted) using the canonical
    reducer event shape.

    Delegates to health.count_open_by_type which walks each ticket dir and
    counts only those whose latest STATUS event has status="open" (or no
    STATUS event yet, matching the reducer initial state). Returns 0 when
    *tickets_dir* does not exist.
    """
    if not tickets_dir.is_dir():
        return 0
    # health.count_open_by_type takes repo_root and joins .tickets-tracker
    # internally; here we already have the tracker_path, so call the
    # function with repo_root=tickets_dir.parent if tickets_dir is named
    # .tickets-tracker, otherwise build a temp root that points at it.
    health = _load_health()
    if tickets_dir.name == ".tickets-tracker":
        return sum(health.count_open_by_type(repo_root=tickets_dir.parent).values())
    # Caller pointed at a non-standard path — wrap it so health treats it
    # as the canonical layout under a synthetic root.
    return sum(
        v
        for _t, v in _count_by_walking_dir(tickets_dir).items()
    )


def _count_by_walking_dir(tickets_dir: Path) -> dict:
    """Fallback walker that mirrors health.count_open_by_type semantics
    against an arbitrary tickets directory path (used when the caller
    passes a non-standard --tickets-dir)."""
    counts: dict[str, int] = {}
    if not tickets_dir.is_dir():
        return counts
    for ticket_dir in tickets_dir.iterdir():
        if not ticket_dir.is_dir():
            continue
        # Skip .scratch/ — scratch-space entries are not ticket directories.
        if '.scratch' in ticket_dir.parts:
            continue
        event_files = sorted(ticket_dir.glob("*.json"))
        ticket_type: str | None = None
        latest_status: str = "open"
        for ef in event_files:
            try:
                event = json.loads(ef.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(event, dict):
                continue
            data = event.get("data", {})
            evt = event.get("event_type", "")
            if evt == "CREATE":
                ticket_type = data.get("ticket_type", "") or ""
            elif evt == "STATUS":
                latest_status = data.get("status", "") or ""
        if ticket_type and latest_status == "open":
            counts[ticket_type] = counts.get(ticket_type, 0) + 1
    return counts


def cmd_summary(args: argparse.Namespace) -> int:
    health_dir = Path(args.health_dir)
    if not health_dir.is_dir():
        print("No health records found.")
        return 0
    records = []
    for f in sorted(health_dir.glob("*.json")):
        try:
            records.append(json.loads(f.read_text()))
        except Exception:  # noqa: BLE001
            continue
    if not records:
        print("No health records found.")
        return 0
    # Print table
    header = f"{'pass_id':<30} {'pre_fsck':>8} {'post_fsck':>9} {'ratio':>6} {'mutations':>9}"
    print(header)
    print("-" * len(header))
    for r in records:
        # Coerce null/missing values to 0 so a producer that writes
        # {"pre_pass_fsck_total": null, ...} does not crash the `> 0` check.
        pre = r.get("pre_pass_fsck_total") or 0
        post = r.get("post_pass_fsck_total") or 0
        ratio = f"{post / pre:.2f}" if pre > 0 else "N/A"
        mutations = r.get("local_mutation_count_at_pass") or 0
        print(
            f"{r.get('pass_id', '?'):<30} {pre:>8} {post:>9} {ratio:>6} {mutations:>9}"
        )
    # Parity check: compare latest health record totals against live ticket store
    if getattr(args, "parity_check", False) and records:
        # Sort by the in-JSON timestamp_ns so 'latest' is chronological, not
        # lexicographic (filename sort breaks for non-zero-padded numeric
        # pass IDs like pass-1, pass-2, ..., pass-10).
        records_by_time = sorted(records, key=lambda r: r.get("timestamp_ns") or 0)
        latest = records_by_time[-1]
        health_total = sum(latest.get("per_type_open_counts", {}).values())
        tickets_dir = Path(getattr(args, "tickets_dir", ".tickets-tracker"))
        live_total = _count_open_tickets(tickets_dir)
        tolerance = max(1, int(health_total * 0.05))
        if abs(health_total - live_total) <= tolerance:
            print("PARITY: PASS")
        else:
            delta = live_total - health_total
            print(
                f"PARITY: DRIFT — health_total={health_total} live_total={live_total} delta={delta}"
            )
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="dso-reconciler health CLI")
    parser.add_argument(
        "--health-dir",
        default="bridge_state/health",
        help="Path to health records directory",
    )
    sub = parser.add_subparsers(dest="command")
    summary_p = sub.add_parser("summary", help="Show summary of health records")
    summary_p.add_argument(
        "--parity-check",
        action="store_true",
        help="Compare latest health record against live ticket counts",
    )
    summary_p.add_argument(
        "--tickets-dir",
        default=".tickets-tracker",
        help="Path to ticket store for parity check (default: .tickets-tracker)",
    )
    args = parser.parse_args(argv)
    if args.command == "summary":
        return cmd_summary(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
