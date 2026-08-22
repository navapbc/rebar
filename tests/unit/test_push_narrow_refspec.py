"""The push retry loop must converge under a NARROW clone refspec (bug 35f7).

Sibling of bug 5546 (fixed in ``_store/sync.py`` by ``2a3abe6ab``), same mechanism, a
different site. ``push_tickets_branch``'s non-fast-forward retry loop fetches the remote
branch and then reconciles with ``git merge <remote>/<branch>``. A bare
``git fetch <remote> <branch>`` always writes ``FETCH_HEAD`` but writes
``refs/remotes/<remote>/<branch>`` only OPPORTUNISTICALLY — when the remote's CONFIGURED
refspec covers that branch. A single-branch clone configures
``+refs/heads/main:refs/remotes/origin/main``, which does not cover ``tickets``, so the
fetch exits 0 having left the remote-tracking ref ABSENT (or, when it already exists,
STALE). The merge target therefore never advances, the loop cannot absorb the history that
rejected the push, and every retry re-pushes the same rejected commits until the budget is
exhausted — the push never lands and the competing writer's events are never adopted.

Both shapes are pinned because they fail differently and are reached differently:

* **absent** — a fresh single-branch clone that never resolved the ticket branch;
* **stale** — the ref was created once by an explicit refspec and a later bare fetch left
  it pinned to that first snapshot.

The control (``test_push_retry_converges_under_wildcard_refspec``) runs the identical
scenario with only the configured refspec changed, so a failure of the narrow cases
isolates the refspec rather than the retry budget or a genuine merge conflict.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

import rebar
from rebar._store import push

NARROW_REFSPEC = "+refs/heads/main:refs/remotes/origin/main"
WILDCARD_REFSPEC = "+refs/heads/*:refs/remotes/origin/*"

_AC = "Body.\n\n## Acceptance Criteria\n- [ ] x"


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=check
    )


def _rev(cwd: Path, ref: str) -> str:
    return _git(cwd, "rev-parse", "--verify", ref, check=False).stdout.strip()


@pytest.fixture
def repo_with_origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A rebar repo whose tickets store pushes to a real local bare origin."""
    origin = tmp_path / "origin.git"
    repo = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True, capture_output=True)
    repo.mkdir()
    _git(repo.parent, "init", "-q", str(repo))
    _git(repo, "config", "user.email", "t@t.co")
    _git(repo, "config", "user.name", "t")
    _git(repo, "remote", "add", "origin", str(origin))
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    rebar.create_ticket("task", "seed", description=_AC, repo_root=str(repo))
    return repo


def _competing_push(tmp_path: Path, repo: Path) -> None:
    """A concurrent writer lands a ticket event on origin/tickets behind our back.

    This is what makes the next local push a non-fast-forward, i.e. what drives the
    retry loop under test.
    """
    origin = tmp_path / "origin.git"
    comp = tmp_path / "competitor"
    subprocess.run(
        ["git", "clone", "-q", "-b", "tickets", str(origin), str(comp)],
        check=True,
        capture_output=True,
    )
    _git(comp, "config", "user.email", "c@c.co")
    _git(comp, "config", "user.name", "c")
    tdir = comp / "9999-comp-9999-9999"
    tdir.mkdir()
    (tdir / "1700000000000000000-9999-comp-9999-9999-CREATE.json").write_text('{"side":"comp"}')
    _git(comp, "add", "-A")
    _git(comp, "commit", "-q", "--no-verify", "-m", "ticket: CREATE competing")
    _git(comp, "push", "-q", "origin", "HEAD:tickets")


def _local_unpushed_commit(tracker: Path) -> None:
    """A local ticket event committed but not pushed — the commit the loop must land."""
    tdir = tracker / "8888-locl-8888-8888"
    tdir.mkdir()
    (tdir / "1700000000000000000-8888-locl-8888-8888-CREATE.json").write_text('{"side":"local"}')
    _git(tracker, "add", "-A")
    _git(tracker, "commit", "-q", "--no-verify", "-m", "ticket: CREATE local")


def _set_refspec(tracker: Path, refspec: str) -> None:
    _git(tracker, "config", "--unset-all", "remote.origin.fetch", check=False)
    _git(tracker, "config", "remote.origin.fetch", refspec)


def _assert_converged(tmp_path: Path, tracker: Path, *, refspec: str) -> None:
    """The contractual postcondition: the push LANDED and the competitor's work survived."""
    remote_sha = _git(tracker, "ls-remote", "origin", "refs/heads/tickets").stdout.split()[0]
    head = _rev(tracker, "HEAD")
    assert remote_sha == head, (
        f"push never converged under refspec {refspec!r}: origin/tickets is {remote_sha} "
        f"but local HEAD is {head} — the retry loop exhausted its budget without landing"
    )
    assert (tracker / "8888-locl-8888-8888").is_dir(), "local ticket event lost by the retry loop"
    assert (tracker / "9999-comp-9999-9999").is_dir(), (
        "the competing writer's ticket event was never merged in — the retry loop's merge "
        "target did not advance"
    )


# ── The bug: a narrow refspec leaves the merge target ABSENT ─────────────────────────


