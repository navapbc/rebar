"""A push failure must survive the process that suffered it (bug vapoury-attack-lamb).

Bug 2a76 made the terminal push-failure WARNING informative. That is a no-op on the
surfaces this ticket covers, because on them the warning is never DELIVERED:

* ``sync.push = async`` re-spawns the push as a detached child with ``stderr=DEVNULL``;
* a library embedder gets a ``NullHandler`` on the ``rebar`` root;
* an MCP client reads only the tool result;
* the reconciler installs its handler on a different logger root.

So the remedy is STATE, not a log line: a durable marker any later caller can read. These
tests drive REAL git against a REAL local bare origin whose REAL ``pre-receive`` hook
declines the push — no mocks, and no assertions on log output (that is exactly the channel
proven not to arrive).

The best-effort contract is load-bearing and must NOT be broken by any of this:
``push_tickets_branch`` still returns ``None`` and never raises on the default path
(``docs/concurrency.md``). ``test_marker_write_failure_does_not_fail_the_push`` guards the
inverse failure — a diagnostic that learns to crash its caller is worse than no diagnostic.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

from rebar._store import push, push_state

pytestmark = pytest.mark.unit

# A faithful GH013 push-protection decline: the `remote:` lines are the server's, and the
# `! [remote rejected] ... (pre-receive hook declined)` porcelain line is git's own.
_DECLINE_HOOK = """\
#!/bin/sh
if [ -f "$ALLOW_FLAG" ]; then exit 0; fi
echo "remote: error: GH013: Repository rule violations found for refs/heads/tickets." >&2
echo "remote: - Push cannot contain secrets" >&2
echo "remote:   locations:" >&2
echo "remote:     - commit: 0a09e51c9f" >&2
echo "remote:       path: 129e-2d88-cce2-492c/evidence-COMMENT.json:1" >&2
exit 1
"""


def _git(d: Path, *a: str) -> subprocess.CompletedProcess[str]:
    r = subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True)
    assert r.returncode == 0, f"git {' '.join(a)} failed: {r.stderr}"
    return r


@pytest.fixture
def rejecting_origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A tracker whose origin DECLINES every push, with one unpushed local commit.

    Returns ``(tracker, allow_flag)``; creating ``allow_flag`` makes the hook start
    accepting, which is how the marker-clears case stops the outage for real rather than
    by reconfiguring the client.
    """
    origin = tmp_path / "origin.git"
    tracker = tmp_path / "tracker"
    allow_flag = tmp_path / "allow"

    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True, capture_output=True)
    tracker.mkdir()
    _git(tracker, "init", "-q")
    _git(tracker, "config", "user.email", "t@e.com")
    _git(tracker, "config", "user.name", "T")
    _git(tracker, "config", "gc.auto", "0")
    _git(tracker, "remote", "add", "origin", str(origin))

    # Seed a shared base and publish it, so origin/tickets EXISTS — the incident shape (a
    # diverging branch), not a first-push-of-a-new-branch.
    (tracker / "seed.json").write_text("{}\n")
    _git(tracker, "add", "seed.json")
    _git(tracker, "commit", "-q", "-m", "seed")
    _git(tracker, "push", "-q", "origin", "HEAD:tickets")
    _git(tracker, "fetch", "-q", "origin", "+refs/heads/tickets:refs/remotes/origin/tickets")

    hook = origin / "hooks" / "pre-receive"
    hook.write_text(_DECLINE_HOOK)
    hook.chmod(0o755)
    _git(origin, "config", "core.hooksPath", str(origin / "hooks"))

    # One local-only commit: the write whose push will be rejected.
    (tracker / "evidence.json").write_text('{"body": "written during the outage"}\n')
    _git(tracker, "add", "evidence.json")
    _git(tracker, "commit", "-q", "-m", "ticket: COMMENT evidence")

    monkeypatch.setenv("REBAR_SYNC_PUSH", "always")
    monkeypatch.setenv("ALLOW_FLAG", str(allow_flag))
    return tracker, allow_flag


def _marker(tracker: Path) -> Path:
    return tracker / ".git" / push_state.MARKER


