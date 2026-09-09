"""Skip fetch for a full SHA already present locally (bug ``sawdusty-snotty-fossa``).

Even the ref-scoped fetch from ``lemuroid-compliant-hoopoe`` adds latency and failure risk
when ``rev_parse`` can resolve this immutable object offline. The spy pins zero fetches for a
present SHA; companion cases require moving ``origin/<branch>`` refs to refresh and an absent
SHA to use a targeted want, never a broad fetch.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

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
def clone_with_present_sha(tmp_path) -> tuple[Path, str, str, str]:
    """Create a blobless clone with present, unrelated, and absent full SHAs.

    ``present_sha`` remains only as a local object after its tracking ref is deleted;
    ``tickets_sha`` names a large branch that must not transfer; ``absent_sha`` is committed
    upstream after cloning and therefore requires a targeted want.
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
    present_sha = _git(upstream, "rev-parse", "HEAD")

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

    # A NEW upstream commit the clone has never seen — genuinely absent locally.
    (upstream / "code.txt").write_text("hello again\n")
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "--quiet", "-m", "later")
    absent_sha = _git(upstream, "rev-parse", "HEAD")

    # Drop tracking refs so only the raw object (for present_sha) is left to resolve against.
    for ref in ("refs/remotes/origin/main", "refs/remotes/origin/tickets"):
        subprocess.run(
            ["git", "-C", str(clone), "update-ref", "-d", ref],
            capture_output=True,
            text=True,
            check=False,
        )
    return clone, present_sha, tickets_sha, absent_sha


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


def _tree_blobs_missing(repo: Path, sha: str) -> int:
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


def test_present_full_sha_issues_no_fetch(clone_with_present_sha, monkeypatch):
    """RED before the fossa fix: hoopoe's code still issues a targeted single-object want,
    so the spy records a fetch. GREEN after: a locally-present full SHA resolves with ZERO
    fetch invocations (rev_parse answers it with no network)."""
    clone, present_sha, tickets_sha, _ = clone_with_present_sha
    # Precondition: the object is genuinely present locally with no tracking ref.
    assert _git(clone, "rev-parse", "--verify", "--quiet", f"{present_sha}^{{commit}}")
    fetches = _spy_fetches(monkeypatch)

    sha = rs.resolve_ref(present_sha, str(clone), fetch=True, blobless=False)

    assert sha == present_sha
    # The teeth: no git fetch of ANY shape may run for a locally-present full SHA.
    assert fetches == [], f"resolving a locally-present full SHA still fetched: {fetches}"
    # And the unrelated large branch stayed untransferred.
    assert _tree_blobs_missing(clone, tickets_sha) >= _TICKETS_FILE_COUNT


def test_moving_branch_still_refreshes(clone_with_present_sha, monkeypatch):
    """The skip is narrow: a moving ``origin/<branch>`` is NOT a full SHA, so resolution
    still fetches (freshness preserved). ``main`` has advanced upstream since the clone, so
    a real refresh resolves to the NEW tip, not the stale one."""
    clone, _, _, advanced_main = clone_with_present_sha
    fetches = _spy_fetches(monkeypatch)

    sha = rs.resolve_ref("origin/main", str(clone), fetch=True, blobless=False)

    assert sha == advanced_main
    assert fetches, "a moving branch ref must still refresh via a fetch"


def _object_absent_offline(repo: Path, sha: str) -> bool:
    """True when ``sha`` is absent from the local object DB, checked WITHOUT a promisor
    lazy fetch (``GIT_NO_LAZY_FETCH``) so the probe itself never populates the object."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", "--end-of-options", f"{sha}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
        env=subprocess_env({"GIT_NO_LAZY_FETCH": "1"}),
    )
    return proc.returncode != 0


def test_absent_full_sha_takes_targeted_want(clone_with_present_sha, monkeypatch):
    """A full SHA that is NOT present locally must still fetch (a targeted want under
    allowReachableSHA1InWant) — the skip only applies when the object is already local."""
    clone, _, tickets_sha, absent_sha = clone_with_present_sha
    # Precondition (offline probe — must not lazily populate the object it is checking).
    assert _object_absent_offline(clone, absent_sha), "absent_sha must not be present"
    fetches = _spy_fetches(monkeypatch)

    sha = rs.resolve_ref(absent_sha, str(clone), fetch=True, blobless=False)

    assert sha == absent_sha
    assert fetches, "an absent full SHA must trigger a targeted fetch, not resolve offline"
    # Even the recovery path must not broad-fetch the unrelated branch.
    assert _tree_blobs_missing(clone, tickets_sha) >= _TICKETS_FILE_COUNT


def test_unresolvable_full_sha_fails_closed(clone_with_present_sha, monkeypatch):
    """A syntactically valid full SHA the remote cannot serve must fail closed, never
    silently resolve or broad-fetch."""
    clone, _, _, _ = clone_with_present_sha
    bogus = "0" * 40
    _spy_fetches(monkeypatch)

    with pytest.raises(SnapshotRefError):
        rs.resolve_ref(bogus, str(clone), fetch=True, blobless=False)
