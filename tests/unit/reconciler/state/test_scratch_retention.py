"""Tests for reconciler/scratch_retention.py — the ``.rebar/bridge/`` layout and its retention.

The retention bound is the point of the module: per-pass snapshots, manifests and sync logs
used to accumulate without limit at the repo root. These tests seed more history than the
bounds keep and check exactly what survives, that a failing directory never raises, and that
only a writing pass prunes.
"""

from __future__ import annotations

import datetime as dt
import logging
import shutil
import subprocess
from pathlib import Path

import pytest

from rebar.reconciler import persist_phase, reconcile, scratch_retention
from rebar.reconciler.reconcile_helpers import _NoOpSyncLogger

REPO_ROOT = Path(__file__).resolve().parents[4]
_PASS_IDS = [f"2026-01-0{day}T00-00-00" for day in range(1, 8)]
_ALERT_DAYS = [(dt.date(2026, 1, 1) + dt.timedelta(days=n)).isoformat() for n in range(35)]


def _seed(root: Path) -> None:
    """Write 7 passes of snapshots + manifests, 7 sync logs, 35 alert days, and the
    two cross-pass state files."""
    snapshots = scratch_retention.snapshots_dir(root)
    snapshots.mkdir(parents=True)
    for pass_id in _PASS_IDS:
        (snapshots / f"{pass_id}.json").write_text("{}", encoding="utf-8")
        (snapshots / f"{pass_id}.manifest.json").write_text("{}", encoding="utf-8")
        scratch_retention.sync_log_path(root, pass_id).write_text("", encoding="utf-8")
    alerts = scratch_retention.alerts_dir(root)
    alerts.mkdir()
    for day in _ALERT_DAYS:
        (alerts / f"{day}.jsonl").write_text("{}\n", encoding="utf-8")
    scratch_retention.mapping_path(root).write_text("{}", encoding="utf-8")
    scratch_retention.divergence_state_path(root).write_text("{}", encoding="utf-8")


def _names(directory: Path, pattern: str) -> list[str]:
    return sorted(p.name for p in directory.glob(pattern))


def test_layout_lives_under_dot_rebar_bridge(tmp_path: Path) -> None:
    root = tmp_path / ".rebar" / "bridge"
    assert scratch_retention.bridge_scratch_dir(tmp_path) == root
    assert scratch_retention.snapshots_dir(tmp_path) == root / "snapshots"
    assert scratch_retention.alerts_dir(tmp_path) == root / "bridge_alerts"
    assert scratch_retention.sync_log_path(tmp_path, "p1") == root / "sync-log-p1.jsonl"
    assert scratch_retention.mapping_path(tmp_path) == root / "mapping.json"
    assert scratch_retention.divergence_state_path(tmp_path) == root / "comment_id_divergence.json"


def test_prune_keeps_the_newest_and_reports_what_it_removed(tmp_path: Path) -> None:
    _seed(tmp_path)

    removed = scratch_retention.prune_bridge_scratch(tmp_path)

    assert removed == {"snapshots": 4, "sync_logs": 2, "alerts": 5}
    kept_passes = _PASS_IDS[-5:]
    snapshots = scratch_retention.snapshots_dir(tmp_path)
    assert _names(snapshots, "*.manifest.json") == [f"{p}.manifest.json" for p in kept_passes]
    assert sorted(
        p.name for p in snapshots.glob("*.json") if not p.name.endswith(".manifest.json")
    ) == [f"{p}.json" for p in kept_passes]
    assert _names(scratch_retention.bridge_scratch_dir(tmp_path), "sync-log-*.jsonl") == [
        f"sync-log-{p}.jsonl" for p in kept_passes
    ]
    assert _names(scratch_retention.alerts_dir(tmp_path), "*.jsonl") == [
        f"{day}.jsonl" for day in _ALERT_DAYS[-30:]
    ]
    assert scratch_retention.mapping_path(tmp_path).is_file()
    assert scratch_retention.divergence_state_path(tmp_path).is_file()


def test_a_second_prune_removes_nothing(tmp_path: Path) -> None:
    _seed(tmp_path)
    scratch_retention.prune_bridge_scratch(tmp_path)

    assert scratch_retention.prune_bridge_scratch(tmp_path) == {
        "snapshots": 0,
        "sync_logs": 0,
        "alerts": 0,
    }


