"""Unit tests for WS-C3 run-identity / determinism-capture / TTL sweep (no store)."""

from __future__ import annotations

import os

import pytest

from rebar.llm.workflow import executor as ex

jsonschema = pytest.importorskip("jsonschema")


def test_new_run_id_is_unique_and_sortable() -> None:
    a, b = ex.new_run_id(), ex.new_run_id()
    assert a != b
    ts_a, _, hex_a = a.partition("-")
    assert ts_a.isdigit() and len(hex_a) == 32


def test_run_id_defaults_when_omitted() -> None:
    doc = {"schema_version": "1", "name": "t", "steps": [{"id": "a", "uses": "e"}]}
    res = ex.run_workflow(doc, scripted_registry={"e": lambda c: {}})
    assert res.run_id  # auto-generated
    assert "-" in res.run_id


def test_capture_persisted_in_step_record() -> None:
    doc = {"schema_version": "1", "name": "t", "steps": [{"id": "a", "uses": "e"}]}
    rec = ex.MemoryRecorder()
    ex.run_workflow(doc, run_id="R", scripted_registry={"e": lambda c: {}}, recorder=rec)
    # A step emits a 'running' progress marker then the final marker; the capture
    # lives on the completed (succeeded) record.
    step = next(s for s in rec.steps if s["step_id"] == "a" and s["status"] == "succeeded")
    assert isinstance(step["captured"]["now_ns"], int)
    assert len(step["captured"]["uuid"]) == 32
    assert isinstance(step["captured"]["seed"], int)


def test_step_sees_captured_values() -> None:
    seen = {}

    def grab(ctx):
        seen["cap"] = dict(ctx.captured)
        return {}

    doc = {"schema_version": "1", "name": "t", "steps": [{"id": "a", "uses": "grab"}]}
    ex.run_workflow(doc, scripted_registry={"grab": grab})
    assert "now_ns" in seen["cap"] and "uuid" in seen["cap"]


def test_sweep_removes_only_old_snapshots(tmp_path) -> None:
    root = tmp_path / ex.SNAPSHOT_DIR_NAME
    root.mkdir(parents=True)
    old = root / "old-run"
    new = root / "new-run"
    old.mkdir()
    new.mkdir()
    # Backdate the old one well past the TTL.
    old_time = 1000.0
    os.utime(old, (old_time, old_time))
    removed = ex.sweep_orphan_snapshots(str(tmp_path), ttl_seconds=3600)
    assert str(old) in removed
    assert not old.exists()
    assert new.exists()  # fresh one kept


def test_sweep_missing_dir_is_noop(tmp_path) -> None:
    assert ex.sweep_orphan_snapshots(str(tmp_path)) == []
