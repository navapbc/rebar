"""Regression coverage for recursively removable pytest temp trees."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from rebar._store import lock
from rebar.llm.workflow import snapshot

_SNAPSHOT_TEST = Path(__file__).parent / "workflow" / "test_snapshot.py"
_ROOT_CONFTEST = Path(__file__).parents[1] / "conftest.py"


def _cleanup_fixture(pytestconfig: pytest.Config):
    root_conftest = next(
        plugin
        for plugin in pytestconfig.pluginmanager.get_plugins()
        if Path(getattr(plugin, "__file__", "")).resolve() == _ROOT_CONFTEST.resolve()
    )
    return root_conftest._remove_readonly_run_snapshots.__wrapped__


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def test_cross_module_snapshot_is_read_only(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path.parent / "adjacent.txt").write_text("keep\n")
    _git("init", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test User", cwd=repo)
    (repo / "a.py").write_text("x = 1\n")
    _git("add", "a.py", cwd=repo)
    _git("commit", "-m", "initial", cwd=repo)

    snapdir = snapshot.snapshot_at_ref("HEAD", str(repo))

    assert not os.access(snapdir / "a.py", os.W_OK)


@pytest.mark.parametrize(
    "selected_node",
    [
        f"{_SNAPSHOT_TEST}::test_snapshot_is_read_only",
        f"{Path(__file__)}::test_cross_module_snapshot_is_read_only",
    ],
    ids=["test_snapshot_is_read_only", "cross_module"],
)
def test_completed_snapshot_test_leaves_basetemp_removable(
    tmp_path: Path, selected_node: str
) -> None:
    basetemp = tmp_path / "nested-basetemp"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            selected_node,
            "--basetemp",
            str(basetemp),
            "-p",
            "no:randomly",
            "-p",
            "no:cacheprovider",
            "-q",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert basetemp.is_dir()
    if selected_node.endswith("::test_cross_module_snapshot_is_read_only"):
        assert (basetemp / "adjacent.txt").read_text() == "keep\n"

    shutil.rmtree(basetemp)
    assert not basetemp.exists()


@pytest.mark.skipif(sys.version_info >= (3, 12), reason="Python 3.11 pathlib race")
def test_snapshot_cleanup_tolerates_disappearing_tracker_lock(
    tmp_path: Path, pytestconfig: pytest.Config
) -> None:
    tracker = tmp_path / "repo" / ".tickets-tracker"
    tracker.mkdir(parents=True)
    snapshot_root = tmp_path / "repo" / ".rebar" / "run_snapshots"
    (snapshot_root / "sha" / "result.json").parent.mkdir(parents=True)
    (snapshot_root / "sha" / "result.json").write_text("{}\n")
    adjacent = tmp_path / "adjacent.txt"
    adjacent.write_text("keep\n")

    handle = lock.acquire(tracker, timeout=1, attempts=1)
    lock_dir = tracker / lock.MKDIR_LOCK_NAME
    assert lock_dir.is_dir()

    real_scandir = os.scandir
    released = False

    class _ReleaseAfterTrackerScan:
        def __init__(self, path: int | os.PathLike[str] | str) -> None:
            self.path = None if isinstance(path, int) else Path(path)
            self.inner = real_scandir(path)

        def __iter__(self):
            return self

        def __next__(self):
            return next(self.inner)

        def __enter__(self):
            self.inner.__enter__()
            return self

        def __exit__(self, *args):
            nonlocal released
            result = self.inner.__exit__(*args)
            if self.path == tracker and not released:
                handle.release()
                released = True
            return result

    cleanup = _cleanup_fixture(pytestconfig)(tmp_path, None)
    next(cleanup)
    try:
        with patch.object(os, "scandir", _ReleaseAfterTrackerScan):
            with pytest.raises(StopIteration):
                next(cleanup)
    finally:
        if not released:
            handle.release()

    assert released
    assert not snapshot_root.exists()
    assert adjacent.read_text() == "keep\n"


def test_snapshot_cleanup_does_not_hide_other_scan_errors(
    tmp_path: Path, pytestconfig: pytest.Config
) -> None:
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    real_scandir = os.scandir

    def fail_blocked_scan(path: os.PathLike[str] | str):
        if Path(path) == blocked:
            raise PermissionError("sentinel scan failure")
        return real_scandir(path)

    cleanup = _cleanup_fixture(pytestconfig)(tmp_path, None)
    next(cleanup)

    with patch.object(os, "scandir", side_effect=fail_blocked_scan):
        with pytest.raises(PermissionError, match="sentinel scan failure"):
            next(cleanup)
