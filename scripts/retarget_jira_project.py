#!/usr/bin/env python3
"""Re-target a rebar ticket store from one Jira project to another (bug 626d).

Changing ``[jira] project`` only governs where NEW (unbound) tickets are created.
A store that previously synced to another project keeps that project's **bindings**
(``.bridge_state/bindings.json``) and a stale remote **snapshot**
(``.bridge_state/prev_snapshot.json``), so the reconciler keeps targeting the old
project's issues. Since bug 626d the outbound applier *refuses* such cross-project
writes (fail-closed) — so to actually sync to the new project you must clear that
legacy bind-state and let every local ticket re-create fresh in the new project.

This tool clears the bridge bind-state (and, with ``--strip-tags``, the residual
``dso-id:jira-<old>-*`` id tags). It is **dry-run by default** — it reports what it
would change and writes nothing until you pass ``--apply``.

Validated on a clone of the store (2026-06-25): clearing bindings + prev_snapshot
dropped a dry-run plan from 1415 mutations (1017 targeting the old project) to 398
clean outbound creates with **0** old-project targets.

Usage:
    # report only (no writes):
    python scripts/retarget_jira_project.py --tracker-dir .tickets-tracker

    # apply (clears bind-state); add --strip-tags to also remove dso-id:jira-* tags:
    python scripts/retarget_jira_project.py --tracker-dir .tickets-tracker --apply

After applying, run `rebar reconcile --mode dry-run` and confirm 0 mutations target
the old project before enabling live sync. The bind-state files are backed up to a
sibling ``.bridge_state.bak-<label>`` directory unless --no-backup is given.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

_EMPTY_BINDINGS = {"version": 1, "bindings": {}, "reverse": {}}
# A residual id tag baked onto local tickets by a prior sync, e.g.
# "dso-id:jira-dig-5673"; we strip any dso-id:jira-<proj>-<n> tag.
_ID_TAG_PATTERN = r'"(dso-id:jira-[a-z]+-\d+)"'


def _load_bindings(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _scan_id_tags(tracker_dir: Path) -> dict[str, list[str]]:
    """Return {ticket_id: [stale id tags]} by replaying each ticket's tag state.

    Cheap heuristic: scan event JSON for tag strings matching the id-tag pattern.
    Used only to report/■strip; the authoritative removal goes through `rebar untag`.
    """
    found: dict[str, set[str]] = {}
    for ticket_dir in tracker_dir.iterdir():
        if not ticket_dir.is_dir() or ticket_dir.name.startswith("."):
            continue
        for event in ticket_dir.glob("*.json"):
            try:
                text = event.read_text()
            except OSError:
                continue
            # Collect quoted id-tag tokens (robust to event nesting).
            for m in re.finditer(_ID_TAG_PATTERN, text, re.IGNORECASE):
                found.setdefault(ticket_dir.name, set()).add(m.group(1))
    return {k: sorted(v) for k, v in found.items()}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--tracker-dir", default=".tickets-tracker", help="path to the ticket store worktree"
    )
    ap.add_argument(
        "--apply", action="store_true", help="actually write changes (default: dry-run report)"
    )
    ap.add_argument(
        "--strip-tags",
        action="store_true",
        help="also remove residual dso-id:jira-* id tags via `rebar untag`",
    )
    ap.add_argument(
        "--no-backup", action="store_true", help="skip backing up .bridge_state before clearing"
    )
    ap.add_argument(
        "--backup-label", default="retarget", help="suffix for the .bridge_state backup dir"
    )
    args = ap.parse_args(argv)

    tracker = Path(args.tracker_dir).resolve()
    bs = tracker / ".bridge_state"
    if not tracker.is_dir():
        print(f"ERROR: tracker dir not found: {tracker}", file=sys.stderr)
        return 1

    bindings_path = bs / "bindings.json"
    retired_path = bs / "bindings-retired.json"
    prev_path = bs / "prev_snapshot.json"

    binds = _load_bindings(bindings_path).get("bindings", {})
    id_tags = _scan_id_tags(tracker)
    tag_count = sum(len(v) for v in id_tags.values())

    print(f"Tracker:        {tracker}")
    print(f"Bindings:       {len(binds)} (-> cleared)")
    print(f"prev_snapshot:  {'present -> cleared' if prev_path.exists() else 'absent'}")
    print(f"retired binds:  {'present -> removed' if retired_path.exists() else 'absent'}")
    print(
        f"id tags:        {tag_count} across {len(id_tags)} tickets"
        f"{' (-> stripped)' if args.strip_tags else ' (use --strip-tags to remove)'}"
    )

    if not args.apply:
        print("\nDRY RUN — no changes written. Re-run with --apply to clear bind-state.")
        return 0

    if not args.no_backup and bs.is_dir():
        backup = bs.with_name(f".bridge_state.bak-{args.backup_label}")
        if backup.exists():
            shutil.rmtree(backup)
        shutil.copytree(bs, backup)
        print(f"\nBacked up bind-state -> {backup}")

    bs.mkdir(parents=True, exist_ok=True)
    bindings_path.write_text(json.dumps(_EMPTY_BINDINGS, indent=2))
    prev_path.write_text("{}")
    if retired_path.exists():
        retired_path.unlink()
    print("Cleared bindings.json, prev_snapshot.json, bindings-retired.json.")

    if args.strip_tags and tag_count:
        print("Stripping residual id tags via `rebar untag`...")
        stripped = 0
        for ticket_id, tags in id_tags.items():
            for tag in tags:
                rc = subprocess.call(["rebar", "untag", ticket_id, tag])
                if rc == 0:
                    stripped += 1
                else:
                    print(f"  WARN: untag {ticket_id} {tag} exited {rc}", file=sys.stderr)
        print(f"Stripped {stripped}/{tag_count} id tags.")

    print(
        "\nDone. Now run `rebar reconcile --mode dry-run` and confirm 0 mutations "
        "target the old project before enabling live sync."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
