"""next-batch characterization — docs/bash-migration.md §5.

The python characterization of the conflict-aware selector: golden output, the
determinism fix, error/exit contracts, and the library/schema shape.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from rebar._engine import in_process_cli

# Drive the in-process rebar CLI; the next-batch arm routes to the in-process
# compute, so this characterizes the production output.
_CLI = in_process_cli()


# ───────────────────────────── fixtures ──────────────────────────────────────
def _write(base: Path, tid: str, idx: int, et: str, data: dict, ts: int) -> None:
    d = base / tid
    d.mkdir(parents=True, exist_ok=True)
    evt = {
        "event_type": et,
        "ticket_id": tid,
        "timestamp": ts,
        "uuid": f"t-{tid}-{idx:04d}",
        "env_id": "test",
        "author": "test",
        "data": data,
    }
    (d / f"{idx:03d}-{et}.json").write_text(json.dumps(evt))


def _three_tier(base: Path) -> None:
    """epic → story-1{task-1,task-2}, story-2(blocked by story-1){task-3}."""
    ts = 1700000000000000000
    _write(
        base,
        "nb-epic",
        1,
        "CREATE",
        {
            "ticket_id": "nb-epic",
            "title": "NB Epic",
            "ticket_type": "epic",
            "status": "open",
            "priority": 1,
            "parent_id": None,
        },
        ts,
    )
    _write(
        base,
        "nb-story-1",
        1,
        "CREATE",
        {
            "ticket_id": "nb-story-1",
            "title": "NB Story One",
            "ticket_type": "story",
            "status": "open",
            "priority": 2,
            "parent_id": "nb-epic",
        },
        ts + 1,
    )
    _write(
        base,
        "nb-task-1",
        1,
        "CREATE",
        {
            "ticket_id": "nb-task-1",
            "title": "NB Task One",
            "ticket_type": "task",
            "status": "open",
            "priority": 2,
            "parent_id": "nb-story-1",
        },
        ts + 2,
    )
    _write(
        base,
        "nb-task-2",
        1,
        "CREATE",
        {
            "ticket_id": "nb-task-2",
            "title": "NB Task Two",
            "ticket_type": "task",
            "status": "open",
            "priority": 2,
            "parent_id": "nb-story-1",
        },
        ts + 3,
    )
    _write(
        base,
        "nb-story-2",
        1,
        "CREATE",
        {
            "ticket_id": "nb-story-2",
            "title": "NB Story Two",
            "ticket_type": "story",
            "status": "open",
            "priority": 3,
            "parent_id": "nb-epic",
        },
        ts + 4,
    )
    _write(
        base, "nb-story-2", 2, "LINK", {"relation": "depends_on", "target_id": "nb-story-1"}, ts + 5
    )
    _write(
        base,
        "nb-task-3",
        1,
        "CREATE",
        {
            "ticket_id": "nb-task-3",
            "title": "NB Task Three",
            "ticket_type": "task",
            "status": "open",
            "priority": 3,
            "parent_id": "nb-story-2",
        },
        ts + 6,
    )


def _file_impact(base: Path) -> None:
    """Two tasks, identical recorded file_impact on ONE shared path."""
    ts = 1700002000000000000
    _write(
        base,
        "fi-epic",
        1,
        "CREATE",
        {
            "ticket_id": "fi-epic",
            "title": "FI Epic",
            "ticket_type": "epic",
            "status": "open",
            "priority": 1,
            "parent_id": None,
        },
        ts,
    )
    _write(
        base,
        "fi-story",
        1,
        "CREATE",
        {
            "ticket_id": "fi-story",
            "title": "FI Story",
            "ticket_type": "story",
            "status": "open",
            "priority": 2,
            "parent_id": "fi-epic",
        },
        ts + 1,
    )
    _write(
        base,
        "fi-a",
        1,
        "CREATE",
        {
            "ticket_id": "fi-a",
            "title": "FI Task A",
            "ticket_type": "task",
            "status": "open",
            "priority": 2,
            "parent_id": "fi-story",
        },
        ts + 2,
    )
    _write(
        base,
        "fi-a",
        2,
        "FILE_IMPACT",
        {"file_impact": [{"path": "src/shared.py", "reason": "edit"}]},
        ts + 3,
    )
    _write(
        base,
        "fi-b",
        1,
        "CREATE",
        {
            "ticket_id": "fi-b",
            "title": "FI Task B",
            "ticket_type": "task",
            "status": "open",
            "priority": 2,
            "parent_id": "fi-story",
        },
        ts + 4,
    )
    _write(
        base,
        "fi-b",
        2,
        "FILE_IMPACT",
        {"file_impact": [{"path": "src/shared.py", "reason": "edit"}]},
        ts + 5,
    )


def _multi_overlap(base: Path) -> None:
    """Two tasks declaring the SAME two file_impact paths → the conflict set has 2
    shared files; the reported conflict_file must be the lexicographically smallest
    ('aaa/shared.py'), byte-stable across runs (the determinism case)."""
    ts = 1700001000000000000
    _write(
        base,
        "ov-epic",
        1,
        "CREATE",
        {
            "ticket_id": "ov-epic",
            "title": "OV Epic",
            "ticket_type": "epic",
            "status": "open",
            "priority": 1,
            "parent_id": None,
        },
        ts,
    )
    _write(
        base,
        "ov-story",
        1,
        "CREATE",
        {
            "ticket_id": "ov-story",
            "title": "OV Story",
            "ticket_type": "story",
            "status": "open",
            "priority": 2,
            "parent_id": "ov-epic",
        },
        ts + 1,
    )
    _write(
        base,
        "ov-a",
        1,
        "CREATE",
        {
            "ticket_id": "ov-a",
            "title": "OV Task A",
            "ticket_type": "task",
            "status": "open",
            "priority": 2,
            "parent_id": "ov-story",
        },
        ts + 2,
    )
    _write(
        base,
        "ov-a",
        2,
        "FILE_IMPACT",
        {"file_impact": [{"path": "aaa/shared.py", "reason": "e"}, {"path": "zzz/shared.py", "reason": "e"}]},
        ts + 3,
    )
    _write(
        base,
        "ov-b",
        1,
        "CREATE",
        {
            "ticket_id": "ov-b",
            "title": "OV Task B",
            "ticket_type": "task",
            "status": "open",
            "priority": 2,
            "parent_id": "ov-story",
        },
        ts + 4,
    )
    _write(
        base,
        "ov-b",
        2,
        "FILE_IMPACT",
        {"file_impact": [{"path": "aaa/shared.py", "reason": "e"}, {"path": "zzz/shared.py", "reason": "e"}]},
        ts + 5,
    )


def _run(tracker: Path, *args: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "TICKETS_TRACKER_DIR": str(tracker),
        "REBAR_NO_SYNC": "1",
        "_TICKET_TEST_NO_SYNC": "1",
    }
    return subprocess.run([_CLI, "next-batch", *args], env=env, capture_output=True, text=True)


@pytest.fixture
def tracker(tmp_path: Path):
    t = tmp_path / "tracker"
    t.mkdir()
    return t


# ───────────────────────────── golden output ─────────────────────────────────
def test_three_tier_text_golden(tracker: Path):
    _three_tier(tracker)
    r = _run(tracker, "nb-epic")
    assert r.returncode == 0
    assert r.stdout == (
        "EPIC: nb-epic\tNB Epic\n"
        "AVAILABLE_POOL: 2\n"
        "BATCH_SIZE: 2\n"
        "TASK: nb-task-1\tP2\ttask\tNB Task One\n"
        "TASK: nb-task-2\tP2\ttask\tNB Task Two\n"
        "SKIPPED_BLOCKED_STORY: nb-task-3\tdeferred (parent story nb-story-2 is blocked)\n"
    )
    # The conflict matrix prints to stderr when ≥2 candidates.
    assert "Conflict Matrix:" in r.stderr


def test_three_tier_json_shape(tracker: Path):
    _three_tier(tracker)
    d = json.loads(_run(tracker, "nb-epic", "--output", "json").stdout)
    assert {
        "epic_id",
        "available_pool",
        "batch_size",
        "batch",
        "skipped_overlap",
        "skipped_blocked_story",
    } <= set(d)
    assert {e["id"] for e in d["batch"]} == {"nb-task-1", "nb-task-2"}
    # batch_item shape: no routing-field leak (the common.schema.json batch_item contract).
    allowed = {"id", "title", "priority", "type", "files", "files_likely_read"}
    for e in d["batch"]:
        assert set(e) <= allowed


def test_file_impact_overlap_serializes(tracker: Path):
    _file_impact(tracker)
    d = json.loads(_run(tracker, "fi-epic", "--output", "json").stdout)
    assert len(d["batch"]) == 1
    assert len(d["skipped_overlap"]) == 1
    assert d["skipped_overlap"][0]["conflict_file"] == "src/shared.py"


def test_multi_overlap_is_deterministic(tracker: Path):
    """The determinism fix: conflict_file is the lexicographically smallest claimed
    file, byte-stable across runs (the bash original coin-flipped under set
    iteration). Batch composition is unambiguous either way."""
    _multi_overlap(tracker)
    outs = {_run(tracker, "ov-epic").stdout for _ in range(6)}
    assert len(outs) == 1, f"non-deterministic: {outs}"
    out = outs.pop()
    assert "SKIPPED_OVERLAP: ov-b\tdeferred (overlaps with ov-a on aaa/shared.py)" in out
    assert "TASK: ov-a" in out and "TASK: ov-b" not in out


# ───────────────────────────── error / exit contract ─────────────────────────
def test_missing_epic_text_and_json(tracker: Path):
    _three_tier(tracker)
    t = _run(tracker, "nope")
    assert t.returncode == 1 and "Error: Could not load epic nope" in t.stderr
    j = _run(tracker, "nope", "--output", "json")
    assert j.returncode == 1
    env = json.loads(j.stdout)
    assert env["error"] == "ticket_not_found" and env["input"] == "nope"


def test_usage_and_limit_errors(tracker: Path):
    _three_tier(tracker)
    assert _run(tracker).returncode == 2  # no epic id
    assert _run(tracker, "nb-epic", "--limit=abc").returncode == 2
    assert _run(tracker, "nb-epic", "--bogus").returncode == 2
    # --limit=0 → empty batch, exit 0
    z = _run(tracker, "nb-epic", "--limit=0")
    assert z.returncode == 0 and z.stdout.strip() == "BATCH_SIZE: 0"


def test_library_and_mcp_shape(tracker: Path, monkeypatch):
    import rebar
    from rebar.mcp_server import NextBatchOut

    _three_tier(tracker)
    monkeypatch.setenv("TICKETS_TRACKER_DIR", str(tracker))
    monkeypatch.setenv("REBAR_NO_SYNC", "1")
    d = rebar.next_batch("nb-epic")
    # Observable result (not just the size): the resolved epic, the selected
    # tickets with their own fields, and the blocked-story skip — so a regression
    # in the field mapping or the selection is caught, not only a count change.
    assert d["epic_id"] == "nb-epic"
    assert d["epic_title"] == "NB Epic"
    assert d["batch_size"] == 2 and d["available_pool"] == 2
    assert [b["id"] for b in d["batch"]] == ["nb-task-1", "nb-task-2"]
    assert d["batch"][0] == {
        "id": "nb-task-1",
        "title": "NB Task One",
        "priority": 2,
        "type": "task",
        "files": [],
        "files_likely_read": [],
    }
    assert [s["id"] for s in d["skipped_blocked_story"]] == ["nb-task-3"]
    assert d["skipped_blocked_story"][0]["blocked_story"] == "nb-story-2"
    NextBatchOut.model_validate(d)
