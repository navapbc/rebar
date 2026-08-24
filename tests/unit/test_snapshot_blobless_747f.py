"""Bug 747f — the attested snapshot path must not DEPEND on git's lazy blob fetch.

``_fetch_origin`` fetches with ``--filter=blob:none`` unconditionally, which both strips
blobs from the fetch AND latches the clone into a promisor remote
(``remote.<name>.promisor=true``). ``_materialize_tree`` then runs ``git read-tree`` (no
``-u``) + ``git checkout-index``, and *neither* of those primitives implements git's
batching prefetch: ``prefetch_cache_entries()`` lives in ``unpack-trees.c``'s
``check_updates()``, which only runs when the working tree is updated, and
``builtin/checkout-index.c`` has no prefetch at all. So every missing blob becomes its own
sequential ``git fetch`` RPC — on the ~25k-file tickets tree, hours.

The invariant this file pins is deliberately stronger than "it is fast": with
``GIT_NO_LAZY_FETCH=1`` set, materialization must still produce the complete tree. That is
only true if the blobs were obtained up front, in one RPC, rather than lazily one at a
time. Before the fix this yields ZERO files (``checkout-index`` errors "unable to read
sha1 file" for every path).

``GIT_NO_LAZY_FETCH`` is documented/user-facing from git 2.45 (it is SILENTLY IGNORED on
older git, which is exactly why it is usable as a test assertion but must never be a
production guard), so these tests skip below that floor.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from rebar._snapshot import repo_snapshot as rs

_FILE_COUNT = 40


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _git_version() -> tuple[int, ...]:
    out = subprocess.run(["git", "--version"], capture_output=True, text=True, check=True).stdout
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", out)
    if not match:  # pragma: no cover - defensive
        return (0,)
    return tuple(int(part) for part in match.groups(default="0"))


_needs_no_lazy_fetch = pytest.mark.skipif(
    _git_version() < (2, 45),
    reason="GIT_NO_LAZY_FETCH is only documented/honoured from git 2.45",
)


@pytest.fixture(autouse=True)
def _isolate_store(monkeypatch, tmp_path):
    store = tmp_path / "gate-tmpdir"
    store.mkdir()
    monkeypatch.setenv("REBAR_GATE_TMPDIR", str(store))


@pytest.fixture
def blobless_clone(tmp_path) -> tuple[Path, str]:
    """A ``--filter=blob:none`` clone whose object DB is missing EVERY blob of the tip tree.

    This is the shape of the real deployment: the gate server's clone was latched into a
    promisor remote (by ``clone --filter`` or by rebar's own filtered fetch), so the tree
    resolves but its file contents live only on the remote.
    """
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "--quiet")
    _git(upstream, "config", "user.email", "t@example.com")
    _git(upstream, "config", "user.name", "Test")
    _git(upstream, "config", "commit.gpgsign", "false")
    # Serving a blobless clone (and a later targeted want) requires both of these.
    _git(upstream, "config", "uploadpack.allowFilter", "true")
    _git(upstream, "config", "uploadpack.allowAnySHA1InWant", "true")
    nested = upstream / "pkg" / "sub"
    nested.mkdir(parents=True)
    for i in range(_FILE_COUNT):
        (nested / f"f{i}.txt").write_text(f"content-{i}\n")
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "--quiet", "-m", "seed")
    sha = _git(upstream, "rev-parse", "HEAD")

    clone = tmp_path / "clone"
    subprocess.run(
        [
            "git",
            "clone",
            "--quiet",
            "--no-checkout",  # never populate a worktree: nothing lazily backfills the blobs
            "--filter=blob:none",
            f"file://{upstream}",
            str(clone),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert _missing_blob_count(clone, sha) == _FILE_COUNT, "fixture must start blob-starved"
    return clone, sha


def _missing_blob_count(repo: Path, sha: str) -> int:
    """Blobs of ``sha``'s tree absent from the local object DB (no network: ``--missing``
    turns off git's lazy fetch, so this probe never perturbs what it measures)."""
    out = _git(
        repo, "rev-list", "--objects", "--missing=print", "--no-object-names", "--no-walk", sha
    )
    return sum(1 for line in out.splitlines() if line.startswith("?"))


# --------------------------------------------------------------------------------------
# The invariant: materialization does not depend on lazy fetching.
# --------------------------------------------------------------------------------------
@_needs_no_lazy_fetch
def test_materialize_completes_without_lazy_fetch(blobless_clone, monkeypatch):
    """RED before the fix: ``checkout-index`` cannot read a single blob, so the snapshot is
    empty (or the materialize fails outright). GREEN after: the whole tree is present."""
    clone, sha = blobless_clone
    # Forbid git's per-blob lazy fetch. The ONLY way to satisfy this is to have already
    # obtained the blobs in one up-front RPC.
    monkeypatch.setenv("GIT_NO_LAZY_FETCH", "1")

    handle = rs.materialize(sha, repo_root=str(clone), fetch=False)

    materialized = sorted(p.name for p in (Path(handle.path) / "pkg" / "sub").iterdir())
    assert materialized == sorted(f"f{i}.txt" for i in range(_FILE_COUNT))
    # Contents must be the real committed bytes, not empty/placeholder files.
    for i in range(_FILE_COUNT):
        assert (Path(handle.path) / "pkg" / "sub" / f"f{i}.txt").read_text() == f"content-{i}\n"


@_needs_no_lazy_fetch
def test_materialize_tickets_completes_without_lazy_fetch(tmp_path, monkeypatch):
    """The ticket-store tree (~25k files in the real deployment) is the path that took
    hours; it materializes through the same primitive and needs the same guarantee."""
    upstream = tmp_path / "tickets-upstream"
    upstream.mkdir()
    _git(upstream, "init", "--quiet", "--initial-branch=tickets")
    _git(upstream, "config", "user.email", "t@example.com")
    _git(upstream, "config", "user.name", "Test")
    _git(upstream, "config", "commit.gpgsign", "false")
    _git(upstream, "config", "uploadpack.allowFilter", "true")
    _git(upstream, "config", "uploadpack.allowAnySHA1InWant", "true")
    for i in range(_FILE_COUNT):
        (upstream / f"e{i}.json").write_text(f'{{"n": {i}}}\n')
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "--quiet", "-m", "events")
    sha = _git(upstream, "rev-parse", "HEAD")

    clone = tmp_path / "tickets-clone"
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
    assert _missing_blob_count(clone, sha) == _FILE_COUNT

    monkeypatch.setenv("GIT_NO_LAZY_FETCH", "1")
    root = Path(rs.materialize_tickets(sha, repo_root=str(clone), fetch=False))

    tracker = root / ".tickets-tracker"
    assert sorted(p.name for p in tracker.iterdir()) == sorted(
        f"e{i}.json" for i in range(_FILE_COUNT)
    )
    assert (tracker / "e7.json").read_text() == '{"n": 7}\n'


