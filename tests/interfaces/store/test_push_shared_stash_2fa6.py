"""Held-out: the push dirty-merge recovery must never touch the shared stash stack.

Bug 2fa6. ``git``'s stash stack is REPO-GLOBAL — every worktree of a repository pushes
onto and pops from the same ``refs/stash``. The old recovery did
``stash push`` → merge → ``stash pop`` inside the TICKETS worktree, so a stash created
in a SOURCE worktree could be popped into the ticket store. That is not hypothetical:
it dropped ``src/…`` and ``.rebar/…`` into the tracker, left the index with unmerged
(DU) entries and no ``MERGE_HEAD``, and blocked every subsequent ticket write.

The test encodes the RACE rather than the steady state, because LIFO ordering alone
hides the bug: if nothing interleaves, a ``pop`` does pop the entry its own ``push``
created. The failure needs a foreign entry to land on the stack BETWEEN our set-aside
and our restore — exactly what a concurrent source-worktree ``git stash`` does. So the
git seam injects one at that point.

RED on the pre-fix code: the pop takes the foreign entry, so ``src/leak.py`` materializes
in the tracker (and the store's own edit is left in the stash). GREEN after: the recovery
addresses its own stash COMMIT OBJECT by sha, which no other worktree can reach, and
``refs/stash`` is left exactly as found.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from _git_upkeep import init_bare_remote

from rebar._store import push as _push


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False
    )


def _commit(cwd: Path, message: str) -> None:
    _git(cwd, "add", "-A")
    _git(cwd, "-c", "user.name=T", "-c", "user.email=t@e", "commit", "-q", "-m", message)


@pytest.fixture
def tracker_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    """A ``tickets`` checkout whose ``origin`` has advanced on a shared file."""
    remote = tmp_path / "remote.git"
    init_bare_remote(remote, initial_branch="tickets")

    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "--quiet", str(remote), str(seed))
    (seed / "shared.txt").write_text("base\n", encoding="utf-8")
    (seed / "untouched.txt").write_text("base\n", encoding="utf-8")
    _commit(seed, "seed")
    _git(seed, "push", "--quiet", "origin", "HEAD:tickets")

    tracker = tmp_path / "tracker"
    _git(tmp_path, "clone", "--quiet", "-b", "tickets", str(remote), str(tracker))

    # origin advances on the same file, so a merge must bring it in.
    (seed / "shared.txt").write_text("upstream\n", encoding="utf-8")
    _commit(seed, "upstream change")
    _git(seed, "push", "--quiet", "origin", "HEAD:tickets")
    _git(tracker, "fetch", "--quiet", "origin")

    # The tracker is dirty on that same file — this is what made the plain merge refuse
    # ("local changes would be overwritten") and drove the store into stash recovery.
    (tracker / "shared.txt").write_text("local-regenerable-edit\n", encoding="utf-8")
    # ...and dirty on a SECOND file the merge does not touch. Nothing can conflict on it,
    # so it must come back byte-for-byte — that is what proves the tree was genuinely
    # restored from our own stash commit, rather than merely reset away.
    (tracker / "untouched.txt").write_text("local-only-edit\n", encoding="utf-8")
    return tracker, remote


def _stash_entries(tracker: Path) -> list[str]:
    out = _git(tracker, "stash", "list").stdout.strip()
    return [line for line in out.splitlines() if line.strip()]


def test_dirty_merge_recovery_ignores_a_foreign_stash(
    tracker_with_remote: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker, _remote = tracker_with_remote

    # A stash created on ANOTHER branch of the same repo, carrying SOURCE files — the
    # shape of the entry that was actually popped into the store during the incident.
    _git(tracker, "checkout", "--quiet", "-b", "some-source-branch")
    (tracker / "src").mkdir()
    (tracker / "src" / "leak.py").write_text("SOURCE — must never reach the store\n", "utf-8")
    _git(tracker, "add", "-A")
    _git(
        tracker,
        "-c",
        "user.name=T",
        "-c",
        "user.email=t@e",
        "stash",
        "push",
        "--quiet",
        "-m",
        "work displaced from a source worktree",
    )
    _git(tracker, "checkout", "--quiet", "tickets")
    (tracker / "shared.txt").write_text("local-regenerable-edit\n", encoding="utf-8")
    (tracker / "untouched.txt").write_text("local-only-edit\n", encoding="utf-8")

    foreign_before = _stash_entries(tracker)
    assert len(foreign_before) == 1, foreign_before

    # Inject a SECOND foreign entry the moment the store sets its own tree aside, so the
    # top of the stack is NOT ours when the restore runs. This is the concurrent
    # source-worktree `git stash` that the incident report describes.
    real_git = _push._git
    injected: list[str] = []

    def _git_with_race(base: str, *args: str, **kwargs: object) -> subprocess.CompletedProcess:
        result = real_git(base, *args, **kwargs)  # type: ignore[arg-type]
        if args[:2] in (("stash", "create"), ("stash", "push")) and not injected:
            injected.append("yes")
            p = Path(base)
            (p / "src").mkdir(exist_ok=True)
            (p / "src" / "raced.py").write_text("SOURCE from a racing worktree\n", "utf-8")
            _git(p, "add", "-A")
            _git(
                p,
                "-c",
                "user.name=T",
                "-c",
                "user.email=t@e",
                "stash",
                "push",
                "--quiet",
                "-m",
                "racing source-worktree stash",
            )
        return result

    monkeypatch.setattr(_push, "_git", _git_with_race)

    ok = _push._recover_dirty_merge(str(tracker), "origin/tickets", 1, False)

    assert injected, "the race was never induced — the recovery did not set the tree aside"
    assert ok is True, "the dirty-tree merge recovery should have succeeded"

    # 1. No source file from EITHER foreign stash reached the ticket store.
    assert not (tracker / "src" / "leak.py").exists(), "a foreign stash was applied into the store"
    assert not (tracker / "src" / "raced.py").exists(), "the raced stash was applied into the store"

    # 2. The index is not stranded — this is what blocked every ticket write.
    assert _git(tracker, "ls-files", "-u").stdout.strip() == "", (
        "unmerged entries left in the index"
    )

    # 3. Both foreign entries are still on the shared stack, untouched: the store neither
    #    popped nor dropped work belonging to another worktree.
    after = _stash_entries(tracker)
    assert len(after) == 2, f"the store mutated the shared stash stack: {after}"
    assert any("work displaced from a source worktree" in e for e in after), after
    assert any("racing source-worktree stash" in e for e in after), after

    # 4. The merge actually happened, and the store's own tree really was restored: the
    #    non-conflicting edit is back byte-for-byte. The CONFLICTING one resolves to the
    #    merged HEAD instead — the long-standing contract of the conflicted-restore repair
    #    (the regenerable edit loses to upstream) — and leaves no markers behind.
    assert "upstream change" in _git(tracker, "log", "--oneline").stdout
    assert (tracker / "untouched.txt").read_text(encoding="utf-8") == "local-only-edit\n"
    assert (tracker / "shared.txt").read_text(encoding="utf-8") == "upstream\n"
    assert "<<<<<<<" not in (tracker / "shared.txt").read_text(encoding="utf-8")
