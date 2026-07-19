#!/usr/bin/env python3
"""Backfill ``close_class`` on already-closed bugs from structural signals (ticket 5062).

A one-time, standalone script. It classifies CLOSED bugs that LACK a reduced
``state["close_class"]`` using only structural signals in the event store — no LLM,
no prose. Bugs that already carry a ``close_class`` are authoritative and SKIPPED.

Classification (first firing signal wins, in priority order):

1. ``regression``       — a substantive revert: reduced ``state["reverts"]`` has an
                          entry whose ``target_event_type`` is ``STATUS`` or ``COMMITS``.
2. ``plan_defect``      — a ``REVIEW_RESULT`` payload with ``verdict == "BLOCK"``.
3. ``env_integration``  — a ``gate_error_v1`` record (schema ``gate_error_v1``) on the
                          bug's ``REVIEW_RESULT`` / ``COMPLETION_VERDICT`` streams.
4. ``undetermined``     — no signal fired.

``classify_backfill(repo_root)`` returns the labeled records; ``main()`` writes them
(one JSON object per line) to ``<repo_root>/.rebar/backfill-close-class.ndjson``,
idempotent by ``ticket_id``.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import rebar
from rebar.llm.plan_review import sidecar

_SUBSTANTIVE_REVERT_TARGETS = {"STATUS", "COMMITS"}
_ARTIFACT_RELPATH = Path(".rebar") / "backfill-close-class.ndjson"


def _has_substantive_revert(state: dict[str, Any]) -> bool:
    for rev in state.get("reverts") or []:
        if isinstance(rev, dict) and rev.get("target_event_type") in _SUBSTANTIVE_REVERT_TARGETS:
            return True
    return False


def _has_block_review(ticket_id: str, repo_root: str) -> bool:
    try:
        payloads = sidecar.all_review_results(ticket_id, repo_root=repo_root)
    except Exception:  # noqa: BLE001 — best-effort sidecar read; a bad/absent stream must not abort backfill
        return False
    return any(isinstance(p, dict) and p.get("verdict") == "BLOCK" for p in payloads)


def _has_gate_error(ticket_dir: Path) -> bool:
    """Raw scan: schema-guarded readers skip ``gate_error_v1``, so glob and load directly."""
    for pattern in ("*-REVIEW_RESULT.json", "*-COMPLETION_VERDICT.json"):
        for path in ticket_dir.glob(pattern):
            try:
                event = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            data = event.get("data")
            if isinstance(data, dict) and data.get("schema") == "gate_error_v1":
                return True
    return False


def _classify(
    ticket_id: str, ticket_dir: Path, state: dict[str, Any], repo_root: str
) -> tuple[str, str]:
    """Return ``(close_class, evidence)`` for a closed bug lacking ``close_class``."""
    if _has_substantive_revert(state):
        return "regression", "substantive_revert"
    if _has_block_review(ticket_id, repo_root):
        return "plan_defect", "review_block"
    if _has_gate_error(ticket_dir):
        return "env_integration", "gate_error_v1"
    return "undetermined", "no_signal"


def classify_backfill(repo_root: str) -> list[dict[str, Any]]:
    """Classify closed bugs lacking ``close_class`` and return labeled backfill records.

    Each record: ``{"ticket_id", "close_class", "source", "confidence", "evidence", "ts"}``.
    Bugs that already carry a ``close_class`` are authoritative and excluded.
    """
    tracker = rebar.config.tracker_dir(repo_root)
    records: list[dict[str, Any]] = []
    if not tracker.is_dir():
        return records

    for ticket_dir in sorted(p for p in tracker.iterdir() if p.is_dir()):
        state = rebar.reduce_ticket(ticket_dir, include_retired=True)
        if not state:
            continue
        if state.get("ticket_type") != "bug" or state.get("status") != "closed":
            continue
        if state.get("close_class"):  # already labeled — authoritative, skip
            continue

        ticket_id = state.get("ticket_id") or ticket_dir.name
        close_class, evidence = _classify(ticket_id, ticket_dir, state, repo_root)
        records.append(
            {
                "ticket_id": ticket_id,
                "close_class": close_class,
                "source": "backfill_classified",
                "confidence": "classified",
                "evidence": evidence,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
        )
    return records


def _existing_ticket_ids(artifact: Path) -> set[str]:
    seen: set[str] = set()
    if not artifact.exists():
        return seen
    for line in artifact.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            tid = json.loads(line).get("ticket_id")
        except json.JSONDecodeError:
            continue
        if tid:
            seen.add(tid)
    return seen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill bug close_class from structural signals."
    )
    parser.add_argument("--repo-root", default=".", help="Repository root (default: cwd).")
    parser.add_argument("--dry-run", action="store_true", help="Print records instead of writing.")
    args = parser.parse_args(argv)

    repo_root = str(Path(args.repo_root).resolve())
    records = classify_backfill(repo_root)

    if args.dry_run:
        for rec in records:
            print(json.dumps(rec))  # noqa: T201 — CLI presentation surface
        return 0

    artifact = Path(repo_root) / _ARTIFACT_RELPATH
    artifact.parent.mkdir(parents=True, exist_ok=True)
    already = _existing_ticket_ids(artifact)
    new = [r for r in records if r["ticket_id"] not in already]
    if new:
        with artifact.open("a", encoding="utf-8") as fh:
            for rec in new:
                fh.write(json.dumps(rec) + "\n")
    skipped = len(records) - len(new)
    print(  # noqa: T201 — CLI presentation surface
        f"backfill: {len(new)} new record(s) written, {skipped} skipped (already present)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
