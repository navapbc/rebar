"""A detached child must not inherit an ephemeral working directory (bug 3198-438c-72a5-470f).

rebar detaches three children that OUTLIVE the command that started them: the enrichment
drain (``llm.enrich_drain._spawn_detached_drain``), the async tickets-branch push
(``_store.push.push_tickets_branch``), and the compaction sweep
(``_commands.compact_trigger._spawn_detached_sweep``). Each documents that contract in its
own docstring ("a child that outlives the current command").

``subprocess.Popen`` without ``cwd=`` hands the child the PARENT's working directory. This
project's workflow runs ordinary writes from short-lived git worktrees which are then
removed — while the child is still working — so the child's inherited cwd stops existing and
the first ``os.getcwd()`` (``_config_sources.repo_root``'s final fallback, or any ``git``
startup) raises ``FileNotFoundError``. The child dies before claiming any work, which is not
"outliving the command" in any useful sense.

The durable answer is the store's own repo root, resolved through symlinks: a worktree's
``.tickets-tracker`` is a SYMLINK to the canonical store, so the UNRESOLVED parent is the very
worktree the child must not depend on.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from rebar._commands import compact_trigger
from rebar._store import push
from rebar.llm import enrich_drain

# Captured before any monkeypatching so the recorder below can still spawn for real.
_REAL_POPEN = subprocess.Popen

# A child that waits for a barrier file (so the spawning directory is provably gone before it
# looks), then reports whether it can resolve its own working directory. Absolute paths only:
# the probe must not need a live cwd to find its own arguments.
_PROBE = (
    "import os, sys, time\n"
    "go, out = sys.argv[1], sys.argv[2]\n"
    "deadline = time.time() + 30\n"
    "while not os.path.exists(go) and time.time() < deadline:\n"
    "    time.sleep(0.01)\n"
    "try:\n"
    "    result = 'CWD ' + os.getcwd()\n"
    "except OSError as exc:\n"
    "    result = 'ERR ' + type(exc).__name__\n"
    "with open(out, 'w') as fh:\n"
    "    fh.write(result)\n"
)


class _Recorder:
    """Stands in for ``subprocess.Popen`` at a detach site.

    It records the kwargs production code asked for AND launches a real child with the
    ``cwd`` production chose — ``None`` when it chose none, which is exactly the inheritance
    the bug is about. So the test exercises the real kernel behaviour, not a stub of it.
    """

    def __init__(self, probe: list[str] | None = None) -> None:
        self.kwargs: dict = {}
        self.proc: subprocess.Popen | None = None
        self._probe = probe

    def __call__(self, args, **kwargs):
        if not (kwargs.get("start_new_session") or kwargs.get("creationflags")):
            # Not a detach. ``subprocess.run`` is built on ``Popen``, so intercepting
            # everything would also hijack unrelated git probes; pass those straight through.
            return _REAL_POPEN(args, **kwargs)
        self.kwargs = kwargs
        cmd = self._probe if self._probe is not None else [sys.executable, "-c", ""]
        self.proc = _REAL_POPEN(
            cmd,
            cwd=kwargs.get("cwd"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        return self.proc


def _store(tmp_path: Path) -> tuple[Path, str]:
    """A canonical store: ``<root>/.tickets-tracker``. Returns ``(root, tracker)``."""
    root = tmp_path / "canonical-repo"
    tracker = root / ".tickets-tracker"
    tracker.mkdir(parents=True)
    return root, str(tracker)


def _worktree_view(tmp_path: Path, tracker: str, name: str = "ephemeral-worktree") -> Path:
    """An ephemeral worktree whose ``.tickets-tracker`` symlinks the canonical store, exactly
    as ``make worktree`` provisions one."""
    wt = tmp_path / name
    wt.mkdir()
    (wt / ".tickets-tracker").symlink_to(tracker)
    return wt


# --- the reported bug: the drain child dies when its spawning worktree is removed ----------


def test_detached_drain_child_survives_removal_of_the_spawning_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mechanism end to end, with real processes and a real ``rmtree``.

    RED before the fix: the child inherits the worktree cwd and reports
    ``ERR FileNotFoundError`` from ``os.getcwd()`` — the production traceback in
    ``.rebar/logs/enrich-drain.log``, reproduced.
    """
    root, tracker = _store(tmp_path)
    worktree = _worktree_view(tmp_path, tracker)
    go, out = tmp_path / "go", tmp_path / "probe.out"

    rec = _Recorder([sys.executable, "-c", _PROBE, str(go), str(out)])
    monkeypatch.setattr(subprocess, "Popen", rec)

    monkeypatch.chdir(worktree)
    assert Path.cwd() == Path(os.path.realpath(worktree))  # precondition: spawned from it
    enrich_drain._spawn_detached_drain(str(worktree / ".tickets-tracker"))

    monkeypatch.chdir(tmp_path)
    shutil.rmtree(worktree)
    assert not worktree.exists()  # precondition: the worktree is gone, the child is not
    go.write_text("go")

    assert rec.proc is not None
    rec.proc.wait(timeout=30)
    assert out.read_text() == "CWD " + os.path.realpath(root)


