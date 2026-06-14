"""Tier E E5c: golden-bytes guard for the relocated transition/claim write core.

The locked STATUS/CLAIM critical section moved from ``_engine/ticket_txn.py`` into
``rebar._commands.txn``. This pins the on-disk event-byte format so the relocation
(and any future edit) cannot silently change committed bytes: transition/claim
events are written with ``json.dump(event, ensure_ascii=False)`` — default
separators, **UNSORTED** keys in composition order — NOT the canonical sorted-compact
form (``separators=(",",":"), sort_keys=True``) used by the leaf-write path.
Canonicalising these bytes is epic P1.0's scope, not E5c's; until then this test is
the tripwire that the two formats stay distinct and transition/claim keep theirs.
"""

from __future__ import annotations

import json
from pathlib import Path

import rebar


def _newest(ticket_dir: Path, suffix: str) -> Path:
    files = sorted(p for p in ticket_dir.iterdir() if p.name.endswith(suffix) and not p.name.startswith("."))
    assert files, f"no {suffix} event in {ticket_dir}"
    return files[-1]


def _assert_unsorted_spaced(raw: bytes) -> dict:
    """The bytes round-trip through the DEFAULT json.dumps (spaced, insertion
    order) but are NOT the canonical sorted-compact form."""
    parsed = json.loads(raw)
    default_form = json.dumps(parsed, ensure_ascii=False).encode("utf-8")
    canonical_form = json.dumps(
        parsed, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    assert raw == default_form, (
        "transition/claim event must keep the default-separator unsorted format "
        f"(got {raw!r}, expected {default_form!r})"
    )
    assert raw != canonical_form, (
        "transition/claim event must NOT be canonical sorted-compact bytes "
        "(that switch belongs to epic P1.0, not E5c)"
    )
    return parsed


def test_status_event_bytes_are_unsorted_spaced(rebar_repo: Path) -> None:
    tracker = rebar_repo / ".tickets-tracker"
    tid = rebar.create_ticket("task", "E3 status bytes", repo_root=str(rebar_repo))
    rebar.transition(tid, "open", "in_progress", repo_root=str(rebar_repo))

    raw = _newest(tracker / tid, "-STATUS.json").read_bytes()
    parsed = _assert_unsorted_spaced(raw)
    # Composition order is the contract the reducer + cross-clone fork tie-break
    # rely on; pin it explicitly (a sort would reorder these).
    assert list(parsed.keys()) == [
        "timestamp",
        "uuid",
        "event_type",
        "env_id",
        "author",
        "parent_status_uuid",
        "data",
    ]
    assert parsed["event_type"] == "STATUS"
    assert parsed["data"]["status"] == "in_progress"


def test_claim_writes_status_and_edit_bytes(rebar_repo: Path) -> None:
    tracker = rebar_repo / ".tickets-tracker"
    tid = rebar.create_ticket("task", "E3 claim bytes", repo_root=str(rebar_repo))
    rebar.claim(tid, assignee="alice", repo_root=str(rebar_repo))

    status = _assert_unsorted_spaced((_newest(tracker / tid, "-STATUS.json")).read_bytes())
    assert status["data"]["status"] == "in_progress"

    edit = _assert_unsorted_spaced((_newest(tracker / tid, "-EDIT.json")).read_bytes())
    assert edit["event_type"] == "EDIT"
    assert edit["data"]["fields"]["assignee"] == "alice"
    # Atomic single-commit: STATUS sorts before EDIT (ts2 sampled after ts1).
    assert status["timestamp"] < edit["timestamp"]
