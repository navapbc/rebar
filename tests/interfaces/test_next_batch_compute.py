"""Tier C (REBAR_COMPUTE) parity + determinism gate for ``next-batch`` —
docs/bash-migration.md §3/§5.

Three things are pinned here, the way the retired Tier B suite pinned leaf writes:

1. **Switch parity.** The dispatcher's bash ``_compute_python`` helper must resolve
   every ``REBAR_COMPUTE`` value to the SAME bash/python verdict as the canonical
   parser ``rebar._switch`` (one source of truth, the §3 letter). We pin the exact
   ``printf | tr | tr`` idiom against a typo/case matrix.
2. **Cross-impl byte parity.** On scenarios whose conflict sets are unambiguous
   (≤1 overlapping file), the legacy bash orchestrator and the Python port produce
   byte-identical stdout/stderr/exit through the dispatcher.
3. **Determinism (the bug the port fixes).** When a candidate overlaps on >1 file,
   the bash original's ``conflict_file`` diagnostic coin-flips per run (it iterates
   an unordered set under hash randomization). The Python port sorts that
   iteration, so its output is byte-stable across runs — pinned here. Batch
   *composition* is identical either way; only the named file stabilizes.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from rebar import _switch

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_DISPATCHER = _PLUGIN_ROOT / "src" / "rebar" / "_engine" / "ticket"

# The dispatcher's _compute_python verdict, byte-for-byte. Kept in sync with that
# helper (a divergence makes test_switch_resolution_* fail). Echoes the helper's
# bash/python decision so the test pins the VERDICT — correct under either default
# (the expansion default + comparison flip together at cutover).
_BASH_RESOLVE = (
    r"""_v=$(printf '%s' "${REBAR_COMPUTE:-python}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]'); """
    r"""[ "$_v" != "bash" ] && echo python || echo bash"""
)
_MATRIX = ["", "python", "PYTHON", " Python ", "bash", "BASH", "py", "bogus", "1", "true"]