def test_a_declined_push_is_recorded_as_durable_state(
    rejecting_origin: tuple[Path, Path],
) -> None:
    """AC1/AC3 core: the failure outlives the process, naming the git reason.

    This is the whole remedy. A warning is deliverable on exactly one of the four
    surfaces; a file on disk is deliverable on all of them.
    """
    tracker, _ = rejecting_origin
    assert not _marker(tracker).exists(), "fixture started with a marker already present"

    push.push_tickets_branch(str(tracker))

    status = push_state.read_status(str(tracker))
    assert status["state"] == "pending", (
        "a REJECTED push left no durable record; every caller that is not watching this "
        f"process's stderr still cannot tell the events are unpushed. Got: {status}"
    )
    assert "GH013" in status["detail"] or "declined" in status["detail"], (
        f"the recorded detail does not name the git rejection reason. Got: {status['detail']!r}"
    )
    assert status["unpushed"] == "1", f"the backlog count was not recorded: {status}"
    assert status["reason"], "no classification recorded"


def test_the_marker_clears_once_a_push_lands(rejecting_origin: tuple[Path, Path]) -> None:
    """AC4: the signal must not latch on past the outage it describes.

    A pending flag that never clears is one an operator learns to ignore, which would make
    the fix worse than the defect. The remote is really un-armed here (the hook starts
    accepting), so the clear is driven by a push that genuinely landed.
    """
    tracker, allow_flag = rejecting_origin
    push.push_tickets_branch(str(tracker))
    assert push_state.read_status(str(tracker))["state"] == "pending", "setup did not go pending"

    allow_flag.write_text("")  # the outage ends, server-side
    push.push_tickets_branch(str(tracker))

    assert push_state.read_status(str(tracker)) == {"state": "ok"}
    assert not _marker(tracker).exists(), "the marker file survived a successful push"