def test_a_stray_manifest_temp_file_goes_with_its_pass(tmp_path: Path) -> None:
    _seed(tmp_path)
    snapshots = scratch_retention.snapshots_dir(tmp_path)
    stray = snapshots / f"{_PASS_IDS[0]}.k3j9x.json.tmp"
    stray.write_text("{", encoding="utf-8")

    removed = scratch_retention.prune_bridge_scratch(tmp_path, keep_passes=6)

    assert removed["snapshots"] == 3
    assert not stray.exists()
    assert (snapshots / f"{_PASS_IDS[1]}.json").is_file()


def test_missing_scratch_prunes_nothing(tmp_path: Path) -> None:
    assert scratch_retention.prune_bridge_scratch(tmp_path) == {
        "snapshots": 0,
        "sync_logs": 0,
        "alerts": 0,
    }


def test_unreadable_directories_return_zeros_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _seed(tmp_path)
    before = sorted(p for p in tmp_path.rglob("*") if p.is_file())

    def _denied(self: Path):
        raise PermissionError(13, "Permission denied", str(self))

    with (
        monkeypatch.context() as patched,
        caplog.at_level(logging.WARNING, logger=scratch_retention.__name__),
    ):
        patched.setattr(Path, "iterdir", _denied)
        removed = scratch_retention.prune_bridge_scratch(tmp_path)

    assert removed == {"snapshots": 0, "sync_logs": 0, "alerts": 0}
    assert sorted(p for p in tmp_path.rglob("*") if p.is_file()) == before
    assert sum("bridge scratch prune of" in r.getMessage() for r in caplog.records) == 3


def test_a_file_that_cannot_be_removed_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_path)
    stuck = scratch_retention.alerts_dir(tmp_path) / f"{_ALERT_DAYS[0]}.jsonl"
    real_unlink = Path.unlink

    def _unlink(self: Path, missing_ok: bool = False) -> None:
        if self == stuck:
            raise PermissionError(13, "Permission denied", str(self))
        real_unlink(self, missing_ok=missing_ok)

    with monkeypatch.context() as patched:
        patched.setattr(Path, "unlink", _unlink)
        removed = scratch_retention.prune_bridge_scratch(tmp_path)

    assert removed["alerts"] == 4
    assert stuck.is_file()


def _pass_ctx(root: Path, *, persist: bool) -> reconcile._PassContext:
    return reconcile._PassContext(
        pass_id=_PASS_IDS[-1],
        repo_root=root,
        persist=persist,
        sync_logger=_NoOpSyncLogger(),
        mutations=[],
        manifest_path=None,
        nowrite_plan=None if persist else {"applied_count": 0},
    )


def test_a_writing_pass_prunes_at_the_end_of_the_persist_phase(tmp_path: Path) -> None:
    _seed(tmp_path)

    persist_phase.persist_and_log(
        _pass_ctx(tmp_path, persist=True), save_and_commit_bindings=lambda _ctx: None
    )

    assert len(_names(scratch_retention.snapshots_dir(tmp_path), "*.manifest.json")) == 5
    assert len(_names(scratch_retention.bridge_scratch_dir(tmp_path), "sync-log-*")) == 5
    assert len(_names(scratch_retention.alerts_dir(tmp_path), "*.jsonl")) == 30


def test_a_preview_pass_does_not_prune(tmp_path: Path) -> None:
    _seed(tmp_path)
    before = sorted(p for p in tmp_path.rglob("*") if p.is_file())

    persist_phase.persist_and_log(
        _pass_ctx(tmp_path, persist=False), save_and_commit_bindings=lambda _ctx: None
    )

    assert sorted(p for p in tmp_path.rglob("*") if p.is_file()) == before


def test_bridge_scratch_is_ignored_by_the_repository_gitignore(tmp_path: Path) -> None:
    """The committed ``.rebar/*`` rule covers the scratch tree, so a pass leaves nothing
    untracked behind it."""
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True, capture_output=True)
    shutil.copyfile(REPO_ROOT / ".gitignore", tmp_path / ".gitignore")
    _seed(tmp_path)

    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert status.splitlines() == ["?? .gitignore"]