@_needs_no_lazy_fetch
def test_blobs_arrive_in_one_rpc_not_one_per_file(blobless_clone, monkeypatch):
    """Teeth against a 'fix' that merely loops a fetch per blob: the whole tree must be
    satisfied by a SMALL, bounded number of git fetch invocations, not ~one per file."""
    clone, sha = blobless_clone
    monkeypatch.setenv("GIT_NO_LAZY_FETCH", "1")

    fetches: list[list[str]] = []
    real_run = subprocess.run

    def counting_run(argv, *a, **kw):
        if isinstance(argv, list) and "fetch" in argv:
            fetches.append(list(argv))
        return real_run(argv, *a, **kw)

    monkeypatch.setattr(subprocess, "run", counting_run)
    handle = rs.materialize(sha, repo_root=str(clone), fetch=False)

    assert len(list((Path(handle.path) / "pkg" / "sub").iterdir())) == _FILE_COUNT
    assert len(fetches) <= 2, f"expected a single batched fetch, saw {len(fetches)}: {fetches}"


# --------------------------------------------------------------------------------------
# Guardrails on the surrounding hygiene the same defect exposed.
# --------------------------------------------------------------------------------------
def test_materialize_tree_disables_interactive_credential_prompt(tmp_path, monkeypatch):
    """``_materialize_tree`` can now drive its own network fetch, so — like
    ``_fetch_origin`` — it must never be able to block on a terminal credential prompt."""
    repo = tmp_path / "plain"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "c")
    sha = _git(repo, "rev-parse", "HEAD")

    seen: list[dict[str, str]] = []
    real_git = rs.git_run

    def spy(repo_root, *args, env=None):
        if env is not None:
            seen.append(env)
        return real_git(repo_root, *args, env=env)

    monkeypatch.setattr(rs, "git_run", spy)
    rs.materialize(sha, repo_root=str(repo), fetch=False)

    assert seen, "expected _materialize_tree to pass an explicit env"
    assert all(env.get("GIT_TERMINAL_PROMPT") == "0" for env in seen)


def test_snapshot_git_calls_are_time_bounded():
    """A stuck remote must not hang the long-lived MCP server: the snapshot path carries a
    timeout, matching the ``_store/push.py`` / ``_store/sync.py`` precedent.

    The network materialization fetch is bounded by the generous, tunable
    ``fetch_timeout()`` backstop (bug curly-open-swan), and the quick LOCAL git ops by
    ``git_fetch._GIT_TIMEOUT`` — both positive, so no call site is unbounded."""
    assert isinstance(rs.fetch_timeout(), (int, float))
    assert rs.fetch_timeout() > 0
