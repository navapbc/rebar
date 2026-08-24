"""Observable contracts for layered tracker-footprint measurement."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from rebar._store import footprint


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _standalone_tracker(tmp_path: Path) -> Path:
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    _git(tracker, "init", "-q", "-b", "tickets")
    _git(tracker, "config", "user.email", "test@example.com")
    _git(tracker, "config", "user.name", "Test")
    (tracker / "alpha").write_bytes(b"abc")
    (tracker / "beta").write_bytes(b"12345")
    _git(tracker, "add", "alpha", "beta")
    _git(tracker, "commit", "-q", "-m", "seed")
    _git(tracker, "gc", "--prune=now")
    return tracker


def _availability_value(field: object) -> int:
    assert isinstance(field, dict)
    assert set(field) == {"value"}
    value = field["value"]
    assert isinstance(value, int)
    return value


def _file_totals(root: Path, *, exclude_dot_git: bool = False) -> tuple[int, int, int]:
    logical = allocated = count = 0
    seen: set[tuple[int, int]] = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        if exclude_dot_git and Path(dirpath) == root and ".git" in dirnames:
            dirnames.remove(".git")
        for name in filenames:
            if exclude_dot_git and Path(dirpath) == root and name == ".git":
                continue
            stat = os.lstat(Path(dirpath) / name)
            logical += stat.st_size
            count += 1
            inode = (stat.st_dev, stat.st_ino)
            if inode not in seen:
                seen.add(inode)
                allocated += stat.st_blocks * 512
    return logical, allocated, count


def test_measure_tracker_reports_exact_standalone_layers(tmp_path: Path) -> None:
    tracker = _standalone_tracker(tmp_path)

    report = footprint.measure_tracker(
        tracker,
        remote="origin",
        branch="tickets",
        mode="mounted",
    )

    assert report["mode"] == "mounted"
    assert report["source"] == {
        "remote": "origin",
        "branch": "tickets",
        "requested_ref": "origin/tickets",
        "measured_ref": "refs/heads/tickets",
        "tip": _git(tracker, "rev-parse", "HEAD"),
    }
    assert report["object_database"] == {"scope": "standalone", "shared_reasons": []}

    layers = report["layers"]
    assert isinstance(layers, dict)
    checkout = layers["checkout"]
    git_directory = layers["git_directory"]
    pack = layers["pack"]
    whole = layers["whole_clone"]
    assert isinstance(checkout, dict)
    assert isinstance(git_directory, dict)
    assert isinstance(pack, dict)
    assert isinstance(whole, dict)

    checkout_logical, checkout_allocated, checkout_files = _file_totals(
        tracker, exclude_dot_git=True
    )
    git_logical, git_allocated, git_files = _file_totals(tracker / ".git")
    expected_pack = sum(
        path.stat().st_size for path in (tracker / ".git/objects/pack").glob("*.pack")
    )

    assert checkout["logical_bytes"] == checkout_logical == 8
    assert checkout["file_count"] == checkout_files == 2
    assert _availability_value(checkout["allocated_bytes"]) == checkout_allocated
    assert _availability_value(checkout["allocation_overhead_bytes"]) == (
        checkout_allocated - checkout_logical
    )
    assert pack == {
        "logical_bytes": expected_pack,
        "file_count": len(list((tracker / ".git/objects/pack").glob("*.pack"))),
        "scope": "standalone",
        "complete": True,
    }
    assert git_directory["logical_bytes"] == git_logical
    assert git_directory["file_count"] == git_files
    assert _availability_value(git_directory["allocated_bytes"]) == git_allocated
    assert whole["logical_bytes"] == checkout_logical + git_logical
    assert whole["file_count"] == checkout_files + git_files
    assert _availability_value(whole["allocated_bytes"]) == checkout_allocated + git_allocated
    assert whole["scope"] == "standalone"
    assert set(report["definitions"]) == {
        "logical_bytes",
        "allocated_bytes",
        "allocation_overhead_bytes",
        "pack",
        "whole_clone",
    }

    (tracker / "one-byte-control").write_bytes(b"x")
    changed = footprint.measure_tracker(
        tracker,
        remote="origin",
        branch="tickets",
        mode="mounted",
    )
    changed_checkout = changed["layers"]["checkout"]  # type: ignore[index]
    assert changed_checkout["logical_bytes"] == checkout_logical + 1
    assert changed_checkout["file_count"] == checkout_files + 1


def test_missing_st_blocks_is_reported_as_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker = _standalone_tracker(tmp_path)
    real_lstat = footprint._lstat

    def lstat_without_blocks(path: Path) -> SimpleNamespace:
        info = real_lstat(path)
        return SimpleNamespace(
            st_mode=info.st_mode,
            st_size=info.st_size,
            st_dev=info.st_dev,
            st_ino=info.st_ino,
        )

    monkeypatch.setattr(footprint, "_lstat", lstat_without_blocks)

    report = footprint.measure_tracker(
        tracker,
        remote="origin",
        branch="tickets",
    )

    unavailable = {"unavailable": {"reason": "filesystem stat does not expose st_blocks"}}
    layers = report["layers"]
    assert isinstance(layers, dict)
    for name in ("checkout", "git_directory", "whole_clone"):
        layer = layers[name]
        assert isinstance(layer, dict)
        assert layer["allocated_bytes"] == unavailable
        assert layer["allocation_overhead_bytes"] == unavailable


def test_linked_worktree_labels_shared_object_database(tmp_path: Path) -> None:
    tracker = _standalone_tracker(tmp_path)
    linked = tmp_path / "linked"
    _git(tracker, "worktree", "add", "-q", "-b", "linked", str(linked))

    report = footprint.measure_tracker(
        linked,
        remote="origin",
        branch="tickets",
    )

    assert report["object_database"] == {
        "scope": "shared",
        "shared_reasons": ["linked-worktree"],
    }
    layers = report["layers"]
    assert isinstance(layers, dict)
    assert layers["pack"]["scope"] == "shared"  # type: ignore[index]
    assert layers["whole_clone"]["scope"] == "shared"  # type: ignore[index]
    # A linked worktree measures the real common-dir pack, so it stays complete.
    assert layers["pack"]["complete"] is True  # type: ignore[index]


def test_alternates_label_the_object_database_as_shared(tmp_path: Path) -> None:
    tracker = _standalone_tracker(tmp_path)
    shared_clone = tmp_path / "shared-clone"
    _git(tmp_path, "clone", "-q", "--shared", str(tracker), str(shared_clone))

    report = footprint.measure_tracker(
        shared_clone,
        remote="origin",
        branch="tickets",
    )

    assert report["object_database"] == {
        "scope": "shared",
        "shared_reasons": ["alternates"],
    }
    layers = report["layers"]
    assert isinstance(layers, dict)
    assert layers["pack"]["scope"] == "shared"  # type: ignore[index]
    assert layers["whole_clone"]["scope"] == "shared"  # type: ignore[index]
    # Objects live in the borrowed alternate object database, so the primary-only pack
    # measurement is explicitly non-exclusive and must not be read as the whole object store.
    assert layers["pack"]["complete"] is False  # type: ignore[index]


def test_allocated_bytes_reflect_injected_block_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Allocation is a deterministic function of injected st_blocks, not the host FS policy."""

    tracker = _standalone_tracker(tmp_path)
    for index in range(8):
        (tracker / f"small-{index:02d}").write_bytes(b"x")

    real_lstat = footprint._lstat
    fixed_blocks = 8  # 8 * 512 == 4096 allocated bytes regardless of logical size

    def lstat_fixed_blocks(path: Path) -> SimpleNamespace:
        info = real_lstat(path)
        return SimpleNamespace(
            st_mode=info.st_mode,
            st_size=info.st_size,
            st_dev=info.st_dev,
            st_ino=info.st_ino,
            st_blocks=fixed_blocks,
        )

    monkeypatch.setattr(footprint, "_lstat", lstat_fixed_blocks)

    report = footprint.measure_tracker(tracker, remote="origin", branch="tickets")
    checkout = report["layers"]["checkout"]  # type: ignore[index]
    assert isinstance(checkout, dict)

    logical = checkout["logical_bytes"]
    allocated = _availability_value(checkout["allocated_bytes"])
    assert isinstance(logical, int)
    assert allocated == checkout["file_count"] * fixed_blocks * 512
    assert _availability_value(checkout["allocation_overhead_bytes"]) == allocated - logical
    assert allocated > logical


def test_hardlinked_inode_is_charged_once_for_allocated_bytes(tmp_path: Path) -> None:
    tracker = _standalone_tracker(tmp_path)
    baseline = footprint.measure_tracker(
        tracker,
        remote="origin",
        branch="tickets",
    )
    baseline_checkout = baseline["layers"]["checkout"]  # type: ignore[index]
    assert isinstance(baseline_checkout, dict)

    os.link(tracker / "alpha", tracker / "alpha-link")
    changed = footprint.measure_tracker(
        tracker,
        remote="origin",
        branch="tickets",
    )
    changed_checkout = changed["layers"]["checkout"]  # type: ignore[index]
    assert isinstance(changed_checkout, dict)

    assert changed_checkout["logical_bytes"] == baseline_checkout["logical_bytes"] + 3
    assert changed_checkout["file_count"] == baseline_checkout["file_count"] + 1
    assert changed_checkout["allocated_bytes"] == baseline_checkout["allocated_bytes"]
