"""Regression coverage for recursively removable pytest temp trees."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from _nested_pytest import nested_basetemp, run_nested_pytest
from _subprocess_env import subprocess_env

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


def _clear_git_readonly_and_retry(func, path: str, exc: BaseException) -> None:
    """Retry a removal refused for git's own read-only object files, and nothing else.

    Git writes objects read-only and Windows refuses to delete a read-only file, so the
    nested run's ``repo/.git/objects`` would block a plain ``rmtree`` there. Those files
    are not what the fixture under test owns: a refusal anywhere outside a ``.git``
    directory, including the rebar snapshot itself, is re-raised so the test still fails.
    """
    if not isinstance(exc, PermissionError) or ".git" not in Path(path).parts:
        raise exc
    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    func(path)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def test_cross_module_snapshot_is_read_only(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path.parent / "adjacent.txt").write_text("keep\n", encoding="utf-8")
    _git("init", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test User", cwd=repo)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
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
    basetemp = nested_basetemp(tmp_path)
    result = run_nested_pytest(
        tmp_path,
        selected_node,
        "-p",
        "no:randomly",
        "-q",
        env=subprocess_env(),
        cwd=None,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert basetemp.is_dir()
    if selected_node.endswith("::test_cross_module_snapshot_is_read_only"):
        assert (basetemp / "adjacent.txt").read_text(encoding="utf-8") == "keep\n"

    if sys.version_info >= (3, 12):
        shutil.rmtree(basetemp, onexc=_clear_git_readonly_and_retry)
    else:  # onexc is 3.12+; onerror passes the exc_info triple instead
        shutil.rmtree(basetemp, onerror=lambda f, p, ei: _clear_git_readonly_and_retry(f, p, ei[1]))
    assert not basetemp.exists()


@pytest.mark.skipif(sys.version_info >= (3, 12), reason="Python 3.11 pathlib race")
def test_snapshot_cleanup_tolerates_disappearing_tracker_lock(
    tmp_path: Path, pytestconfig: pytest.Config
) -> None:
    tracker = tmp_path / "repo" / ".tickets-tracker"
    tracker.mkdir(parents=True)
    snapshot_root = tmp_path / "repo" / ".rebar" / "run_snapshots"
    (snapshot_root / "sha" / "result.json").parent.mkdir(parents=True)
    (snapshot_root / "sha" / "result.json").write_text("{}\n", encoding="utf-8")
    adjacent = tmp_path / "adjacent.txt"
    adjacent.write_text("keep\n", encoding="utf-8")

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
    assert adjacent.read_text(encoding="utf-8") == "keep\n"


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


@pytest.mark.skipif(sys.version_info >= (3, 12), reason="Python 3.11 pathlib flavour selection")
def test_snapshot_cleanup_ignores_ambient_path_flavour(
    tmp_path: Path,
    pytestconfig: pytest.Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_root = tmp_path / "repo" / ".rebar" / "run_snapshots"
    result = snapshot_root / "sha" / "result.json"
    result.parent.mkdir(parents=True)
    result.write_text("{}\n", encoding="utf-8")
    adjacent = tmp_path / "adjacent.txt"
    adjacent.write_bytes(b"keep\n")

    cleanup = _cleanup_fixture(pytestconfig)(tmp_path, None)
    next(cleanup)

    with monkeypatch.context() as ambient:
        ambient.setattr(os, "name", "nt")
        with pytest.raises(StopIteration):
            next(cleanup)

    assert not snapshot_root.exists()
    assert adjacent.read_bytes() == b"keep\n"
