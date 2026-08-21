"""S1 — faithful, lock-free snapshot materialization core (epic raze-vet-ditch).

Covers the ``rebar._snapshot.repo_snapshot`` acceptance criteria: faithful tree
(incl. ``export-ignore``), LFS-pointer detection + submodule omission, atomic
population + startup sweep, portability (no external ``tar``, no hardcoded ``/tmp``),
descriptive fail-closed credential errors, and concurrent different-SHA materializations
that do not contend on the repo index lock.
"""

from __future__ import annotations

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from rebar._snapshot import git_fetch
from rebar._snapshot import repo_snapshot as rs


def _git(repo: Path, *args: str, env: dict | None = None) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return proc.stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "--quiet")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "commit.gpgsign", "false")
    return path


def _commit_all(repo: Path, msg: str = "c") -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture(autouse=True)
def _isolate_store(monkeypatch, tmp_path):
    """Point the snapshot store at a per-test tmp dir (never a hardcoded /tmp)."""
    store = tmp_path / "gate-tmpdir"
    store.mkdir()
    monkeypatch.setenv("REBAR_GATE_TMPDIR", str(store))


# --------------------------------------------------------------------------------------
# AC1 — faithful tree, including export-ignore; export-subst NOT applied
# --------------------------------------------------------------------------------------
def test_materialize_byte_matches_committed_tree_including_export_ignore(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "keep.txt").write_text("hello\n")
    (repo / "sub").mkdir()
    (repo / "sub" / "nested.py").write_text("x = 1\n")
    # A path git-archive would DROP (export-ignore) but a faithful snapshot KEEPS.
    (repo / "secret.txt").write_text("present\n")
    (repo / ".gitattributes").write_text("secret.txt export-ignore\n")
    # A path git-archive would REWRITE via export-subst; committed bytes must survive.
    (repo / "ver.txt").write_text("$Format:%H$\n")
    (repo / ".gitattributes").write_text("secret.txt export-ignore\nver.txt export-subst\n")
    sha = _commit_all(repo)

    handle = rs.materialize(sha, repo_root=str(repo), fetch=False)
    snap = handle.path
    assert (snap / "keep.txt").read_text() == "hello\n"
    assert (snap / "sub" / "nested.py").read_text() == "x = 1\n"
    # export-ignore file IS present (faithful, not git archive)
    assert (snap / "secret.txt").read_text() == "present\n"
    # export-subst NOT applied — committed placeholder preserved verbatim
    assert (snap / "ver.txt").read_text() == "$Format:%H$\n"
    assert handle.signable is True
    assert handle.sha == sha


