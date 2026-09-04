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

# Volatile REPO_ROOT state dirs a test must never write into, but which exist
# locally because this checkout dogfoods rebar (so ``.rebar/`` is always present).
# Watched ONE level deep so a leak INTO them is caught locally — not only on a
# fresh CI checkout where the same write would instead create a new top-level
# entry (the false-green/false-red split behind bug hurt-brow-swan). Kept shallow
# (one extra ``os.listdir`` each) so the per-test guard stays cheap; dirs with
# legitimate per-test churn (``.git/``, ``.tickets-tracker/``, ``.venv/``) are
# deliberately NOT watched.
LEAK_WATCH_SUBDIRS = (".rebar",)
GIT_PROBE_TIMEOUT_SECONDS = 5

# A timed-out sample of a READ-ONLY, IDEMPOTENT probe (``rev-parse``/``status``) is not
# evidence of anything, so it is re-sampled rather than reported. The guard runs once per
# test — ~19k times in the macOS full-suite sweep, three xdist workers deep — on a runner
# where ORDINARY fixture setup was measured at 4.562s against this 5s per-attempt bound.
# One starved sample there is contention, and erroring the innocent test that happened to
# be running is a false red that turns the whole branch head red (bug 860b-28eb-10c0-4249).
#
# Retries carry NO sleep: the elapsed per-attempt timeout is itself the backoff, and
# sleeping here would re-introduce the patched-``time.sleep`` hazard that
# ``_stable_subprocess_time`` exists to defend against.
#
# The LAST attempt's ``TimeoutExpired`` still propagates, and that is load-bearing: a probe
# that can never sample must fail LOUDLY rather than silently cease to detect a HEAD move.
# Retrying absorbs a TRANSIENT stall; it must never absorb a PERSISTENT one.
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


def head(root) -> str | None:
    """The repo's current HEAD sha, or ``None`` if *root* is not a git repo."""
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
    """The REPO_ROOT leak guard's failure text for *leaked* entries.

    The guard diffs REPO_ROOT around each test, so it cannot tell a leak from a
    write that landed from outside the suite — the message names both causes
    rather than asserting the one it cannot know.

    For the same reason the guard does not delete what it reports, so the text
    says so explicitly: whoever reads this is the only one who can tell the two
    causes apart, and therefore the only one who can safely clean up.
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
    """The HEAD-move guard's failure text for a move from *before* to *after*.

    The guard's only evidence is two ``git rev-parse HEAD`` samples taken around
    the test, and a sha inequality across that window carries no attribution: a
    commit made by the test and one made by another process sharing this checkout
    are the same kind of object, by the same user, in the same repo. So the text
    names both causes instead of asserting the one it cannot know.

    That is also why the recovery command is fenced by a warning placed *before*
    it: ``git reset --hard`` is right for whoever owns the commit and destructive
    for everyone else, and a caveat read after the command is read too late.
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
    """The session working-tree backstop's failure text for *leaked* entries.

    The backstop diffs two ``git status --porcelain`` snapshots taken around the
    whole session, and an entry appearing across that window carries no writer
    identity: a stray test write and a concurrent commit, build, or editor save
    in the same checkout produce the same porcelain line. So the text names both
    causes instead of asserting the one it cannot know — the same limit the
    sibling guards hit (tickets 746c-185a, hot-guessable-ungulate).
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
