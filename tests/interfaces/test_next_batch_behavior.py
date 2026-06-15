"""Behavioral characterization of the in-process next-batch computation.

The conflict-aware selector is driven here as a *library* call — ``compute`` /
``to_json_dict`` / ``render_text`` / ``render_conflict_matrix`` against a
hand-built tracker — and the assertions pin **observable output** (the exact JSON
key set and values, the per-candidate fields, which tickets land in the batch vs
each skip bucket, the file-overlap decision, and the default-limit semantics)
rather than only the batch *size*. The byte-level CLI/text goldens and the
exit/usage contract live in ``test_next_batch_compute.py``; this module exists so
the selection logic is exercised in-process with rich assertions (so a regression
in a field mapping, an output key, or a skip-bucket decision is caught, not just a
change in the count of selected tickets).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rebar._engine_support import next_batch as nb


def _write(base: Path, tid: str, idx: int, et: str, data: dict, ts: int) -> None:
    d = base / tid
    d.mkdir(parents=True, exist_ok=True)
    evt = {
        "event_type": et,
        "ticket_id": tid,
        "timestamp": ts,
        "uuid": f"t-{tid}-{idx:04d}",
        "env_id": "test",
        "author": "alice",
        "data": data,
    }
    (d / f"{idx:03d}-{et}.json").write_text(json.dumps(evt))


def _create(
    base: Path,
    tid: str,
    ttype: str,
    parent: str | None,
    *,
    priority: int = 2,
    title: str | None = None,
    ts: int = 1700000000000000000,
) -> None:
    _write(
        base,
        tid,
        1,
        "CREATE",
        {
            "ticket_id": tid,
            "title": title or f"Title {tid}",
            "ticket_type": ttype,
            "status": "open",
            "priority": priority,
            "parent_id": parent,
        },
        ts,
    )


@pytest.fixture
def tracker(tmp_path: Path) -> Path:
    t = tmp_path / "tracker"
    t.mkdir()
    return t


# ───────────────────────── output shape + field mapping ──────────────────────
def test_to_json_dict_exact_keys_and_values(tracker: Path) -> None:
    """The JSON projection's top-level key SET is exact, and the per-candidate
    item carries the candidate's own id/title/priority/type (not a default or a
    renamed key). Pins the output contract a renamed/dropped key would break."""
    _create(tracker, "e", "epic", None, priority=1, title="The Epic")
    _create(tracker, "s", "story", "e", priority=2, title="The Story")
    _create(tracker, "t1", "task", "s", priority=2, title="Task One")
    _create(tracker, "t2", "task", "s", priority=3, title="Task Two")

    r = nb.compute(str(tracker), "e")
    d = nb.to_json_dict(r)

    assert set(d) == {
        "epic_id",
        "epic_title",
        "batch_size",
        "available_pool",
        "batch",
        "skipped_overlap",
        "skipped_blocked_story",
        "skipped_design_awaiting",
        "skipped_manual_awaiting",
        "skipped_in_progress",
        "skipped_needs_planning",
    }
    assert d["epic_id"] == "e"
    assert d["epic_title"] == "The Epic"
    assert d["batch_size"] == 2
    assert d["available_pool"] == 2

    # Per-item field mapping is exact (id/title/priority/type), and ordering is
    # priority-then-id, so t1 (P2) precedes t2 (P3).
    assert [b["id"] for b in d["batch"]] == ["t1", "t2"]
    first = d["batch"][0]
    assert set(first) == {"id", "title", "priority", "type", "files", "files_likely_read"}
    assert first == {
        "id": "t1",
        "title": "Task One",
        "priority": 2,
        "type": "task",
        "files": [],
        "files_likely_read": [],
    }
    # The second item carries ITS OWN priority/title (3 / "Task Two"), proving the
    # mapping reads per-candidate, not a shared default.
    assert d["batch"][1]["priority"] == 3
    assert d["batch"][1]["title"] == "Task Two"


def test_candidate_carries_per_ticket_fields(tracker: Path) -> None:
    """``_Candidate`` records the ticket's own id/title/priority/type — a
    regression that hard-coded any of these (or read the wrong key) is observable."""
    _create(tracker, "e", "epic", None, priority=1)
    _create(tracker, "s", "story", "e", priority=2)
    _create(tracker, "hi", "task", "s", priority=0, title="Critical task")
    _create(tracker, "lo", "task", "s", priority=4, title="Low task")

    r = nb.compute(str(tracker), "e")
    by_id = {c.id: c for c in r.batch}
    assert by_id["hi"].priority == 0 and by_id["hi"].title == "Critical task"
    assert by_id["lo"].priority == 4 and by_id["lo"].title == "Low task"
    assert by_id["hi"].itype == "task"
    # Priority-then-id sort puts the P0 task first.
    assert [c.id for c in r.batch] == ["hi", "lo"]


# ───────────────────────── skip-bucket classification ────────────────────────
def test_blocked_parent_story_defers_children(tracker: Path) -> None:
    """A task whose parent story has an open ``depends_on`` lands in
    skipped_blocked_story with the blocking story id — not in the batch."""
    ts = 1700000000000000000
    _create(tracker, "e", "epic", None, priority=1, ts=ts)
    _create(tracker, "s1", "story", "e", priority=2, ts=ts + 1)
    _create(tracker, "s2", "story", "e", priority=2, ts=ts + 2)
    # s2 depends on s1 (still open) → s2 is blocked.
    _write(tracker, "s2", 2, "LINK", {"relation": "depends_on", "target_id": "s1"}, ts + 3)
    _create(tracker, "t_ok", "task", "s1", priority=2, ts=ts + 4)
    _create(tracker, "t_blk", "task", "s2", priority=2, ts=ts + 5)

    r = nb.compute(str(tracker), "e")
    assert [c.id for c in r.batch] == ["t_ok"]
    assert r.skipped_blocked_story == [("t_blk", "Title t_blk", "s2")]
    # The JSON projection of the blocked-story bucket carries the exact item keys.
    d = nb.to_json_dict(r)
    assert d["skipped_blocked_story"] == [
        {"id": "t_blk", "title": "Title t_blk", "blocked_story": "s2"}
    ]


def test_in_progress_task_is_skipped_not_batched(tracker: Path) -> None:
    """An in_progress task is reported under skipped_in_progress and excluded from
    the batch (the batch is open work only)."""
    ts = 1700000000000000000
    _create(tracker, "e", "epic", None, priority=1, ts=ts)
    _create(tracker, "s", "story", "e", priority=2, ts=ts + 1)
    _create(tracker, "t", "task", "s", priority=2, ts=ts + 2)
    _write(tracker, "t", 2, "STATUS", {"status": "in_progress"}, ts + 3)

    r = nb.compute(str(tracker), "e")
    assert r.batch == []
    assert r.skipped_in_progress == [("t", "Title t")]


def test_design_awaiting_parent_defers_children(tracker: Path) -> None:
    """A task whose parent story is tagged ``design:awaiting_import`` is deferred
    into skipped_design_awaiting (with the story id), surfaced in both the result
    object and the text rendering — not batched."""
    ts = 1700000000000000000
    _create(tracker, "e", "epic", None, priority=1, ts=ts)
    # Tag the story at creation time (CREATE data.tags).
    _write(
        tracker,
        "s",
        1,
        "CREATE",
        {
            "ticket_id": "s",
            "title": "Awaiting Story",
            "ticket_type": "story",
            "status": "open",
            "priority": 2,
            "parent_id": "e",
            "tags": ["design:awaiting_import"],
        },
        ts + 1,
    )
    _create(tracker, "t", "task", "s", priority=2, ts=ts + 2)

    r = nb.compute(str(tracker), "e")
    assert r.batch == []
    assert r.skipped_design_awaiting == [("t", "Title t", "s")]
    d = nb.to_json_dict(r)
    assert d["skipped_design_awaiting"] == [{"id": "t", "title": "Title t", "blocked_story": "s"}]
    assert "SKIPPED_DESIGN_AWAITING: t\tdeferred (parent story s awaiting designer import)" in (
        nb.render_text(r)
    )


def test_childless_story_needs_planning(tracker: Path) -> None:
    """A story with no children is reported as needing planning, never batched."""
    ts = 1700000000000000000
    _create(tracker, "e", "epic", None, priority=1, ts=ts)
    _create(tracker, "lonely", "story", "e", priority=2, ts=ts + 1)

    r = nb.compute(str(tracker), "e")
    assert r.batch == []
    assert r.skipped_needs_planning == [("lonely", "Title lonely")]


# ───────────────────────── file-overlap selection ────────────────────────────
def test_file_impact_overlap_defers_second_task(tracker: Path) -> None:
    """Two ready tasks declaring the SAME file_impact path cannot run together:
    the first claims the file, the second is deferred with the conflict recorded."""
    ts = 1700000000000000000
    _create(tracker, "e", "epic", None, priority=1, ts=ts)
    _create(tracker, "s", "story", "e", priority=2, ts=ts + 1)
    _create(tracker, "a", "task", "s", priority=2, title="Task A", ts=ts + 2)
    _write(
        tracker,
        "a",
        2,
        "FILE_IMPACT",
        {"file_impact": [{"path": "src/x.py", "reason": "e"}]},
        ts + 3,
    )
    _create(tracker, "b", "task", "s", priority=2, title="Task B", ts=ts + 4)
    _write(
        tracker,
        "b",
        2,
        "FILE_IMPACT",
        {"file_impact": [{"path": "src/x.py", "reason": "e"}]},
        ts + 5,
    )

    r = nb.compute(str(tracker), "e")
    # a sorts before b (same priority, id tie-break) and claims src/x.py.
    assert [c.id for c in r.batch] == ["a"]
    assert r.skipped_overlap == [("b", "Task B", "src/x.py", "a")]
    # The JSON projection of the overlap bucket carries the exact item keys.
    d = nb.to_json_dict(r)
    assert d["skipped_overlap"] == [
        {"id": "b", "title": "Task B", "conflict_file": "src/x.py", "conflict_with": "a"}
    ]


def test_non_overlapping_tasks_both_batch(tracker: Path) -> None:
    """Distinct file_impact paths do NOT conflict — both tasks are selected."""
    ts = 1700000000000000000
    _create(tracker, "e", "epic", None, priority=1, ts=ts)
    _create(tracker, "s", "story", "e", priority=2, ts=ts + 1)
    _create(tracker, "a", "task", "s", priority=2, ts=ts + 2)
    _write(
        tracker,
        "a",
        2,
        "FILE_IMPACT",
        {"file_impact": [{"path": "src/a.py", "reason": "e"}]},
        ts + 3,
    )
    _create(tracker, "b", "task", "s", priority=2, ts=ts + 4)
    _write(
        tracker,
        "b",
        2,
        "FILE_IMPACT",
        {"file_impact": [{"path": "src/b.py", "reason": "e"}]},
        ts + 5,
    )

    r = nb.compute(str(tracker), "e")
    assert sorted(c.id for c in r.batch) == ["a", "b"]
    assert r.skipped_overlap == []


# ───────────────────────── limit semantics ───────────────────────────────────
def test_default_limit_is_unlimited(tracker: Path) -> None:
    """The ``limit`` default is 0 == unlimited: with three independent ready tasks
    all three are selected (a default of 1 would batch only one)."""
    ts = 1700000000000000000
    _create(tracker, "e", "epic", None, priority=1, ts=ts)
    _create(tracker, "s", "story", "e", priority=2, ts=ts + 1)
    for n in range(3):
        _create(tracker, f"t{n}", "task", "s", priority=2, ts=ts + 2 + n)

    r = nb.compute(str(tracker), "e")  # no limit arg → default
    assert len(r.batch) == 3


def test_explicit_limit_caps_batch(tracker: Path) -> None:
    """A positive limit caps the batch at that many tickets (lowest priority/id
    first), leaving the remainder unselected."""
    ts = 1700000000000000000
    _create(tracker, "e", "epic", None, priority=1, ts=ts)
    _create(tracker, "s", "story", "e", priority=2, ts=ts + 1)
    for n in range(3):
        _create(tracker, f"t{n}", "task", "s", priority=2, ts=ts + 2 + n)

    r = nb.compute(str(tracker), "e", limit=2)
    assert [c.id for c in r.batch] == ["t0", "t1"]


# ───────────────────────── conflict matrix rendering ─────────────────────────
def test_conflict_matrix_marks_overlapping_pair(tracker: Path) -> None:
    """The NxN matrix marks an overlapping candidate pair with 'X' and lists the
    shared file; non-overlapping cells stay '.'. Fewer than 2 candidates → empty."""
    ts = 1700000000000000000
    _create(tracker, "e", "epic", None, priority=1, ts=ts)
    _create(tracker, "s", "story", "e", priority=2, ts=ts + 1)
    _create(tracker, "a", "task", "s", priority=2, ts=ts + 2)
    _write(
        tracker,
        "a",
        2,
        "FILE_IMPACT",
        {"file_impact": [{"path": "src/x.py", "reason": "e"}]},
        ts + 3,
    )
    _create(tracker, "b", "task", "s", priority=2, ts=ts + 4)
    _write(
        tracker,
        "b",
        2,
        "FILE_IMPACT",
        {"file_impact": [{"path": "src/x.py", "reason": "e"}]},
        ts + 5,
    )

    r = nb.compute(str(tracker), "e")
    matrix = nb.render_conflict_matrix(r.candidates)
    assert "Conflict Matrix:" in matrix
    assert "X" in matrix
    assert "a <-> b: src/x.py" in matrix

    # A single candidate yields no matrix at all.
    assert nb.render_conflict_matrix(r.candidates[:1]) == ""


def test_render_text_lists_batch_and_skips(tracker: Path) -> None:
    """The text rendering names the epic, the pool/batch sizes, each batched TASK
    with its priority/type/title, and the blocked-story skip line."""
    ts = 1700000000000000000
    _create(tracker, "e", "epic", None, priority=1, title="My Epic", ts=ts)
    _create(tracker, "s1", "story", "e", priority=2, ts=ts + 1)
    _create(tracker, "s2", "story", "e", priority=2, ts=ts + 2)
    _write(tracker, "s2", 2, "LINK", {"relation": "depends_on", "target_id": "s1"}, ts + 3)
    _create(tracker, "ok", "task", "s1", priority=2, title="Do it", ts=ts + 4)
    _create(tracker, "blk", "task", "s2", priority=2, ts=ts + 5)

    r = nb.compute(str(tracker), "e")
    text = nb.render_text(r)
    assert "EPIC: e\tMy Epic" in text
    assert "AVAILABLE_POOL: 1" in text
    assert "BATCH_SIZE: 1" in text
    assert "TASK: ok\tP2\ttask\tDo it" in text
    assert "SKIPPED_BLOCKED_STORY: blk\tdeferred (parent story s2 is blocked)" in text


# ───────────────────────── tombstone status override ─────────────────────────
def test_deleted_dependency_does_not_block(tracker: Path) -> None:
    """A ``.tombstone.json`` carrying a terminal status makes a depends_on target
    count as closed — so a task whose only blocker was deleted becomes ready. This
    exercises the tombstone-directory scan (the per-entry is-dir guard + read)."""
    ts = 1700000000000000000
    _create(tracker, "e", "epic", None, priority=1, ts=ts)
    _create(tracker, "s", "story", "e", priority=2, ts=ts + 1)
    _create(tracker, "dep", "task", "s", priority=2, ts=ts + 2)
    _create(tracker, "t", "task", "s", priority=2, ts=ts + 3)
    _write(tracker, "t", 2, "LINK", {"relation": "depends_on", "target_id": "dep"}, ts + 4)
    # Without the tombstone, dep is open → t is blocked and excluded.
    r_blocked = nb.compute(str(tracker), "e")
    assert "t" not in {c.id for c in r_blocked.batch}

    # Tombstone dep as deleted: the scan reads it and t's blocker counts as closed.
    (tracker / "dep" / ".tombstone.json").write_text(json.dumps({"status": "deleted"}))
    r = nb.compute(str(tracker), "e")
    assert "t" in {c.id for c in r.batch}