def test_attested_handle_is_signable_local_is_not(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("a\n")
    _commit_all(repo)
    local = rs.materialize(source_mode="local", repo_root=str(repo))
    assert local.signable is False
    assert local.sha is None
    assert local.source == "local"
    assert local.path == Path(str(repo)).resolve()


# --------------------------------------------------------------------------------------
# AC2 — LFS pointer detection + submodule omission/recording
# --------------------------------------------------------------------------------------
def test_lfs_pointer_detected_not_served_as_content(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    pointer = "version https://git-lfs.github.com/spec/v1\noid sha256:abc123\nsize 12345\n"
    (repo / "big.bin").write_text(pointer)
    (repo / "plain.txt").write_text("not a pointer\n")
    sha = _commit_all(repo)

    handle = rs.materialize(sha, repo_root=str(repo), fetch=False)
    assert "big.bin" in handle.lfs_pointers
    assert "plain.txt" not in handle.lfs_pointers
    assert rs.is_lfs_pointer(handle.path / "big.bin") is True
    assert rs.is_lfs_pointer(handle.path / "plain.txt") is False


def test_submodule_gitlink_omitted_and_recorded(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "top.txt").write_text("top\n")
    _commit_all(repo, "base")
    base_sha = _git(repo, "rev-parse", "HEAD")
    # Fabricate a gitlink (mode 160000) entry pointing at a commit, without a real
    # submodule checkout — exercises the omit-and-record path deterministically.
    _git(
        repo,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{base_sha},vendor/sub",
    )
    sha = _git(repo, "write-tree")
    sha = _git(repo, "commit-tree", sha, "-m", "add gitlink", "-p", base_sha)
    _git(repo, "reset", "--soft", sha)

    handle = rs.materialize(sha, repo_root=str(repo), fetch=False)
    assert "vendor/sub" in handle.submodules
    # Submodule contents are omitted (no blob to materialize for a gitlink).
    assert not (handle.path / "vendor" / "sub").exists() or not any(
        (handle.path / "vendor" / "sub").iterdir()
    )
    assert (handle.path / "top.txt").read_text() == "top\n"


# --------------------------------------------------------------------------------------
# AC3 — atomic population + startup sweep
# --------------------------------------------------------------------------------------
def test_no_tmp_left_after_successful_materialize(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("a\n")
    sha = _commit_all(repo)
    rs.materialize(sha, repo_root=str(repo), fetch=False)
    tmp_dir = rs.store_root() / "tmp"
    leftovers = list(tmp_dir.iterdir()) if tmp_dir.is_dir() else []
    assert leftovers == []


def test_sweep_tmp_clears_crashed_build(tmp_path):
    store = rs.store_root()
    crashed = store / "tmp" / "build-deadbeef-xyz"
    crashed.mkdir(parents=True)
    (crashed / "partial").write_text("half\n")
    (store / "tmp" / "build-deadbeef-xyz.index").write_text("idx\n")
    removed = rs.sweep_tmp()
    assert removed == 2
    assert not crashed.exists()


def test_cache_hit_reuses_entry(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("a\n")
    sha = _commit_all(repo)
    h1 = rs.materialize(sha, repo_root=str(repo), fetch=False)
    h2 = rs.materialize(sha, repo_root=str(repo), fetch=False)
    assert h1.path == h2.path == rs.entry_path(sha)


# --------------------------------------------------------------------------------------
# AC4 — portability: REBAR_GATE_TMPDIR honored; no hardcoded /tmp
# --------------------------------------------------------------------------------------
def test_store_root_honors_gate_tmpdir(tmp_path, monkeypatch):
    target = tmp_path / "elsewhere"
    monkeypatch.setenv("REBAR_GATE_TMPDIR", str(target))
    root = rs.store_root()
    assert str(root).startswith(str(target))
    assert root.is_dir()


# --------------------------------------------------------------------------------------
# AC5 — descriptive, actionable credential error; attested fails closed
# --------------------------------------------------------------------------------------
def test_missing_credential_fetch_raises_descriptive_error(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("a\n")
    _commit_all(repo)
    # Point origin at a non-existent remote so fetch fails "could not read from remote".
    _git(repo, "remote", "add", "origin", str(tmp_path / "nonexistent-remote.git"))
    with pytest.raises(rs.SnapshotFetchError) as exc:
        rs.materialize("origin/main", repo_root=str(repo))
    msg = str(exc.value)
    assert "credential" in msg.lower() or "could not read" in msg.lower()


def test_unresolvable_ref_fails_closed(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("a\n")
    _commit_all(repo)
    with pytest.raises(rs.SnapshotRefError):
        rs.materialize("no-such-ref-xyz", repo_root=str(repo), fetch=False)


def test_auth_marker_detection():
    assert git_fetch.is_auth_failure("fatal: Authentication failed for 'https://...'")
    assert git_fetch.is_auth_failure("git@github.com: Permission denied (publickey).")
    assert not git_fetch.is_auth_failure("fatal: couldn't find remote ref main")


def test_malicious_ref_in_targeted_fetch_does_not_execute(tmp_path):
    """SECURITY: a client ref that survives to the targeted fetch (rev-parse miss +
    origin present) must be treated as a refspec, never an option — no command runs."""
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "--quiet", str(remote))
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("a\n")
    _commit_all(repo)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "--quiet", "origin", "HEAD:refs/heads/main")

    sentinel = tmp_path / "PWNED"
    malicious = f"--upload-pack=touch {sentinel};true"
    # The general fetch succeeds; rev-parse of the malicious ref misses; the targeted
    # fetch must reject it as an invalid refspec (--end-of-options) rather than exec it.
    with pytest.raises(rs.SnapshotError):
        rs.materialize(malicious, repo_root=str(repo))
    assert not sentinel.exists(), "ref injection executed a command — RCE!"


def test_invalid_source_mode_rejected(tmp_path):
    with pytest.raises(rs.SnapshotError):
        rs.materialize("HEAD", source_mode="bogus", repo_root=str(tmp_path))


# --------------------------------------------------------------------------------------
# AC6 — concurrent different-SHA materializations do not contend / corrupt
# --------------------------------------------------------------------------------------
def test_concurrent_different_shas_no_index_contention(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("v1\n")
    sha1 = _commit_all(repo, "v1")
    (repo / "f.txt").write_text("v2\n")
    sha2 = _commit_all(repo, "v2")
    # Leave a clean working tree + index; the materializations must not disturb it.
    before_status = _git(repo, "status", "--porcelain")

    def go(sha: str) -> tuple[str, str]:
        h = rs.materialize(sha, repo_root=str(repo), fetch=False)
        return sha, (h.path / "f.txt").read_text()

    with ThreadPoolExecutor(max_workers=2) as ex:
        results = dict(ex.map(go, [sha1, sha2, sha1, sha2]))

    assert results[sha1] == "v1\n"
    assert results[sha2] == "v2\n"
    # The repo's own index/working tree is untouched: materialization uses a throwaway
    # GIT_INDEX_FILE, so it never writes the repo index (which `status` would reflect).
    # This is the load-bearing assertion — drop GIT_INDEX_FILE and this regresses.
    assert _git(repo, "status", "--porcelain") == before_status


# --------------------------------------------------------------------------------------
# Bug 8386 — adjacent tickets SHAs must not each cost a whole fresh tree
# --------------------------------------------------------------------------------------
def _distinct_bytes(root: Path) -> int:
    """Bytes the store really occupies: each inode charged exactly once.

    A naive ``st_size`` sum counts a hardlinked blob once per entry that references it,
    which is the very sharing under test — so recency/eviction accounting aside, the
    honest measure of "how much disk did N materializations cost" is per-inode."""
    seen: set[tuple[int, int]] = set()
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except OSError:  # pragma: no cover - racing eviction
                continue
            key = (st.st_dev, st.st_ino)
            if key in seen:
                continue
            seen.add(key)
            total += st.st_size
    return total


def test_adjacent_tickets_shas_reuse_unchanged_blobs(tmp_path):
    """N gate resolutions across N adjacent tickets SHAs must cost ~ONE tree, not N trees.

    The tickets tip advances every ~26s and each commit touches a handful of files, so the
    ``tickets-<sha>`` key effectively never hits. ADR 0005 D2 grounds the store on "an
    immutable SHA is a perfect cache key", which holds only for a key with REUSE; without
    sharing, a one-file delta rewrites the whole tree and the store degenerates into an
    append-only log of full copies (measured in the wild at 64k entries / 47.2 GiB).

    The property asserted is algorithmic — distinct bytes consumed per resolution — never
    wall-clock, and it is measured on observable on-disk output rather than any internal.
    """
    repo = _init_repo(tmp_path / "repo")
    (repo / "seed.txt").write_text("seed\n")
    _commit_all(repo, "seed")
    _git(repo, "checkout", "--quiet", "-b", "tickets")

    payload = "T" * 20_000
    tickets = repo / "tickets"
    tickets.mkdir()
    for i in range(40):
        (tickets / f"t{i:03d}.json").write_text(payload)
    _commit_all(repo, "tickets base")

    store = rs.store_root()
    shas: list[str] = []
    for i in range(4):
        # Exactly ONE file changes per commit — the shape of a real tickets commit.
        (tickets / f"t{i:03d}.json").write_text(payload[:-1] + "X")
        shas.append(_commit_all(repo, f"one-file delta {i}"))
        rs.materialize_tickets("tickets", repo_root=str(repo), fetch=False)

    one_tree = 40 * len(payload)
    consumed = _distinct_bytes(store)

    # Four resolutions, four distinct SHAs, four entries — but they differ by one file
    # each, so the DISK cost must stay near a single tree. Without sharing this is ~4x.
    assert consumed < one_tree * 2, (
        f"4 adjacent-SHA resolutions consumed {consumed} bytes; one tree is ~{one_tree}. "
        "Unchanged blobs are being rewritten instead of shared."
    )

    # The sharing is real, not an artefact of the measurement: a file untouched by every
    # delta is ONE inode across all four entries.
    entries = sorted(p for p in store.iterdir() if p.name.startswith("tickets-"))
    assert len(entries) == 4, "each distinct tickets SHA still gets its own entry"
    unchanged = {os.lstat(e / ".tickets-tracker" / "tickets" / "t039.json").st_ino for e in entries}
    assert len(unchanged) == 1, f"an unchanged blob occupies {len(unchanged)} inodes, want 1"

    # Faithfulness is NOT traded away for sharing: every entry must still byte-match the
    # committed tree at its own SHA, including the file that changed in that commit.
    for i, sha in enumerate(shas):
        root = store / f"tickets-{sha}" / ".tickets-tracker"
        listed = _git(repo, "ls-tree", "-r", "--name-only", sha).splitlines()
        on_disk = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
        assert on_disk == sorted(listed), f"entry {i} tree does not match {sha[:8]}"
        for rel in listed:
            want = _git(repo, "show", f"{sha}:{rel}")
            assert (root / rel).read_text().rstrip("\n") == want.rstrip("\n"), (
                f"entry {i} content diverges at {rel}"
            )


def test_donor_untracked_files_do_not_leak_into_a_new_entry(tmp_path):
    """A donor entry holds untracked files; none of them may appear in the entry built from it.

    A live ``tickets-<sha>`` entry is NOT just its committed tree: every ticket read drops a
    ``.cache.json`` into each ticket dir (4,844 measured inside one live entry). They are
    gitignored, so they belong to no tree. If the donor's contents were enumerated by walking
    its directory instead of by ``git ls-tree``, those extras would be hardlinked into the new
    entry — which would then no longer byte-match its own SHA — and any "walk count == tree
    count" completeness check would fail forever in production, silently disabling the delta
    path while still looking green on a clean fixture. This test makes that fixture dirty.
    """
    repo = _init_repo(tmp_path / "repo")
    (repo / "seed.txt").write_text("seed\n")
    _commit_all(repo, "seed")
    _git(repo, "checkout", "--quiet", "-b", "tickets")
    tickets = repo / "tickets"
    tickets.mkdir()
    for i in range(6):
        (tickets / f"t{i}.json").write_text("T" * 500)
    _commit_all(repo, "tickets base")

    first = Path(rs.materialize_tickets("tickets", repo_root=str(repo), fetch=False))

    # Simulate what a real read does to a published entry, post-materialization.
    donor_tracker = first / ".tickets-tracker" / "tickets"
    (donor_tracker / ".cache.json").write_text('{"derived": true}')
    (donor_tracker / "..cache.json.abc.tmp").write_text("transient")

    (tickets / "t0.json").write_text("T" * 499 + "X")
    sha = _commit_all(repo, "one-file delta")
    second = Path(rs.materialize_tickets("tickets", repo_root=str(repo), fetch=False))
    assert second != first

    listed = sorted(_git(repo, "ls-tree", "-r", "--name-only", sha).splitlines())
    root = second / ".tickets-tracker"
    on_disk = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
    assert on_disk == listed, (
        "the new entry must contain exactly its committed tree; untracked donor files leaked"
    )
