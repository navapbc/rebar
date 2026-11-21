"""Shared primitives for the repo-isolation guard (see tests/conftest.py).

No test may commit to or dirty this checkout — tests operate on disposable
trackers under ``tmp_path``. The conftest guard is built on these two read-only
git probes; keeping them here lets the guard's self-test
(``tests/unit/test_repo_isolation_guard.py``) exercise the *same* logic against a
throwaway repo instead of a copy that could drift.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable
from types import SimpleNamespace

_run_process = subprocess.run
_stable_subprocess_time = SimpleNamespace(
    sleep=subprocess.time.sleep,  # type: ignore[attr-defined]
)

# Watch volatile repo-root state one level deep to catch test leaks into existing dirs.
# Git, tracker, and virtualenv dirs have legitimate churn and are excluded.
LEAK_WATCH_SUBDIRS = (".rebar",)
GIT_PROBE_TIMEOUT_SECONDS = 5

# Retry timed-out read-only Git probes without sleep. A starved sample is inconclusive.
# the last timeout still fails so persistent inability to sample cannot hide a HEAD move.
GIT_PROBE_ATTEMPTS = 3


def _git_probe_once(root, args: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    """One bounded attempt. Only the timeout is retryable — a non-zero exit is
    deterministic, so ``CalledProcessError`` propagates from the first attempt."""
    return _run_process(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=GIT_PROBE_TIMEOUT_SECONDS,
    )


def _run_git_probe(root, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a bounded Git probe without inheriting a test's patched sleep.

    A timed-out attempt is re-sampled up to ``GIT_PROBE_ATTEMPTS`` times. The final
    attempt is deliberately OUTSIDE the retry loop so its ``TimeoutExpired`` propagates
    unconditionally — the loudness of a persistent failure is structural here, not a
    conditional that a later edit could quietly invert.
    """
    subprocess_time = subprocess.time  # type: ignore[attr-defined]
    subprocess.time = _stable_subprocess_time  # type: ignore[attr-defined]
    try:
        for _ in range(GIT_PROBE_ATTEMPTS - 1):
            try:
                return _git_probe_once(root, args)
            except subprocess.TimeoutExpired:
                continue
        return _git_probe_once(root, args)
    finally:
        subprocess.time = subprocess_time  # type: ignore[attr-defined]


def repo_leak_snapshot(root) -> set[str]:
    """Snapshot of the REPO_ROOT entries a test must not add to: every top-level
    name, PLUS the one-level entries inside the watched volatile state dirs (e.g.
    ``.rebar/run_snapshots``). Diff two snapshots (before/after a test) to find
    leaks — including a write INTO a pre-existing dir, which a top-level-only
    ``os.listdir`` diff cannot see."""
    snap = set(os.listdir(root))
    for sub in LEAK_WATCH_SUBDIRS:
        sub_dir = os.path.join(str(root), sub)
        if os.path.isdir(sub_dir):
            for entry in os.listdir(sub_dir):
                snap.add(f"{sub}/{entry}")
    return snap


# Environment that redirects git away from ``<root>/.git``; with any of it set, only git
# itself knows which repository ``git -C <root>`` resolves, so the file reader stands aside.
_GIT_LOCATION_ENV = ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE")
# Refs git keeps per worktree (in the worktree's own git dir); every other ref is shared.
_PER_WORKTREE_REF_PREFIXES = ("refs/bisect/", "refs/worktree/", "refs/rewritten/")
_SYMREF_HOPS = 5


def _is_object_id(text: str) -> bool:
    return len(text) in (40, 64) and all(c in "0123456789abcdef" for c in text)


def _git_dirs(root) -> tuple[str, str] | None:
    """``(git_dir, common_dir)`` for a repo whose ``.git`` sits directly in *root*."""
    dot_git = os.path.join(str(root), ".git")
    if os.path.isdir(dot_git):
        git_dir = dot_git
    elif os.path.isfile(dot_git):
        with open(dot_git, encoding="utf-8") as handle:
            line = handle.readline().strip()
        if not line.startswith("gitdir: "):
            return None
        git_dir = os.path.join(str(root), line[len("gitdir: ") :])
    else:
        return None
    common_file = os.path.join(git_dir, "commondir")
    common_dir = git_dir
    if os.path.isfile(common_file):
        with open(common_file, encoding="utf-8") as handle:
            common_dir = os.path.join(git_dir, handle.readline().strip())
    if os.path.exists(os.path.join(common_dir, "reftable")):
        return None
    return git_dir, common_dir


def _packed_ref(common_dir: str, name: str) -> str | None:
    try:
        with open(os.path.join(common_dir, "packed-refs"), encoding="utf-8") as handle:
            for line in handle:
                if line.startswith(("#", "^")):
                    continue
                sha, _, ref = line.rstrip("\n").partition(" ")
                if ref == name:
                    return sha
    except FileNotFoundError:
        return None
    return None


