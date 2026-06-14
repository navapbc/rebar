"""In-process ``bridge-status`` (Tier E E5).

Ports the bash ``ticket-bridge-status.sh`` arm: report the last bridge run from
``<tracker>/.bridge-status.json`` and the count of unresolved ``BRIDGE_ALERT``
events across the store. The ``.bridge-status.json`` producer was retired at the
level-triggered reconciler cutover (epic 3a03), so on post-cutover repos this
exits 1 ("file missing") — the contract operator runbooks rely on.

Byte-parity with the dispatcher arm (verified empirically):

* missing status file → stderr ``No bridge status file found. Has the bridge run
  yet?`` exit 1 (both ``text`` and ``json``).
* bad ``--output`` value → ``Error: <msg>`` (the canonical output-format text),
  exit 1 (the bash ``_resolve_output_format … || exit 1``).
* unknown option / unexpected argument → 2-line ``Error:`` + ``Usage:`` to stderr,
  exit 1.
* ``text`` → the aligned human report; ``json`` → the raw status object with the
  computed ``unresolved_alerts_count`` appended, ``json.dumps(ensure_ascii=False)``
  (default spaced separators, source key order preserved), exit 0.

Pinned by ``tests/interfaces/test_e5_bridge_status.py``.
"""

from __future__ import annotations

import json
import os
import sys

from rebar._engine_support.output import OutputFormatError, parse_output

_USAGE = "Usage: ticket bridge-status [--output json]"


def count_unresolved_alerts(tracker: str) -> int:
    """Count net-unresolved ``BRIDGE_ALERT`` events across every ticket dir.

    A BRIDGE_ALERT raises an alert keyed by its own ``uuid``; a later event with
    ``data.resolved`` truthy clears the alert named by ``resolves_uuid`` /
    ``alert_uuid``. Per ticket, events apply in filename (timestamp) order; the
    residual unresolved count sums across all (non-dot) ticket dirs.
    """
    unresolved = 0
    try:
        entries = sorted(os.scandir(tracker), key=lambda e: e.name)
    except OSError:
        return 0
    for entry in entries:
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        alerts: dict[str, dict] = {}
        try:
            alert_paths = sorted(
                p.path for p in os.scandir(entry.path)
                if p.name.endswith("-BRIDGE_ALERT.json")
            )
        except OSError:
            # Unreadable ticket dir → skip silently (the bash glob did too).
            continue
        for event_path in alert_paths:
            try:
                with open(event_path, encoding="utf-8") as fh:
                    event = json.load(fh)
            except (json.JSONDecodeError, OSError):
                continue
            event_uuid = event.get("uuid", "")
            data = event.get("data", {})
            if data.get("resolved"):
                target_uuid = data.get("resolves_uuid") or data.get("alert_uuid")
                if target_uuid and target_uuid in alerts:
                    alerts[target_uuid]["resolved"] = True
            else:
                alerts[event_uuid] = {"resolved": False}
        unresolved += sum(1 for a in alerts.values() if not a.get("resolved", False))
    return unresolved


def bridge_status_cli(argv: list[str], tracker: str) -> int:
    """``rebar bridge-status [--output json]`` entry."""
    try:
        fmt, rest = parse_output(argv, "report")
    except OutputFormatError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1

    for arg in rest:
        if arg.startswith("-"):
            sys.stderr.write(f"Error: unknown option '{arg}'\n{_USAGE}\n")
        else:
            sys.stderr.write(f"Error: unexpected argument '{arg}'\n{_USAGE}\n")
        return 1

    status_file = os.path.join(tracker, ".bridge-status.json")
    if not os.path.isfile(status_file):
        sys.stderr.write("No bridge status file found. Has the bridge run yet?\n")
        return 1

    with open(status_file, encoding="utf-8") as fh:
        data = json.load(fh)
    unresolved_alerts = count_unresolved_alerts(tracker)

    if fmt == "json":
        data["unresolved_alerts_count"] = unresolved_alerts
        sys.stdout.write(json.dumps(data, ensure_ascii=False) + "\n")
        return 0

    last_run = data.get("last_run_timestamp", "unknown")
    success = data.get("success", False)
    error = data.get("error")
    unresolved_conflicts = data.get("unresolved_conflicts", 0)
    status_str = "success" if success else "failure"

    sys.stdout.write(f"Last run time:          {last_run}\n")
    sys.stdout.write(f"Status:                 {status_str}\n")
    if error:
        sys.stdout.write(f"Error:                  {error}\n")
    sys.stdout.write(f"Unresolved conflicts:   {unresolved_conflicts}\n")
    sys.stdout.write(f"Unresolved BRIDGE_ALERTs: {unresolved_alerts}\n")
    return 0