# ───────────────────────────── switch parity ─────────────────────────────────
@pytest.mark.parametrize("value", _MATRIX)
def test_switch_resolution_matches_bash_idiom(value: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("REBAR_COMPUTE", value)
    py_uses_python = _switch.resolve("REBAR_COMPUTE") == "python"
    out = subprocess.run(
        ["bash", "-c", _BASH_RESOLVE],
        env={"REBAR_COMPUTE": value, "PATH": "/usr/bin:/bin:/usr/local/bin"},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    bash_uses_python = out == "python"
    assert py_uses_python == bash_uses_python


def test_switch_unset_defaults_python(monkeypatch: pytest.MonkeyPatch):
    # Tier C default flipped to python on 2026-06-12 (rollback: REBAR_COMPUTE=bash).
    monkeypatch.delenv("REBAR_COMPUTE", raising=False)
    assert _switch.resolve("REBAR_COMPUTE") == "python"


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
    _write(base, "nb-epic", 1, "CREATE", {"ticket_id": "nb-epic", "title": "NB Epic", "ticket_type": "epic", "status": "open", "priority": 1, "parent_id": None}, ts)
    _write(base, "nb-story-1", 1, "CREATE", {"ticket_id": "nb-story-1", "title": "NB Story One", "ticket_type": "story", "status": "open", "priority": 2, "parent_id": "nb-epic"}, ts + 1)
    _write(base, "nb-task-1", 1, "CREATE", {"ticket_id": "nb-task-1", "title": "NB Task One", "ticket_type": "task", "status": "open", "priority": 2, "parent_id": "nb-story-1"}, ts + 2)
    _write(base, "nb-task-2", 1, "CREATE", {"ticket_id": "nb-task-2", "title": "NB Task Two", "ticket_type": "task", "status": "open", "priority": 2, "parent_id": "nb-story-1"}, ts + 3)
    _write(base, "nb-story-2", 1, "CREATE", {"ticket_id": "nb-story-2", "title": "NB Story Two", "ticket_type": "story", "status": "open", "priority": 3, "parent_id": "nb-epic"}, ts + 4)
    _write(base, "nb-story-2", 2, "LINK", {"relation": "depends_on", "target_id": "nb-story-1"}, ts + 5)
    _write(base, "nb-task-3", 1, "CREATE", {"ticket_id": "nb-task-3", "title": "NB Task Three", "ticket_type": "task", "status": "open", "priority": 3, "parent_id": "nb-story-2"}, ts + 6)


def _file_impact(base: Path) -> None:
    """Two tasks, identical recorded file_impact on ONE shared path (deterministic)."""
    ts = 1700002000000000000
    _write(base, "fi-epic", 1, "CREATE", {"ticket_id": "fi-epic", "title": "FI Epic", "ticket_type": "epic", "status": "open", "priority": 1, "parent_id": None}, ts)
    _write(base, "fi-story", 1, "CREATE", {"ticket_id": "fi-story", "title": "FI Story", "ticket_type": "story", "status": "open", "priority": 2, "parent_id": "fi-epic"}, ts + 1)
    _write(base, "fi-a", 1, "CREATE", {"ticket_id": "fi-a", "title": "FI Task A", "ticket_type": "task", "status": "open", "priority": 2, "parent_id": "fi-story"}, ts + 2)
    _write(base, "fi-a", 2, "FILE_IMPACT", {"file_impact": [{"path": "src/shared.py", "reason": "edit"}]}, ts + 3)
    _write(base, "fi-b", 1, "CREATE", {"ticket_id": "fi-b", "title": "FI Task B", "ticket_type": "task", "status": "open", "priority": 2, "parent_id": "fi-story"}, ts + 4)
    _write(base, "fi-b", 2, "FILE_IMPACT", {"file_impact": [{"path": "src/shared.py", "reason": "edit"}]}, ts + 5)


def _multi_overlap(base: Path) -> None:
    """Two tasks sharing a path that ALSO triggers the extensionless 'rebar'
    substring match → conflict set has 2 files (the bash-nondeterministic case)."""
    ts = 1700001000000000000
    sf = "src/rebar/_engine/sprint-next-batch.sh"
    _write(base, "ov-epic", 1, "CREATE", {"ticket_id": "ov-epic", "title": "OV Epic", "ticket_type": "epic", "status": "open", "priority": 1, "parent_id": None}, ts)
    _write(base, "ov-story", 1, "CREATE", {"ticket_id": "ov-story", "title": "OV Story", "ticket_type": "story", "status": "open", "priority": 2, "parent_id": "ov-epic"}, ts + 1)
    _write(base, "ov-a", 1, "CREATE", {"ticket_id": "ov-a", "title": f"OV Task A - modifies {sf}", "ticket_type": "task", "status": "open", "priority": 2, "parent_id": "ov-story"}, ts + 2)
    _write(base, "ov-b", 1, "CREATE", {"ticket_id": "ov-b", "title": f"OV Task B - modifies {sf}", "ticket_type": "task", "status": "open", "priority": 2, "parent_id": "ov-story"}, ts + 3)


def _run(tracker: Path, impl: str, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "TICKETS_TRACKER_DIR": str(tracker), "REBAR_COMPUTE": impl, "REBAR_NO_SYNC": "1", "_TICKET_TEST_NO_SYNC": "1"}
    return subprocess.run([str(_DISPATCHER), "next-batch", *args], env=env, capture_output=True, text=True)


# ───────────────────────────── cross-impl byte parity ────────────────────────
_DETERMINISTIC_SCENARIOS = [
    ("three_tier_text", _three_tier, ["nb-epic"]),
    ("three_tier_json", _three_tier, ["nb-epic", "--output", "json"]),
    ("three_tier_limit1", _three_tier, ["nb-epic", "--limit=1"]),
    ("three_tier_limit0", _three_tier, ["nb-epic", "--limit=0"]),
    ("file_impact_text", _file_impact, ["fi-epic"]),
    ("file_impact_json", _file_impact, ["fi-epic", "--output", "json"]),
    ("missing_text", _three_tier, ["nope"]),
    ("missing_json", _three_tier, ["nope", "--output", "json"]),
    ("usage_noargs", _three_tier, []),
    ("bad_limit", _three_tier, ["nb-epic", "--limit=abc"]),
]


@pytest.mark.parametrize("name,builder,args", _DETERMINISTIC_SCENARIOS, ids=[s[0] for s in _DETERMINISTIC_SCENARIOS])
def test_cross_impl_byte_parity(tmp_path: Path, name: str, builder, args: list[str]):
    """bash orchestrator == python port, byte-for-byte (stdout, stderr, exit)."""
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    builder(tracker)
    b = _run(tracker, "bash", *args)
    p = _run(tracker, "python", *args)
    assert b.returncode == p.returncode, f"{name}: exit {b.returncode} vs {p.returncode}"
    assert b.stdout == p.stdout, f"{name}: stdout drift\nBASH:\n{b.stdout}\nPY:\n{p.stdout}"
    assert b.stderr == p.stderr, f"{name}: stderr drift\nBASH:\n{b.stderr}\nPY:\n{p.stderr}"


# ───────────────────────────── determinism pin ───────────────────────────────
def test_python_multi_overlap_is_deterministic(tmp_path: Path):
    """The Python port is byte-stable across runs on the multi-file-overlap case
    (the latent bash nondeterminism the port fixes). conflict_file is the
    lexicographically smallest claimed file ('rebar' < 'src/...sh')."""
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    _multi_overlap(tracker)
    outs = {_run(tracker, "python", "ov-epic").stdout for _ in range(6)}
    assert len(outs) == 1, f"non-deterministic output: {outs}"
    out = outs.pop()
    assert "SKIPPED_OVERLAP: ov-b\tdeferred (overlaps with ov-a on rebar)" in out
    # Batch composition is unambiguous regardless: exactly one of the pair runs.
    assert "TASK: ov-a" in out and "TASK: ov-b" not in out


def test_multi_overlap_batch_composition_matches_bash(tmp_path: Path):
    """Even where the diagnostic differs, the SET of batched vs skipped ids is the
    same across impls (the contract agents actually depend on)."""
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    _multi_overlap(tracker)
    b = json.loads(_run(tracker, "bash", "ov-epic", "--output", "json").stdout)
    p = json.loads(_run(tracker, "python", "ov-epic", "--output", "json").stdout)
    assert {e["id"] for e in b["batch"]} == {e["id"] for e in p["batch"]}
    assert {e["id"] for e in b["skipped_overlap"]} == {e["id"] for e in p["skipped_overlap"]}
