"""Held-out coupling regression for RP-04 C3b (store/snapshot/io cutover).

The dangerous cut in this slice is ``push.py``'s ``load_config().sync.push`` read. It is
resolved **live per push**, and ``_io/import_ndjson.py`` mutates ``REBAR_SYNC_PUSH`` in the
process env mid-import (``=off`` while interior events are written, restored for one final
push) precisely so those live reads defer per-event delivery during a bulk import.

If the cutover routes ``push.py`` through a value composed ONCE at the seam (cached /
threaded), it stops observing that mid-flight mutation and the import's push-deferral
silently breaks -- every interior write would push. This test pins the observable contract:
a ``REBAR_SYNC_PUSH`` change made BETWEEN two ``push_tickets_branch`` calls MUST be observed
by the second call.

Observed SERVER-SIDE (a real bare origin with a pre-receive hook that logs every push
attempt), not via a call-count spy on an internal function -- so a behaviour-preserving
rename/extraction of the resolver cannot fool it, and only a real change in delivery
behaviour turns it red.

RED if ``push.py`` is cut to a compose-once value: the second push (under
``REBAR_SYNC_PUSH=off``) would still hit the remote, so the hook would log two attempts.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rebar._store import push

pytestmark = pytest.mark.unit

# A pre-receive hook that ACCEPTS every push but records one line per real attempt, so
# "how many pushes actually reached the remote" is measured from the server side.
_LOGGING_HOOK = """\
#!/bin/sh
echo "push" >> "$PUSH_LOG"
exit 0
"""


def _git(d: Path, *a: str) -> subprocess.CompletedProcess[str]:
    r = subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"git {' '.join(a)} failed: {r.stderr}")
    return r


def _bare_git(d: Path, *a: str) -> None:
    r = subprocess.run(["git", "--git-dir", str(d), *a], capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"git {' '.join(a)} failed: {r.stderr}")


@pytest.fixture
def tracker_with_logging_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    """A tracker whose origin ACCEPTS pushes and logs each attempt to ``push.log``.

    ``sync.push=always`` is set via the tracker's own config file so the ONLY thing that
    can flip delivery off is the ``REBAR_SYNC_PUSH`` env override -- which is exactly the
    coupling under test.
    """
    origin = tmp_path / "origin.git"
    tracker = tmp_path / "tracker"
    push_log = tmp_path / "push.log"
    push_log.touch()

    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True, capture_output=True)
    tracker.mkdir()
    _git(tracker, "init", "-q")
    _git(tracker, "config", "user.email", "t@e.com")
    _git(tracker, "config", "user.name", "T")
    _git(tracker, "config", "gc.auto", "0")
    _git(tracker, "config", "sync.push", "always")
    _git(tracker, "remote", "add", "origin", str(origin))

    (tracker / "seed.json").write_text("{}\n")
    _git(tracker, "add", "seed.json")
    _git(tracker, "commit", "-q", "-m", "seed")
    _git(tracker, "push", "-q", "origin", "HEAD:tickets")
    _git(tracker, "fetch", "-q", "origin", "+refs/heads/tickets:refs/remotes/origin/tickets")

    hook = origin / "hooks" / "pre-receive"
    hook.write_text(_LOGGING_HOOK)
    hook.chmod(0o755)
    _bare_git(origin, "config", "core.hooksPath", str(origin / "hooks"))

    monkeypatch.setenv("PUSH_LOG", str(push_log))
    # Ensure no ambient override is leaking in from the runner.
    monkeypatch.delenv("REBAR_SYNC_PUSH", raising=False)
    return tracker, push_log


def _commit(tracker: Path, name: str) -> None:
    (tracker / name).write_text('{"body": "x"}\n')
    _git(tracker, "add", name)
    _git(tracker, "commit", "-q", "-m", f"ticket: COMMENT {name}")


def _pushes(push_log: Path) -> int:
    return sum(1 for line in push_log.read_text().splitlines() if line.strip())


def test_push_mode_is_resolved_live_so_a_midflight_off_defers(
    tracker_with_logging_origin: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker, push_log = tracker_with_logging_origin

    # First write with sync.push=always (no env override): it must actually deliver.
    _commit(tracker, "evidence1.json")
    push.push_tickets_branch(str(tracker))
    assert _pushes(push_log) == 1, "the first push (sync.push=always) must reach the remote"

    # Now flip the env override OFF *between* pushes -- exactly what import_ndjson does
    # around its interior writes. A live resolver must observe it and defer.
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    _commit(tracker, "evidence2.json")
    push.push_tickets_branch(str(tracker))
    assert _pushes(push_log) == 1, (
        "a REBAR_SYNC_PUSH=off set BETWEEN pushes must be observed by push.py (live "
        "per-push resolution); a compose-once cut would deliver anyway and log a 2nd push"
    )

    # Restore to always -- the final push (as import does) delivers again.
    monkeypatch.setenv("REBAR_SYNC_PUSH", "always")
    _commit(tracker, "evidence3.json")
    push.push_tickets_branch(str(tracker))
    assert _pushes(push_log) == 2, "restoring sync.push=always must deliver the final push"
