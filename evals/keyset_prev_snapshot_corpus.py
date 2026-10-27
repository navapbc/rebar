#!/usr/bin/env python3
"""Compare full and key-set prev snapshots over real tickets-branch history.

This is the reproducible G1/rollback artifact for ticket 2cc0-da4a-d736-4ba2.
It reads historical ``.bridge_state/prev_snapshot.json`` blobs directly from git,
runs the production snapshot differ and its call-site suppression, and compares the
observable mutation stream in both directions.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
ENGINE_ROOT = REPO_ROOT / "src" / "rebar" / "_engine"
SNAPSHOT_PATH = ".bridge_state/prev_snapshot.json"
sys.path.insert(0, str(ENGINE_ROOT))

from rebar_reconciler.differ import compute_mutations  # noqa: E402
from rebar_reconciler.reconcile_helpers import (  # noqa: E402
    drop_snapshot_differ_local_state_emissions,
)


def _git(*args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        check=True,
        stdout=subprocess.PIPE,
    ).stdout


def _full_snapshot_revisions(ref: str, needed: int) -> list[tuple[str, dict[str, dict]]]:
    commits = _git("log", "--format=%H", ref, "--", SNAPSHOT_PATH).decode().splitlines()
    snapshots: list[tuple[str, dict[str, dict]]] = []
    for commit in commits:
        raw = _git("show", f"{commit}:{SNAPSHOT_PATH}")
        snapshot = json.loads(raw)
        if not isinstance(snapshot, dict):
            continue
        # A rollback corpus needs the historical field-bearing representation,
        # not revisions already written in the new key-set shape.
        if snapshot and not any(isinstance(value, dict) and value for value in snapshot.values()):
            continue
        snapshots.append((commit, snapshot))
        if len(snapshots) == needed:
            break
    if len(snapshots) < needed:
        raise RuntimeError(f"need {needed} full snapshots on {ref}, found {len(snapshots)}")
    return list(reversed(snapshots))


def _value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _observable_mutations(prev: dict[str, dict], curr: dict[str, dict]) -> Counter[str]:
    mutations = compute_mutations(local_state=prev, jira_state=curr)
    mutations = drop_snapshot_differ_local_state_emissions(mutations)
    encoded: Counter[str] = Counter()
    for mutation in mutations:
        record = {
            "direction": _value(mutation.direction),
            "action": _value(mutation.action),
            "target": mutation.target,
            "payload": dict(mutation.payload or {}),
        }
        encoded[json.dumps(record, sort_keys=True, separators=(",", ":"))] += 1
    return encoded


def _inbound_create_count(delta: Counter[str]) -> int:
    total = 0
    for encoded, count in delta.items():
        record = json.loads(encoded)
        if record["direction"] == "inbound" and record["action"] == "create":
            total += count
    return total


def run(ref: str, pairs: int) -> dict[str, Any]:
    revisions = _full_snapshot_revisions(ref, pairs + 1)
    cases: list[dict[str, Any]] = []
    forward_total = 0
    reverse_total = 0
    unexpected_inbound_creates = 0

    for (prev_commit, full_prev), (curr_commit, curr) in zip(
        revisions[:-1], revisions[1:], strict=True
    ):
        key_set_prev = {jira_key: {} for jira_key in full_prev}
        full_effects = _observable_mutations(full_prev, curr)
        key_set_effects = _observable_mutations(key_set_prev, curr)
        forward = key_set_effects - full_effects
        reverse = full_effects - key_set_effects
        forward_count = sum(forward.values())
        reverse_count = sum(reverse.values())
        inbound_count = _inbound_create_count(reverse)
        forward_total += forward_count
        reverse_total += reverse_count
        unexpected_inbound_creates += inbound_count
        key_set_bytes = len(
            (json.dumps(key_set_prev, sort_keys=True, separators=(",", ":")) + "\n").encode()
        )
        cases.append(
            {
                "prev": prev_commit[:12],
                "curr": curr_commit[:12],
                "keys": len(key_set_prev),
                "full_bytes": len(json.dumps(full_prev, separators=(",", ":")).encode()),
                "key_set_bytes": key_set_bytes,
                "forward_extra_effects": forward_count,
                "reverse_extra_effects": reverse_count,
                "reverse_unexpected_inbound_creates": inbound_count,
            }
        )

    return {
        "ref": ref,
        "pairs_checked": len(cases),
        "forward_nonempty_effect_differences": forward_total,
        "reverse_nonempty_effect_differences": reverse_total,
        "reverse_unexpected_inbound_creates": unexpected_inbound_creates,
        "max_key_set_bytes": max(case["key_set_bytes"] for case in cases),
        "passed": forward_total == reverse_total == unexpected_inbound_creates == 0,
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ref", default="origin/tickets")
    parser.add_argument("--pairs", type=int, default=4)
    args = parser.parse_args()
    if args.pairs < 4:
        parser.error("--pairs must be at least 4")
    result = run(args.ref, args.pairs)
    print(json.dumps(result, indent=2, sort_keys=True))  # noqa: T201 - CLI artifact
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