# --- the contract, across all three detach sites -------------------------------------------


def _spawn_drain(tracker: str) -> None:
    enrich_drain._spawn_detached_drain(tracker)


def _spawn_sweep(tracker: str) -> None:
    compact_trigger._spawn_detached_sweep(tracker)


def _spawn_push(tracker: str) -> None:
    push.push_tickets_branch(tracker)


_SITES = [
    pytest.param(_spawn_drain, id="enrich-drain"),
    pytest.param(_spawn_sweep, id="compact-sweep"),
    pytest.param(_spawn_push, id="async-push"),
]


@pytest.fixture
def _async_push(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put the push site on its detached branch without touching a real remote."""
    monkeypatch.setenv("REBAR_SYNC_PUSH", "async")
    monkeypatch.setattr(push, "_push_mode", lambda root=None: "async")
    monkeypatch.setattr(push, "_require_s3_helper_for_configured_remote", lambda base: None)


@pytest.mark.usefixtures("_async_push")
@pytest.mark.parametrize("spawn", _SITES)
def test_detached_child_cwd_is_the_store_root_not_the_callers_directory(
    spawn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Independence from the caller is proved by INVARIANCE: two different callers, in two
    different directories, must produce the same explicit ``cwd`` — the canonical store root.

    This is what makes the choice durable rather than merely different: any cwd derived from
    the caller is, by construction, as short-lived as the caller.
    """
    root, tracker = _store(tmp_path)
    seen = []
    for name in ("worktree-a", "worktree-b"):
        wt = _worktree_view(tmp_path, tracker, name)
        rec = _Recorder()
        # A nested context, not ``undo()``: ``undo()`` would also revert the ``_async_push``
        # fixture's patches, which share this test's monkeypatch instance.
        with monkeypatch.context() as m:
            m.setattr(subprocess, "Popen", rec)
            m.chdir(wt)
            spawn(str(wt / ".tickets-tracker"))
        assert rec.proc is not None, "the detach site did not spawn"
        rec.proc.wait(timeout=30)
        seen.append(rec.kwargs.get("cwd"))

    assert seen[0] == seen[1], "the child's cwd varies with the caller's directory"
    assert seen[0] == os.path.realpath(root)


# --- the helper's own contract --------------------------------------------------------------


def test_durable_cwd_resolves_a_worktree_symlink_to_the_canonical_store(tmp_path: Path) -> None:
    """The crux of durability: the tracker reached THROUGH a worktree must still yield the
    canonical root, or the child is merely pinned to the doomed worktree by name."""
    from rebar._proc import detached_child_cwd

    root, tracker = _store(tmp_path)
    worktree = _worktree_view(tmp_path, tracker)
    assert detached_child_cwd(str(worktree / ".tickets-tracker")) == os.path.realpath(root)


def test_durable_cwd_never_returns_a_directory_that_does_not_exist(tmp_path: Path) -> None:
    """Contrast case: when even the store root is gone, fall back to an ancestor that exists
    rather than hand the child another dead directory."""
    from rebar._proc import detached_child_cwd

    chosen = detached_child_cwd(str(tmp_path / "gone" / "also-gone" / ".tickets-tracker"))
    assert os.path.isdir(chosen)


def test_durable_cwd_of_a_live_store_is_its_repo_root_not_the_tracker(tmp_path: Path) -> None:
    """Negative control: the tracker itself is a live directory, so a naive "nearest existing
    directory" rule would stop there — and a child running INSIDE the tracker resolves the
    repo root to the tracker, which would then look for a tracker inside the tracker."""
    from rebar._proc import detached_child_cwd

    root, tracker = _store(tmp_path)
    assert detached_child_cwd(tracker) == os.path.realpath(root)
