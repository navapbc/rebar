"""REBAR_SYNC_PUSH=async survives the CLI parent's exit.

The async push detaches a child (``subprocess.Popen(start_new_session=True)``)
that re-runs the push in ``always`` mode and does the real ``git push`` *after*
the parent CLI process has returned. This pins the observable invariant:

* the parent ``create`` process exits promptly (returncode 0) WITHOUT blocking
  on the push, and
* while it is gone, the detached child is still mid-``git push`` (origin has NOT
  advanced), and
* once released, the EXACT event created by the parent lands on origin's
  ``tickets`` branch.

Determinism: the detached push child resolves ``git`` from ``PATH``
(``run_git`` invokes ``["git", "-C", base, …]``), so a ``git`` shim placed
first on ``PATH`` intercepts its ``git push`` and blocks it on a release file we
control. All waits are bounded polls gated on marker/release files, never sleeps
racing real work. Spawn-failure logging and PUSH_PENDING are covered elsewhere;
this test is only about parent-exit survival + the event landing.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

import rebar

# The shim is a POSIX ``sh`` script and start_new_session is POSIX-only.
pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="POSIX-only (sh shim + start_new_session)"
)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _origin_ref(origin: Path) -> str:
    """The origin's ``tickets`` branch tip, or ``"NONE"`` when unborn."""
    r = subprocess.run(
        ["git", "--git-dir", str(origin), "rev-parse", "--verify", "-q", "refs/heads/tickets"],
        capture_output=True,
        text=True,
    )
    return r.stdout.strip() if r.returncode == 0 else "NONE"


@pytest.fixture
def repo_with_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Path, Path]]:
    """An initialized rebar repo wired to a real local bare origin.

    Yields (repo, origin_git_dir).
    """
    origin = tmp_path / "origin.git"
    repo = tmp_path / "work"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(origin)],
        check=True,
        capture_output=True,
        text=True,
    )
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "t@t.co", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    _git("remote", "add", "origin", str(origin), cwd=repo)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    yield repo, origin


def _write_git_shim(bin_dir: Path) -> None:
    """Write an executable ``git`` shim that blocks only on ``git push``.

    Reads ``REAL_GIT`` / ``PUSH_ENTERED_MARKER`` / ``PUSH_RELEASE_FILE`` from the
    environment. On a ``push`` invocation it touches the entered-marker, spin-waits
    for the release file, then execs the real git; every other invocation execs the
    real git immediately (so the parent's own add/commit pass through untouched).
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "git"
    shim.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do\n'
        '  if [ "$a" = "push" ]; then\n'
        '    : > "$PUSH_ENTERED_MARKER"\n'
        '    while [ ! -e "$PUSH_RELEASE_FILE" ]; do sleep 0.05; done\n'
        '    exec "$REAL_GIT" "$@"\n'
        "  fi\n"
        "done\n"
        'exec "$REAL_GIT" "$@"\n'
    )
    shim.chmod(0o755)


def _wait_for(predicate, timeout: float, interval: float = 0.05) -> bool:
    """Poll ``predicate`` until true or ``timeout`` elapses; return the final value."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_async_push_survives_parent_exit_and_lands_event(
    repo_with_origin: tuple[Path, Path], tmp_path: Path
) -> None:
    repo, origin = repo_with_origin
    real_git = shutil.which("git")
    assert real_git, "a real git must be on PATH"

    bin_dir = tmp_path / "bin"
    entered = tmp_path / "push_entered"
    release = tmp_path / "push_release"
    _write_git_shim(bin_dir)

    before = _origin_ref(origin)
    assert before == "NONE"  # fresh bare origin has no tickets branch yet

    # NOTE: the title/description must contain NO literal "push" token, or the
    # parent's own git calls would falsely block in the shim.
    env = {
        **os.environ,
        "REBAR_ROOT": str(repo),
        "REBAR_SYNC_PUSH": "async",
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "REAL_GIT": real_git,
        "PUSH_ENTERED_MARKER": str(entered),
        "PUSH_RELEASE_FILE": str(release),
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "rebar.cli",
            "create",
            "task",
            "async parent exit",
            "--description",
            "verify the detached child completes the send after the parent returns; "
            "this description is padded to comfortably clear the clarity floor length.",
        ],
        cwd=repo,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # 2. The parent must return promptly WITHOUT blocking on the push.
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, f"parent create failed: rc={proc.returncode}\n{err}"

    # The CLI prints the full ticket id as its last non-empty stdout line.
    ticket_id = [ln.strip() for ln in out.splitlines() if ln.strip()][-1]
    assert ticket_id and "-" in ticket_id, f"could not parse ticket id from: {out!r}"

    # 3. The detached push child must reach `git push` and block in the shim.
    assert _wait_for(entered.exists, timeout=30), "detached push child never reached git push"

    # 4. THE CRUX — the parent has exited while the push is still in flight:
    #    parent returned, release not yet granted, origin NOT advanced.
    assert proc.returncode is not None  # parent has exited
    assert not release.exists()  # we have not released the push yet
    assert _origin_ref(origin) == before, "origin advanced before the push was released"

    # 5. Release the push and wait (bounded) for origin to advance.
    release.write_text("go")
    assert _wait_for(lambda: _origin_ref(origin) != before, timeout=15), (
        "async push never landed on origin after release"
    )

    # 6. The EXACT event the parent created must be present on origin's tickets tree.
    tree = subprocess.run(
        ["git", "--git-dir", str(origin), "ls-tree", "-r", "--name-only", "refs/heads/tickets"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert ticket_id in tree, f"ticket {ticket_id} not found on origin tickets branch:\n{tree}"
