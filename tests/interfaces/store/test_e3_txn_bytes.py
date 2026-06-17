"""Tier E E5c → P1.0: golden-bytes guard for the transition/claim write core.

The locked STATUS/CLAIM critical section lives in ``rebar._commands.txn``. This
pins the on-disk event-byte format so any future edit cannot silently change
committed bytes. As of epic P1.0 (canonical event-byte unification) transition/
claim events are serialized through the single canonical helper
``rebar._store.canonical.canonical_bytes`` — sorted keys, compact
``separators=(",",":")``, ``ensure_ascii=False`` — byte-identical to every other
live writer (previously these were raw ``json.dump`` default-separator/unsorted
bytes; P1.0 flipped that, and this test with it).
"""

from __future__ import annotations

import json
from pathlib import Path

import rebar
from rebar._store.canonical import canonical_bytes


def _newest(ticket_dir: Path, suffix: str) -> Path:
    files = sorted(
        p for p in ticket_dir.iterdir() if p.name.endswith(suffix) and not p.name.startswith(".")
    )
    assert files, f"no {suffix} event in {ticket_dir}"
    return files[-1]


def _assert_canonical(raw: bytes) -> dict:
    """The bytes are exactly the canonical sorted-compact serialization of their
    own parsed content (and NOT the old default-separator/unsorted form)."""
    parsed = json.loads(raw)
    default_form = json.dumps(parsed, ensure_ascii=False).encode("utf-8")
    assert raw == canonical_bytes(parsed), (
        "transition/claim event must be canonical sorted-compact bytes "
        f"(got {raw!r}, expected {canonical_bytes(parsed)!r})"
    )
    assert raw != default_form, (
        "canonical bytes must differ from the old default-separator spaced form"
    )
    return parsed


def test_status_event_bytes_are_canonical(rebar_repo: Path) -> None:
    tracker = rebar_repo / ".tickets-tracker"
    tid = rebar.create_ticket("task", "E3 status bytes", repo_root=str(rebar_repo))
    rebar.transition(tid, "open", "in_progress", repo_root=str(rebar_repo))

    raw = _newest(tracker / tid, "-STATUS.json").read_bytes()
    parsed = _assert_canonical(raw)
    # Keys are now emitted sorted (the canonical form); the reducer reads keys, not
    # byte order, so this is replay-safe.
    assert list(parsed.keys()) == [
        "author",
        "data",
        "env_id",
        "event_type",
        "parent_status_uuid",
        "timestamp",
        "uuid",
    ]
    assert parsed["event_type"] == "STATUS"
    assert parsed["data"]["status"] == "in_progress"


def test_claim_writes_status_and_edit_bytes(rebar_repo: Path) -> None:
    tracker = rebar_repo / ".tickets-tracker"
    tid = rebar.create_ticket("task", "E3 claim bytes", repo_root=str(rebar_repo))
    rebar.claim(tid, assignee="alice", repo_root=str(rebar_repo))

    status = _assert_canonical((_newest(tracker / tid, "-STATUS.json")).read_bytes())
    assert status["data"]["status"] == "in_progress"

    edit = _assert_canonical((_newest(tracker / tid, "-EDIT.json")).read_bytes())
    assert edit["event_type"] == "EDIT"
    assert edit["data"]["fields"]["assignee"] == "alice"
    # Atomic single-commit: STATUS sorts before EDIT (ts2 sampled after ts1).
    assert status["timestamp"] < edit["timestamp"]
