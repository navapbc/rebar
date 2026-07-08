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
    except Exception:  # noqa: BLE001 — helper fail-open: any parse/IO error yields None (documented contract)
        return None


def _load_classify():
    """Load the pure reconciler classifier (leaf, stdlib-only) by path.

    bridge-fsck is the SECOND consumer of the one classifier (epic 3006-e198,
    child 8de5): the live pass ACTS on Decisions, this offline audit REPORTS
    them — healing the report-only/healing fork. classify.py lives under the
    hyphen-free reconciler package, so it is loaded via spec_from_file_location
    (the established pattern for reaching reconciler leaves from _engine_support).
    """
    import importlib.util
    import sys

    src = Path(__file__).resolve().parent.parent / "_engine" / "rebar_reconciler" / "classify.py"
    name = "rebar_reconciler_classify_fsck"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, src)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Register BEFORE exec: dataclass annotation resolution (Py 3.14) looks the
    # module up in sys.modules while processing @dataclass at import time.
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


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
            "binding_drift": _empty_binding_drift(),
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

    # Binding-level drift (child 8de5): the offline arm of the ONE classifier.
    # Best-effort — a failure here must never break the event-scan checks.
    try:
        binding_drift = audit_binding_drift(tickets_tracker)
    except Exception:  # noqa: BLE001 — binding-drift arm is additive; degrade to empty on any error
        binding_drift = _empty_binding_drift()

    return {
        "orphaned": orphaned,
        "duplicates": duplicates,
        "stale": stale,
        "unknown_event_types": sorted(unknown_event_types),
        "binding_drift": binding_drift,
    }


def _empty_binding_drift() -> dict:
    return {
        "would_terminal": [],
        "local_gone": [],
        "retired_overlap": [],
        "dangling": [],
        "unbound_jira": [],
    }