def test_push_retry_converges_when_remote_tracking_ref_is_absent(
    tmp_path: Path, repo_with_origin: Path
) -> None:
    """Fresh single-branch clone shape: ``refs/remotes/origin/tickets`` never existed."""
    repo = repo_with_origin
    tracker = repo / ".tickets-tracker"
    _competing_push(tmp_path, repo)

    _set_refspec(tracker, NARROW_REFSPEC)
    _git(tracker, "update-ref", "-d", "refs/remotes/origin/tickets", check=False)
    assert not _rev(tracker, "origin/tickets"), (
        "precondition: the remote-tracking ref must be absent (narrow-clone shape)"
    )
    _local_unpushed_commit(tracker)

    push.push_tickets_branch(str(tracker))

    _assert_converged(tmp_path, tracker, refspec=NARROW_REFSPEC)


def test_push_retry_converges_when_remote_tracking_ref_is_stale(
    tmp_path: Path, repo_with_origin: Path
) -> None:
    """The ref exists but is pinned to an older snapshot, so the merge is a no-op.

    This is the harder shape: the ``rev-parse``-style existence checks all pass and the
    merge even *succeeds* — it just merges nothing, so the next push is rejected again on
    identical grounds. Nothing in the loop can notice.
    """
    repo = repo_with_origin
    tracker = repo / ".tickets-tracker"

    # Resolve the ref ONCE while it is still current, then narrow the refspec so a later
    # bare fetch cannot advance it.
    _git(tracker, "fetch", "-q", "origin", "+refs/heads/tickets:refs/remotes/origin/tickets")
    stale_sha = _rev(tracker, "origin/tickets")
    assert stale_sha, "precondition: the remote-tracking ref must exist before it goes stale"
    _set_refspec(tracker, NARROW_REFSPEC)

    _competing_push(tmp_path, repo)
    assert _rev(tracker, "origin/tickets") == stale_sha, (
        "precondition: the remote-tracking ref is now behind the true remote branch"
    )
    _local_unpushed_commit(tracker)

    push.push_tickets_branch(str(tracker))

    _assert_converged(tmp_path, tracker, refspec=NARROW_REFSPEC)


# ── The discriminating control: identical scenario, wildcard refspec ─────────────────


def test_push_retry_converges_under_wildcard_refspec(
    tmp_path: Path, repo_with_origin: Path
) -> None:
    """Same contention, same retry budget — only the configured refspec differs.

    This passes both before and after the fix. It is here so that a failure of the two
    narrow cases points at the refspec and not at the retry budget, the competing writer's
    timing, or a genuine merge conflict.
    """
    repo = repo_with_origin
    tracker = repo / ".tickets-tracker"
    _competing_push(tmp_path, repo)

    _set_refspec(tracker, WILDCARD_REFSPEC)
    _git(tracker, "update-ref", "-d", "refs/remotes/origin/tickets", check=False)
    _local_unpushed_commit(tracker)

    push.push_tickets_branch(str(tracker))

    _assert_converged(tmp_path, tracker, refspec=WILDCARD_REFSPEC)


# ── Reintroduction guard for the store-side sites (AC4) ──────────────────────────────


def test_store_modules_never_bare_fetch_the_tickets_branch() -> None:
    """``_store`` must name the destination ref when it fetches a branch it then consumes
    as ``<remote>/<branch>``.

    A bare ``_git(..., "fetch", remote, branch)`` is the exact defect of 5546 and 35f7: it
    compiles, exits 0, and silently leaves the remote-tracking ref absent or stale. The
    correct form passes ``+refs/heads/<branch>:refs/remotes/<remote>/<branch>``.
    """
    store = Path(rebar.__file__).resolve().parent / "_store"
    offenders: list[str] = []
    for mod in sorted(store.glob("*.py")):
        for lineno, line in enumerate(mod.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#") or '"fetch"' not in stripped:
                continue
            if "refs/remotes/" in stripped or "refspec" in stripped:
                continue
            offenders.append(f"{mod.name}:{lineno}: {stripped}")
    assert not offenders, (
        "bare 'git fetch <remote> <branch>' in _store — it only opportunistically writes "
        "refs/remotes/<remote>/<branch>, so under a narrow clone refspec the ref is left "
        "absent or stale (bugs 5546, 35f7). Fetch with an explicit "
        "'+refs/heads/<branch>:refs/remotes/<remote>/<branch>' refspec instead. Found:\n"
        + "\n".join(offenders)
    )


def test_environment_has_no_ambient_narrowing(tmp_path: Path) -> None:
    """``git remote add`` installs the WILDCARD refspec by default.

    Pinned because it is the reason the CI reconciler workflows are NOT affected by this
    bug (``actions/checkout`` builds its workspace with ``git remote add``), and therefore
    the reason the narrow shape above must be constructed explicitly rather than assumed.
    """
    probe = tmp_path / "probe"
    probe.mkdir()
    _git(probe.parent, "init", "-q", str(probe))
    _git(probe, "remote", "add", "origin", str(tmp_path / "nowhere.git"))
    assert (
        _git(probe, "config", "--get-all", "remote.origin.fetch").stdout.strip() == WILDCARD_REFSPEC
    ), "git remote add no longer installs the wildcard refspec — revisit the CI analysis"