def _resolve_ref(git_dir: str, common_dir: str, content: str) -> str | None:
    """Follow ``ref: <name>`` indirection to an object id, as git does for ``HEAD``."""
    for _ in range(_SYMREF_HOPS):
        if _is_object_id(content):
            return content
        if not content.startswith("ref: "):
            return None
        name = content[len("ref: ") :]
        base = git_dir if name.startswith(_PER_WORKTREE_REF_PREFIXES) else common_dir
        try:
            with open(os.path.join(base, name), encoding="utf-8") as handle:
                content = handle.readline().strip()
        except (FileNotFoundError, NotADirectoryError):
            packed = _packed_ref(common_dir, name)
            return packed if packed is not None and _is_object_id(packed) else None
    return None


def _read_head_from_files(root) -> str | None:
    """HEAD's object id read straight from the git files, or ``None`` to defer to git.

    Reading two or three small files costs no process start, so a host too loaded to
    start ``git`` inside the probe bound cannot turn a passing test into a teardown
    error (geode-objective-salamander). Anything this reader does not recognise (an
    unborn branch, the reftable backend, a redirected ``GIT_DIR``, an unreadable layout)
    returns ``None``, and :func:`head` asks git, which stays the authority.
    """
    if any(os.environ.get(name) for name in _GIT_LOCATION_ENV):
        return None
    try:
        dirs = _git_dirs(root)
        if dirs is None:
            return None
        git_dir, common_dir = dirs
        with open(os.path.join(git_dir, "HEAD"), encoding="utf-8") as handle:
            content = handle.readline().strip()
        return _resolve_ref(git_dir, common_dir, content)
    except (OSError, UnicodeDecodeError):
        return None


def head(root) -> str | None:
    """The repo's current HEAD sha, or ``None`` if *root* is not a git repo.

    Read from the git files when their layout is plain; otherwise ask git through the
    bounded, re-sampled probe."""
    from_files = _read_head_from_files(root)
    if from_files is not None:
        return from_files
    try:
        return _run_git_probe(root, "rev-parse", "HEAD").stdout.strip()
    except subprocess.TimeoutExpired:
        raise
    except (OSError, subprocess.SubprocessError):
        return None


def porcelain(root) -> set[str] | None:
    """The set of ``git status --porcelain`` lines (gitignored paths excluded),
    or ``None`` if *root* is not a git repo. Compare two snapshots to find what a
    run added to the working tree."""
    try:
        out = _run_git_probe(root, "status", "--porcelain").stdout
    except subprocess.TimeoutExpired:
        raise
    except (OSError, subprocess.SubprocessError):
        return None
    return {line for line in out.splitlines() if line.strip()}


def leak_failure_message(leaked: Iterable[str]) -> str:
    """Describe a repo-root entry seen during a test without assigning its writer.

    The guard reports the entry without deleting it because another process may own it.
    """
    return (
        f"New entries appeared in REPO_ROOT during this test: {sorted(leaked)}\n"
        "They were left on disk untouched — the guard removes nothing, so clean "
        "up yourself once you know which cause applies.\n"
        "If this test created them, sandbox it — write under tmp_path (or "
        "another disposable temp dir) instead of the checkout.\n"
        "If it did not, a concurrent write from outside the suite looks "
        "identical here: a detached run shares this checkout with whatever "
        "else is touching it (an external commit, build, or editor)."
    )


def head_move_failure_message(before: str, after: str) -> str:
    """Describe a HEAD move without attributing it to the test or another process.

    Place the warning before the reset command because a reset can discard another writer's work.
    """
    return (
        f"The repo HEAD moved during this test ({before[:10]} -> {after[:10]}). "
        "The guard only samples HEAD around each test, so it cannot tell which "
        "process committed.\n"
        "If this test committed, isolate its git writes — pin "
        "GIT_CEILING_DIRECTORIES to the tmp root (see tests/scripts/graph/"
        "conftest.py::_isolate_git_from_enclosing_repo) or init the tracker as "
        "its own git repo.\n"
        "If it did not, a concurrent commit from outside the suite looks "
        "identical here: a detached run shares this checkout with whatever else "
        "is committing, rebasing, or switching branches in it.\n"
        "So do not run the recovery below until you know the moved HEAD is "
        "yours — it discards another process's commit and every "
        "uncommitted change in this worktree.\n"
        f"Once you own the commit(s), undo them with: git reset --hard {before[:10]}"
    )


def working_tree_failure_message(leaked: Iterable[str]) -> str:
    """Describe a session-wide working-tree change without assigning its writer.

    A test write and a concurrent editor, build, or commit can produce the same status entry.
    """
    entries = sorted(leaked)
    return (
        "REPO ISOLATION FAILURE: new changes appeared in the checkout during "
        "this test run. Entries from `git status --porcelain`:\n  "
        + "\n  ".join(entries[:40])
        + "\nThe backstop only diffs `git status --porcelain` around the whole "
        "session, so it cannot tell which process wrote them.\n"
        "If a test wrote them, sandbox it — write under tmp_path (or another "
        "disposable temp dir) instead of the checkout.\n"
        "If none did, a concurrent write from outside the suite looks identical "
        "here: a detached run shares this checkout with whatever else is "
        "committing, building, or saving in it."
    )
