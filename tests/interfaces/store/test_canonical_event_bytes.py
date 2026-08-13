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

import pytest

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


def test_every_committed_event_is_canonical_bytes(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = str(rebar_repo)
    # Fold unconditionally when the explicit compact below runs: the default threshold and
    # compaction horizon would both skip a small, freshly-written ticket, and a skipped fold
    # would silently drop SNAPSHOT from the type-coverage assertion at the end.
    monkeypatch.setenv("REBAR_COMPACT_THRESHOLD", "1")
    monkeypatch.setenv("REBAR_COMPACTION_HORIZON_NS", "0")

    epic = rebar.create_ticket("epic", "parity epic", repo_root=repo)
    task = rebar.create_ticket("task", "parity task", repo_root=repo)

    # task: lifecycle left at in_progress (NOT closed) so each producer's event
    # file survives for the type-coverage assertion — the explicit compact below
    # would otherwise fold them.
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

    # A throwaway ticket compacted to exercise the SNAPSHOT writer, kept separate from
    # `task` so the fold cannot swallow the per-producer events asserted above. The compact
    # is EXPLICIT: closing no longer compacts (bug choosy-arthrodic-barbet moved compaction
    # out of band), so relying on the close to produce a SNAPSHOT would silently stop
    # covering the SNAPSHOT writer — which is exactly what the type-coverage assertion at the
    # end of this test exists to catch.
    snap = rebar.create_ticket("task", "snapshot fodder", repo_root=repo)
    rebar.claim(snap, assignee="me", repo_root=repo)
    rebar.transition(snap, "in_progress", "closed", repo_root=repo)
    rebar.compact(snap, repo_root=repo)  # → SNAPSHOT

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