def audit_binding_drift(
    tickets_tracker: Path,
    local_states: list[dict] | None = None,
    jira_snapshot: dict | None = None,
    use_prev_snapshot: bool = True,
) -> dict:
    """Binding-level drift audit (epic 3006-e198, child 8de5) — the REPORT consumer
    of the ONE convergence classifier (the live pass ACTS on the same Decisions).

    Reads ``.bridge_state/bindings.json`` + ``bindings-retired.json`` READ-ONLY
    (never writes a rebar-id label — L9 audit boundary) and runs ``classify()``
    over every binding and every unbound snapshot key, projecting the Decisions
    into a findings dict:

      * ``would_terminal`` — TERMINAL_TRANSITION: bound + local archived/deleted +
        Jira live (drift class A).
      * ``dangling`` — PROBE_GET: bound key absent from the Jira snapshot (drift
        class C candidate). Per ADR 0028 absence is not *proof* of deletion — a live
        probe confirms — so the report labels it a candidate, and the acting path
        (13eb) still requires a confirmed 404.
      * ``local_gone`` — ALERT: bound but the local ticket is absent from the store.
      * ``unbound_jira`` — ADOPT: a Jira-native issue in the snapshot with no
        binding (drift class B).
      * ``retired_overlap`` — a jira_key present in BOTH the live and retired stores.

    The Jira snapshot is taken from the persisted ``prev_snapshot.json`` artifact
    (no live fetch), so the whole audit is OFFLINE. Without a snapshot (none
    persisted, or ``use_prev_snapshot=False`` and none injected) only the
    local-decidable cells (local-archived ``would_terminal``, ``local_gone``) run.
    ``local_states`` / ``jira_snapshot`` are injectable seams for testing. This is
    the parity ORACLE the epic's convergence heals are validated against.
    """
    drift = _empty_binding_drift()
    bridge_state = tickets_tracker / ".bridge_state"
    store = _read_json(bridge_state / "bindings.json")
    if not isinstance(store, dict):
        # No store (or unreadable) → nothing bindings-level to audit.
        return drift
    bindings = store.get("bindings")
    reverse = store.get("reverse")
    if not isinstance(bindings, dict):
        return drift

    # Local ticket states (INCLUDING archived + deleted — the whole point). No
    # exclusions: an archived/deleted ticket must still be resolved so its binding
    # can be classified TERMINAL. ``local_states`` may be injected for testing.
    if local_states is None:
        from rebar.reducer import reduce_all_tickets

        local_states = reduce_all_tickets(str(tickets_tracker))
    local_by_id: dict[str, dict] = {}
    for state in local_states:
        tid = state.get("ticket_id") or state.get("id")
        if tid:
            local_by_id[tid] = state

    # The Jira side: the persisted snapshot artifact (offline). None ⇒ the
    # snapshot-requiring cells (dangling / unbound_jira) are skipped.
    if jira_snapshot is None and use_prev_snapshot:
        prev = _read_json(bridge_state / "prev_snapshot.json")
        jira_snapshot = prev if isinstance(prev, dict) else None
    have_snapshot = isinstance(jira_snapshot, dict)

    classify_mod = _load_classify()
    LocalState = classify_mod.LocalState
    DecisionKind = classify_mod.DecisionKind
    ObservedJira = classify_mod.ObservedJira
    JiraObservation = classify_mod.JiraObservation

    def _is_retired(key: str) -> bool:
        retired = _read_json(bridge_state / "bindings-retired.json")
        if not isinstance(retired, dict):
            return False
        rmap = retired.get("retired")
        keys = set(rmap) if isinstance(rmap, (dict, list)) else set()
        return key in keys

    for local_id, entry in bindings.items():
        if not isinstance(entry, dict) or entry.get("state") != "confirmed":
            continue
        jira_key = entry.get("jira_key")
        local = local_by_id.get(local_id)
        if not have_snapshot:
            # Local-decidable-only projection (no Jira artifact available).
            lstate = classify_mod.local_state(local)
            if lstate is LocalState.TERMINAL:
                drift["would_terminal"].append({"local_id": local_id, "jira_key": jira_key})
            elif lstate is LocalState.ABSENT:
                drift["local_gone"].append({"local_id": local_id, "jira_key": jira_key})
            continue
        # Full classifier over (local × snapshot × binding).
        assert jira_snapshot is not None  # narrowed by have_snapshot
        if jira_key in jira_snapshot:
            obs = JiraObservation(
                ObservedJira.PRESENT, key=jira_key, fields=jira_snapshot[jira_key]
            )
        else:
            obs = JiraObservation(ObservedJira.ABSENT_IN_WINDOW, key=jira_key)
        decision = classify_mod.classify(local, obs, entry, entry.get("baseline"))
        if decision.kind is DecisionKind.TERMINAL_TRANSITION:
            drift["would_terminal"].append({"local_id": local_id, "jira_key": jira_key})
        elif decision.kind is DecisionKind.PROBE_GET:
            drift["dangling"].append({"local_id": local_id, "jira_key": jira_key})
        elif decision.kind is DecisionKind.ALERT:
            drift["local_gone"].append({"local_id": local_id, "jira_key": jira_key})

    # Unbound Jira-native issues (drift class B) — snapshot keys with no binding.
    if jira_snapshot is not None:
        bound_keys = set(reverse) if isinstance(reverse, dict) else set()
        for key in jira_snapshot:
            if key in bound_keys:
                continue
            obs = JiraObservation(
                ObservedJira.PRESENT, key=key, fields=jira_snapshot[key], retired=_is_retired(key)
            )
            decision = classify_mod.classify(None, obs, None, None)
            if decision.kind is DecisionKind.ADOPT:
                drift["unbound_jira"].append({"jira_key": key})

    # Overlap sanity: a key must not be both a live binding and retired.
    retired = _read_json(bridge_state / "bindings-retired.json")
    if isinstance(retired, dict):
        retired_map = retired.get("retired")
        retired_keys = set(retired_map) if isinstance(retired_map, (dict, list)) else set()
        live_keys = set(reverse) if isinstance(reverse, dict) else set()
        for key in sorted(retired_keys & live_keys):
            drift["retired_overlap"].append({"jira_key": key})

    return drift


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def _format_report(findings: dict) -> str:
    """Format the audit findings as a human-readable report."""
    orphaned = findings.get("orphaned", [])
    duplicates = findings.get("duplicates", [])
    stale = findings.get("stale", [])
    unknown_types = findings.get("unknown_event_types", [])
    binding_drift = findings.get("binding_drift") or {}
    drift_total = sum(len(binding_drift.get(k, [])) for k in _empty_binding_drift())

    lines: list[str] = ["=== Bridge FSck Report ==="]
    lines.append(f"Orphans: {len(orphaned)}" if orphaned else "Orphans: none found")
    lines.append(f"Duplicates: {len(duplicates)}" if duplicates else "Duplicates: none found")
    lines.append(f"Stale SYNCs: {len(stale)}" if stale else "Stale SYNCs: none found")
    lines.append(f"Binding drift: {drift_total}" if drift_total else "Binding drift: none found")
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

    if drift_total:
        lines.append("")
        lines.append("--- Binding-Level Drift ---")
        for entry in binding_drift.get("would_terminal", []):
            lines.append(
                f"  would_terminal: local={entry['local_id']} jira_key={entry['jira_key']}"
                " (local archived/deleted; Jira would be driven to Done)"
            )
        for entry in binding_drift.get("local_gone", []):
            lines.append(
                f"  local_gone: local={entry['local_id']} jira_key={entry['jira_key']}"
                " (bound but local ticket absent from store)"
            )
        for entry in binding_drift.get("retired_overlap", []):
            lines.append(
                f"  retired_overlap: jira_key={entry['jira_key']}"
                " (present in BOTH live and retired stores)"
            )
        for entry in binding_drift.get("dangling", []):
            lines.append(
                f"  dangling: local={entry.get('local_id')} jira_key={entry.get('jira_key')}"
            )
        for entry in binding_drift.get("unbound_jira", []):
            lines.append(f"  unbound_jira: jira_key={entry.get('jira_key')}")

    if not (orphaned or duplicates or stale or drift_total):
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
            "Defaults to the REBAR_TRACKER_DIR env var "
            "or <repo-root>/.tickets-tracker."
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
        except Exception:  # noqa: BLE001 — git rev-parse fallback: an unresolvable repo root defaults to cwd
            repo_root = Path.cwd()
        # fsck walks the tracker directly by design.
        tracker_path = repo_root / ".tickets-tracker"  # tickets-boundary-ok

    findings = audit_bridge_mappings(tracker_path, now_ts=args.now_ts)
    if out_fmt == "json":
        print(
            json.dumps(
                {
                    "orphaned": findings.get("orphaned", []),
                    "duplicates": findings.get("duplicates", []),
                    "stale": findings.get("stale", []),
                    "unknown_event_types": findings.get("unknown_event_types", []),
                    "binding_drift": findings.get("binding_drift", _empty_binding_drift()),
                }
            )
        )
    else:
        print(_format_report(findings))

    # unknown_event_types is an informational WARN (upgrade signal), never a bridge
    # "issue" — it must not change the exit code. binding_drift IS real drift (the
    # class-D blindness this child heals), so it DOES set a non-zero exit.
    binding_drift = findings.get("binding_drift") or {}
    drift_total = sum(len(binding_drift.get(k, [])) for k in _empty_binding_drift())
    has_issues = any(findings.get(k) for k in ("orphaned", "duplicates", "stale")) or drift_total
    return 1 if has_issues else 0


if __name__ == "__main__":
    sys.exit(main())