def test_the_async_path_records_the_failure_its_stderr_discards(
    rejecting_origin: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1: ``sync.push=async`` is the limb where NOTHING arrived — measured exit 0 with
    completely empty stderr while the backlog grew.

    The parent detaches the push into a child whose stdout/stderr are ``/dev/null``, so the
    child's warning is discarded by the OS and cannot be captured by any caplog. The child
    now records the marker instead, which is what makes this limb observable at all. This
    drives the REAL detached ``subprocess.Popen`` path, then waits for the file to appear.
    """
    tracker, _ = rejecting_origin
    monkeypatch.setenv("REBAR_SYNC_PUSH", "async")

    push.push_tickets_branch(str(tracker))

    marker = _marker(tracker)
    deadline = __import__("time").time() + 60
    while __import__("time").time() < deadline and not marker.exists():
        __import__("time").sleep(0.1)

    assert marker.exists(), (
        "the DETACHED async push left no trace: its stderr goes to /dev/null, so with no "
        "marker the failure is undeliverable to every caller on this path"
    )
    recorded = json.loads(marker.read_text())
    assert recorded["state"] == "pending"
    assert "GH013" in recorded["detail"] or "declined" in recorded["detail"]


def test_a_disabled_or_detached_push_is_not_itself_reported_as_a_failure(
    rejecting_origin: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two NON-failure delivery reasons must not raise the alarm.

    ``sync.push=off`` is an operator's choice and ``async-delivery-unobservable`` is raised
    in the PARENT the instant it hands off to a child that has not failed yet. Recording
    either would leave a healthy store permanently pending — the false-positive that makes
    an alarm worth ignoring. Asserted against the reason set directly so a future reason
    added to ``NON_FAILURE_REASONS`` is covered too.
    """
    tracker, _ = rejecting_origin
    for reason in sorted(push_state.NON_FAILURE_REASONS):
        push_state.record_failure(str(tracker), reason, "detail", "origin/tickets")
        assert push_state.read_status(str(tracker)) == {"state": "ok"}, (
            f"the non-failure reason {reason!r} was recorded as a delivery failure"
        )

    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    push.push_tickets_branch(str(tracker))
    assert not _marker(tracker).exists(), "sync.push=off recorded a spurious pending push"


def test_the_best_effort_contract_survives(rejecting_origin: tuple[Path, Path]) -> None:
    """AC5: recording is a SIGNAL, never an exception. The contract is authoritative
    intent (``docs/concurrency.md``), so this guards the opposite failure from the rest of
    the file and must pass both before and after the fix."""
    tracker, _ = rejecting_origin
    head_before = _git(tracker, "rev-parse", "HEAD").stdout.strip()

    assert push.push_tickets_branch(str(tracker)) is None, "a rejected push stopped returning None"

    assert _git(tracker, "rev-parse", "HEAD").stdout.strip() == head_before, "HEAD moved"
    assert _git(tracker, "rev-list", "--count", "origin/tickets..HEAD").stdout.strip() == "1", (
        "the local commit was not left intact"
    )


def test_marker_write_failure_does_not_fail_the_push(
    rejecting_origin: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC5: an unwritable marker must degrade to "no status", never to a crash.

    A diagnostic that can fail its caller is a regression on a path whose entire contract is
    "never fails the caller". Driven by making the real ``atomic_write`` raise, not by
    stubbing ``record_failure`` out — the point is that the swallow is real.
    """
    tracker, _ = rejecting_origin

    def _explode(*_a: object, **_k: object) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr("rebar._store.fsutil.atomic_write", _explode)

    assert push.push_tickets_branch(str(tracker)) is None
    assert push_state.read_status(str(tracker)) == {"state": "ok"}


def test_a_corrupt_marker_reports_ok_rather_than_a_phantom_outage(tmp_path: Path) -> None:
    """A broken diagnostic must not convince a caller that a healthy store is broken."""
    tracker = tmp_path / "t"
    (tracker / ".git").mkdir(parents=True)
    (tracker / ".git" / push_state.MARKER).write_text("{not json")
    assert push_state.read_status(str(tracker)) == {"state": "ok"}


def test_the_marker_follows_a_linked_worktrees_gitdir_pointer(tmp_path: Path) -> None:
    """``<tracker>/.git`` is a FILE in a linked worktree, not a directory.

    Resolving it naively would write the marker into a path that is not the git dir (or
    fail), so a worktree-mounted tracker would silently lose the signal. Resolved without a
    git subprocess, since this runs on every push.
    """
    tracker = tmp_path / "wt"
    real_git = tmp_path / "real.git"
    real_git.mkdir()
    tracker.mkdir()
    (tracker / ".git").write_text(f"gitdir: {real_git}\n")

    push_state.record_failure(str(tracker), "final-push-rejected", "declined", "origin/tickets")

    assert (real_git / push_state.MARKER).exists(), "the gitdir: pointer was not followed"
    assert push_state.read_status(str(tracker))["state"] == "pending"


def test_the_marker_never_touches_the_tracker_working_tree(
    rejecting_origin: tuple[Path, Path],
) -> None:
    """The marker must be invisible to git, not merely untracked.

    Two invariants ride on this. A record that the remote is unreachable must not itself
    need the remote, so it can never be committable. And ``push.py`` sets a dirty working
    tree ASIDE (stash-commit -> merge -> restore) to reconcile a non-fast-forward: a marker
    in the working tree would perturb exactly the state that dance operates on, and would
    surface as an untracked file in every store on earth. Living in the git dir makes both
    structurally impossible rather than merely unlikely.
    """
    tracker, _ = rejecting_origin
    status_before = _git(tracker, "status", "--porcelain=v1").stdout

    push.push_tickets_branch(str(tracker))
    assert _marker(tracker).exists(), "setup did not record a marker"

    assert _git(tracker, "status", "--porcelain=v1").stdout == status_before, (
        "the marker changed the tracker's working-tree status; it must live in the git dir"
    )
    tracked = subprocess.run(
        ["git", "-C", str(tracker), "ls-files", "--error-unmatch", push_state.MARKER],
        capture_output=True,
        text=True,
    )
    assert tracked.returncode != 0, "the push-pending marker is TRACKED by the tickets branch"


def test_the_library_surface_reports_it_with_no_logging_handler(
    rejecting_origin: tuple[Path, Path],
) -> None:
    """AC3: an in-process embedder gets a ``NullHandler``, so the warning goes nowhere.

    ``rebar.push_status()`` is the read side that needs no handler and no git subprocess.
    """
    import rebar

    tracker, _ = rejecting_origin
    push.push_tickets_branch(str(tracker))

    status = rebar.push_status(str(tracker))
    assert status["state"] == "pending", (
        "a library embedder still has no way to learn the push was rejected"
    )
    assert "GH013" in status["detail"] or "declined" in status["detail"]


def test_push_state_is_importable_without_the_mcp_extra() -> None:
    """The marker is store-level, so it must not drag in an optional dependency."""
    probe = (
        "import sys; sys.modules['mcp'] = None; "
        "from rebar._store import push_state; print(push_state.MARKER)"
    )
    run = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, env=subprocess_env({})
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == push_state.MARKER
