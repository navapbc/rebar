"""Cross-producer event-byte parity (epic P1.0, Success Criterion #1).

Drives the rebar **library** through a real git-backed store so that every live
event producer actually runs — CREATE/COMMENT/EDIT/TAG/FILE_IMPACT/VERIFY_COMMANDS
via the ``_seam`` committer, STATUS/EDIT via the inline ``txn`` writer, LINK via
``graph._links``, and SNAPSHOT via ``compact`` — then walks **every** committed
event file and asserts its bytes are exactly ``canonical_bytes(parsed_event)``.

This is the writer-agnostic statement of the contract: regardless of which code
path wrote an event, the on-disk bytes equal the one canonical serialization of
their own parsed content. Before P1.0 the inline txn/link/compact writers emitted
unsorted ``json.dumps`` and would fail this. A non-ASCII comment body exercises
the ``ensure_ascii=False`` leg.
"""

from __future__ import annotations

import json
from pathlib import Path

import rebar
from rebar._store.canonical import canonical_bytes


def _event_files(tracker: Path) -> list[Path]:
    """Every committed event file: a ``*.json`` whose parsed content carries an
    ``event_type`` (skips ``.cache.json`` / ``.tombstone.json`` markers)."""
    out: list[Path] = []
    for p in tracker.rglob("*.json"):
        try:
            obj = json.loads(p.read_bytes())
        except (ValueError, OSError):
            continue
        if isinstance(obj, dict) and "event_type" in obj:
            out.append(p)
    return out


def test_every_committed_event_is_canonical_bytes(rebar_repo: Path):
    repo = str(rebar_repo)

    epic = rebar.create_ticket("epic", "parity epic", repo_root=repo)
    task = rebar.create_ticket("task", "parity task", repo_root=repo)

    # task: lifecycle left at in_progress (NOT closed) so each producer's event
    # file survives for the type-coverage assertion — closing auto-compacts.
    # _seam-committed producers (already canonical pre-P1.0 — the baseline):
    rebar.comment(task, "héllo 世界 — non-ascii body", repo_root=repo)
    rebar.edit_ticket(task, description="updated desc", repo_root=repo)
    rebar.tag(task, "parity", repo_root=repo)
    rebar.set_file_impact(task, [{"path": "src/x.py", "reason": "r"}], repo_root=repo)
    rebar.set_verify_commands(
        task, [{"dd_id": "DD1", "dd_text": "tests pass", "command": "pytest -q"}], repo_root=repo
    )
    # The writers P1.0 actually fixed: LINK (graph._links) and STATUS/EDIT (txn):
    rebar.link(task, epic, "discovered_from", repo_root=repo)
    rebar.claim(task, assignee="me", repo_root=repo)  # STATUS(open→in_progress) + EDIT

    # A throwaway ticket closed to exercise the SNAPSHOT writer (compact-on-close
    # squashes its events into one SNAPSHOT, so keep it separate from `task`).
    snap = rebar.create_ticket("task", "snapshot fodder", repo_root=repo)
    rebar.claim(snap, assignee="me", repo_root=repo)
    rebar.transition(snap, "in_progress", "closed", repo_root=repo)  # → compact-on-close SNAPSHOT

    tracker = rebar_repo / ".tickets-tracker"
    files = _event_files(tracker)
    assert files, "expected committed event files under the tracker"

    seen: set[str] = set()
    for p in files:
        raw = p.read_bytes()
        parsed = json.loads(raw)
        assert raw == canonical_bytes(parsed), f"non-canonical event bytes in {p.name}"
        assert not raw.endswith(b"\n"), f"trailing newline in {p.name}"
        seen.add(parsed["event_type"])

    # Guard against a silent no-op: the writers P1.0 fixed must actually have run.
    assert {"CREATE", "COMMENT", "EDIT", "LINK", "STATUS", "SNAPSHOT"} <= seen, seen
