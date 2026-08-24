"""Bug ``lemuroid-compliant-hoopoe`` — attested ref resolution must scope its fetch.

``resolve_ref`` used to open with an UNSCOPED ``git fetch --no-filter origin`` (no refspec).
On a partial/promisor checkout whose ``origin.fetch`` maps every head
(``+refs/heads/*:refs/remotes/origin/*``) that transfers EVERY branch — including the huge,
unrelated ``tickets`` history — when only a single code SHA is needed. In the field a
post-merge ``review-plan`` made zero LLM calls and still spent 512s before ``git fetch``
tripped the 300s ceiling, leaving partial packs (~153.6 MB).

The invariant pinned here is deliberately about the *fetch shape*, not wall-clock time: the
network fetch backing an attested resolution must carry a refspec scoped to the requested
ref, and must NEVER be the bare-remote ``git fetch ... origin`` that pulls all heads. A RED
run (pre-fix) sees exactly that unscoped argv and the unrelated branch materialized locally.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rebar._snapshot import repo_snapshot as rs
from rebar._snapshot.git_fetch import SnapshotRefError

_TICKETS_FILE_COUNT = 60


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


@pytest.fixture(autouse=True)
def _isolate_store(monkeypatch, tmp_path):
    store = tmp_path / "gate-tmpdir"
    store.mkdir()
    monkeypatch.setenv("REBAR_GATE_TMPDIR", str(store))


@pytest.fixture
def clone_with_large_unrelated_branch(tmp_path) -> tuple[Path, str, str]:
    """A ``--filter=blob:none`` clone of an upstream carrying a tiny ``main`` and a large,
    UNRELATED ``tickets`` branch, with ``origin.fetch`` mapping every head.

    Returns ``(clone, main_sha, tickets_sha)``. The clone starts with NO local
    remote-tracking refs for either branch (they are deleted) so resolving ``origin/main``
    genuinely needs a network fetch — the exact shape of the deployment that broad-fetched.
    """
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "--quiet", "--initial-branch=main")
    _git(upstream, "config", "user.email", "t@example.com")
    _git(upstream, "config", "user.name", "Test")
    _git(upstream, "config", "commit.gpgsign", "false")
    _git(upstream, "config", "uploadpack.allowFilter", "true")
    _git(upstream, "config", "uploadpack.allowAnySHA1InWant", "true")

    (upstream / "code.txt").write_text("hello\n")
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "--quiet", "-m", "code")
    main_sha = _git(upstream, "rev-parse", "HEAD")

    # A large, unrelated history that a scoped fetch must never transfer.
    _git(upstream, "checkout", "--quiet", "-b", "tickets")
    big = upstream / "events"
    big.mkdir()
    for i in range(_TICKETS_FILE_COUNT):
        (big / f"e{i}.json").write_text('{{"n": {}, "pad": "{}"}}\n'.format(i, "x" * 800))
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "--quiet", "-m", "tickets")
    tickets_sha = _git(upstream, "rev-parse", "HEAD")
    _git(upstream, "checkout", "--quiet", "main")

    clone = tmp_path / "clone"
    subprocess.run(
        [
            "git",
            "clone",
            "--quiet",
            "--no-checkout",
            "--filter=blob:none",
            f"file://{upstream}",
            str(clone),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    # Force a real fetch on resolve: drop any tracking refs the clone auto-created.
    for ref in ("refs/remotes/origin/main", "refs/remotes/origin/tickets"):
        subprocess.run(
            ["git", "-C", str(clone), "update-ref", "-d", ref],
            capture_output=True,
            text=True,
            check=False,
        )
    return clone, main_sha, tickets_sha


def _spy_fetches(monkeypatch) -> list[list[str]]:
    """Record the argv of every ``git ... fetch`` subprocess run."""
    fetches: list[list[str]] = []
    real_run = subprocess.run

    def counting_run(argv, *a, **kw):
        if isinstance(argv, list) and "fetch" in argv:
            fetches.append(list(argv))
        return real_run(argv, *a, **kw)

    monkeypatch.setattr(subprocess, "run", counting_run)
    return fetches


def _is_unscoped_origin_fetch(argv: list[str], remote: str = "origin") -> bool:
    """True for a bare ``git fetch ... <remote>`` with NO refspec/positional after it.

    That argv makes git apply the configured ``origin.fetch`` and pull EVERY head — the
    exact defect. A scoped fetch names a refspec or object after the remote.
    """
    if "fetch" not in argv or remote not in argv:
        return False
    return argv[-1] == remote


def _tracking_ref_absent(repo: Path, ref: str) -> bool:
    """True when the remote-tracking ``ref`` does not exist locally.

    A broad ``git fetch origin`` applies the clone's all-heads refspec and CREATES
    ``refs/remotes/origin/tickets``; a fetch scoped to ``main`` never does. (Commit/tree
    objects alone are an unreliable signal here — a ``blob:none`` clone already holds every
    commit and tree, filtering only blobs.)"""
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", ref],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode != 0


def _tree_blobs_missing(repo: Path, sha: str) -> int:
    """Count blobs of ``sha``'s tree absent from the local object DB (no network:
    ``--missing`` disables git's lazy fetch). A broad ``--no-filter`` fetch of the tickets
    branch would leave ZERO missing; a scoped fetch of ``main`` leaves them all missing."""
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "rev-list",
            "--objects",
            "--missing=print",
            "--no-object-names",
            "--no-walk",
            sha,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return sum(1 for line in proc.stdout.splitlines() if line.startswith("?"))


def test_resolving_remote_ref_scopes_the_fetch(clone_with_large_unrelated_branch, monkeypatch):
    """RED before the fix: the first fetch is ``git fetch --no-filter origin`` (unscoped),
    and ``origin/tickets`` gets materialized. GREEN after: the fetch carries a refspec
    scoped to ``main`` and the unrelated branch is never transferred."""
    clone, main_sha, tickets_sha = clone_with_large_unrelated_branch
    fetches = _spy_fetches(monkeypatch)

    sha = rs.resolve_ref("origin/main", str(clone), fetch=True, blobless=False)

    assert sha == main_sha
    assert fetches, "resolution must fetch from origin when the tracking ref is absent"
    # The teeth: NO fetch may be the bare-remote form that pulls every head.
    unscoped = [f for f in fetches if _is_unscoped_origin_fetch(f)]
    assert not unscoped, f"resolution issued an unscoped broad fetch: {unscoped}"
    # And the scoped fetch must actually name the requested branch.
    assert any("main" in " ".join(f) for f in fetches), fetches
    # The unrelated large branch's tip must NOT have been transferred.
    assert _tree_blobs_missing(clone, tickets_sha) >= _TICKETS_FILE_COUNT, (
        "resolving origin/main pulled the unrelated 'tickets' history into the clone"
    )
    # A broad fetch would also have created the tickets tracking ref; a scoped one does not.
    assert _tracking_ref_absent(clone, "refs/remotes/origin/tickets"), (
        "resolving origin/main created refs/remotes/origin/tickets (broad fetch)"
    )


def test_scoped_fetch_updates_the_remote_tracking_ref(
    clone_with_large_unrelated_branch, monkeypatch
):
    """A remote-qualified ref must still resolve via its tracking ref after the scoped
    fetch (support for remote refs preserved)."""
    clone, main_sha, _ = clone_with_large_unrelated_branch
    _spy_fetches(monkeypatch)

    rs.resolve_ref("origin/main", str(clone), fetch=True, blobless=False)

    assert _git(clone, "rev-parse", "--verify", "--quiet", "origin/main") == main_sha


def test_unresolvable_ref_still_fails_closed(clone_with_large_unrelated_branch, monkeypatch):
    """A missing/unreachable requested ref must continue to fail closed, not resolve to
    something else or silently broad-fetch to find it."""
    clone, _, _ = clone_with_large_unrelated_branch
    fetches = _spy_fetches(monkeypatch)

    with pytest.raises(SnapshotRefError):
        rs.resolve_ref("origin/does-not-exist", str(clone), fetch=True, blobless=False)

    unscoped = [f for f in fetches if _is_unscoped_origin_fetch(f)]
    assert not unscoped, f"fail-closed path fell back to an unscoped broad fetch: {unscoped}"


def test_bare_sha_resolution_scopes_to_the_object(clone_with_large_unrelated_branch, monkeypatch):
    """Resolving a specific code SHA (allowReachableSHA1InWant) must scope to that object,
    never broad-fetch every head to find it."""
    clone, main_sha, tickets_sha = clone_with_large_unrelated_branch
    fetches = _spy_fetches(monkeypatch)

    sha = rs.resolve_ref(main_sha, str(clone), fetch=True, blobless=False)

    assert sha == main_sha
    unscoped = [f for f in fetches if _is_unscoped_origin_fetch(f)]
    assert not unscoped, f"SHA resolution issued an unscoped broad fetch: {unscoped}"
    assert _tree_blobs_missing(clone, tickets_sha) >= _TICKETS_FILE_COUNT
