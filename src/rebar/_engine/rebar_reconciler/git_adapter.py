"""The reconciler's single git seam.

Every ``git`` subprocess the reconciler runs flows through this module. Before it
existed, ~8 reconciler modules each hand-rolled ``subprocess.run(["git", "-C",
str(repo_root), …])`` inline — the same shape copied a dozen ways, each with its
own drift in ``check``/``text``/``env`` handling and no single place to see (or
change) how the reconciler talks to git.

This module is deliberately **thin**: it adds NO new subprocess wrapper. It is
built ON TOP of :func:`rebar._store.gitutil.run_git` — the one shared
``subprocess.run(["git", …])`` shape for the whole codebase — and only

  * re-exports :func:`run_git` (so a module needing a bespoke argv still goes
    through the shared seam rather than calling ``subprocess`` itself), and
  * names the reconciler's higher-level operations (``rev_parse``,
    ``commit_tree``, ``remote_get_url``, …) as functions that each take an
    **explicit** ``repo_root`` and delegate to :func:`run_git`.

Each op pins ``git -C <repo_root>`` via ``run_git``. The two attestation ops
(:func:`verify_commit`, :func:`commit_email`) accept ``repo_root=None`` to
preserve their historical CWD-relative behaviour (``git`` with no ``-C``) —
:func:`run_git` omits the ``-C`` prefix when ``cwd`` is ``None``.

Behaviour is byte-for-byte the same as the inline calls this replaced: the same
argv, the same ``check`` posture (call sites that inspected ``returncode`` pass
``check=False`` here too), and the same ``env``/``timeout``/``text`` handling.
"""

from __future__ import annotations

import os
import subprocess

from rebar._store.gitutil import run_git

__all__ = [
    "TICKETS_BRANCH",
    "TICKETS_REF",
    "run_git",
    "rev_parse",
    "cat_file_exists",
    "read_tree",
    "rm_cached",
    "write_tree",
    "commit_tree",
    "update_ref",
    "add",
    "diff_cached_names",
    "commit",
    "log_format",
    "remote_get_url",
    "verify_commit",
    "commit_email",
]

# ---------------------------------------------------------------------------
# Named refs/paths (were bare string literals scattered across the reconciler).
# The tickets orphan branch that the reconciler commits binding-store snapshots
# and legacy-lock purges onto. ``__main__``'s binding-store commit-back pins this
# branch by name; keep it in ONE place so the ref/short-name pair can't drift.
# ---------------------------------------------------------------------------

TICKETS_BRANCH = "tickets"  # short name / ``<branch>:<path>`` spec + ``rev-parse`` target
TICKETS_REF = f"refs/heads/{TICKETS_BRANCH}"  # fully-qualified ref for ``update-ref``

RepoRoot = str | os.PathLike[str]


def _root(repo_root: RepoRoot) -> str:
    """Normalise *repo_root* to the ``str`` the inline call sites passed to git.

    The historical calls all did ``"-C", str(repo_root)`` — mirror that so the
    argv (which tests assert on) is identical.
    """
    return str(repo_root)


# ---------------------------------------------------------------------------
# Read / query ops
# ---------------------------------------------------------------------------


def rev_parse(
    repo_root: RepoRoot, ref: str, *, check: bool = False, text: bool = True
) -> subprocess.CompletedProcess:
    """``git -C <repo_root> rev-parse <ref>`` → the :class:`CompletedProcess`.

    Defaults to ``check=False`` because the callers (concurrency snapshot) inspect
    ``returncode`` and fall back rather than raise.
    """
    return run_git(_root(repo_root), "rev-parse", ref, check=check, text=text)


def cat_file_exists(repo_root: RepoRoot, spec: str) -> bool:
    """True iff ``git cat-file -e <spec>`` exits 0 (object *spec* exists).

    *spec* is a git object spec such as ``"tickets:<path>"``. Never raises — the
    inline call passed no ``check`` (so ``check=False``) and read ``returncode``.
    """
    return run_git(_root(repo_root), "cat-file", "-e", spec, check=False).returncode == 0


def diff_cached_names(repo_root: RepoRoot, *, check: bool = True) -> str:
    """``git diff --cached --name-only`` stdout (one staged path per line)."""
    return run_git(_root(repo_root), "diff", "--cached", "--name-only", check=check).stdout


def log_format(
    repo_root: RepoRoot,
    sha: str,
    fmt: str,
    *,
    check: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    """``git log -1 <sha> --format=<fmt>`` → the :class:`CompletedProcess`.

    Defaults ``check=False`` so a bad/absent sha yields a non-zero
    ``returncode`` the caller can inspect rather than an exception.
    """
    return run_git(
        _root(repo_root), "log", "-1", sha, f"--format={fmt}", check=check, timeout=timeout
    )


def remote_get_url(
    repo_root: RepoRoot, remote: str, *, check: bool = False
) -> subprocess.CompletedProcess:
    """``git remote get-url <remote>`` → the :class:`CompletedProcess`.

    Defaults ``check=False``: the advisory-lock caller treats a non-zero exit
    (remote not configured) as "pure-local", not an error.
    """
    return run_git(_root(repo_root), "remote", "get-url", remote, check=check)


# ---------------------------------------------------------------------------
# Detached-index / commit-back plumbing (binding-store snapshot + legacy purge)
# ---------------------------------------------------------------------------


def read_tree(
    repo_root: RepoRoot,
    tree: str,
    *,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """``git read-tree <tree>`` (populate the index — usually a detached one via ``env``)."""
    return run_git(_root(repo_root), "read-tree", tree, check=check, env=env)


def rm_cached(
    repo_root: RepoRoot,
    *paths: str,
    ignore_unmatch: bool = True,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """``git rm --cached [--ignore-unmatch] <paths…>`` (prune index entries)."""
    args = ["rm", "--cached"]
    if ignore_unmatch:
        args.append("--ignore-unmatch")
    args.extend(paths)
    return run_git(_root(repo_root), *args, check=check, env=env)


def write_tree(
    repo_root: RepoRoot, *, env: dict[str, str] | None = None, check: bool = True
) -> str:
    """``git write-tree`` → the written tree OID (stripped)."""
    return run_git(_root(repo_root), "write-tree", check=check, env=env).stdout.strip()


def commit_tree(
    repo_root: RepoRoot,
    tree: str,
    *,
    parent: str,
    message: str,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> str:
    """``git commit-tree <tree> -p <parent> -m <message>`` → the new commit OID (stripped)."""
    return run_git(
        _root(repo_root),
        "commit-tree",
        tree,
        "-p",
        parent,
        "-m",
        message,
        check=check,
        env=env,
    ).stdout.strip()


def update_ref(
    repo_root: RepoRoot,
    ref: str,
    new_value: str,
    old_value: str | None = None,
    *,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """``git update-ref <ref> <new> [<old>]`` (CAS-advance a ref when *old_value* is given)."""
    args = ["update-ref", ref, new_value]
    if old_value is not None:
        args.append(old_value)
    return run_git(_root(repo_root), *args, check=check)


def add(repo_root: RepoRoot, *paths: str, check: bool = True) -> subprocess.CompletedProcess:
    """``git add <paths…>`` (stage the given paths only — never ``-A``)."""
    return run_git(_root(repo_root), "add", *paths, check=check)


def commit(
    repo_root: RepoRoot,
    message: str,
    *,
    no_verify: bool = False,
    quiet: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """``git commit [--no-verify] [-q] -m <message>``."""
    args = ["commit"]
    if no_verify:
        args.append("--no-verify")
    if quiet:
        args.append("-q")
    args.extend(["-m", message])
    return run_git(_root(repo_root), *args, check=check)


# ---------------------------------------------------------------------------
# Attestation ops — support ``repo_root=None`` (CWD-relative, no ``-C``).
#
# ``verify_attested_commit`` (ticket c9a5 / F14) grew an optional ``repo_root``
# so a band invoked from a sibling worktree looks the sha up in the RIGHT repo.
# When ``repo_root`` is None the historical CWD-relative behaviour must be
# preserved — no ``-C`` — which ``run_git(None, …)`` provides.
# ---------------------------------------------------------------------------


def verify_commit(
    repo_root: RepoRoot | None, sha: str, *, timeout: float | None = 15
) -> subprocess.CompletedProcess:
    """``git [-C <repo_root>] verify-commit <sha>`` (GPG signature check).

    ``repo_root=None`` runs ``git`` in the caller's CWD (no ``-C``). ``check`` is
    always False — the caller reads ``returncode`` (and catches a timeout).
    """
    cwd = None if repo_root is None else _root(repo_root)
    return run_git(cwd, "verify-commit", sha, check=False, timeout=timeout)


def commit_email(
    repo_root: RepoRoot | None, sha: str, *, timeout: float | None = 10
) -> subprocess.CompletedProcess:
    """``git [-C <repo_root>] log -1 --format=%ae <sha>`` → the committer-email result.

    ``repo_root=None`` runs ``git`` in the caller's CWD (no ``-C``). ``check`` is
    always False — the caller reads ``returncode``/``stdout``.
    """
    cwd = None if repo_root is None else _root(repo_root)
    return run_git(cwd, "log", "-1", "--format=%ae", sha, check=False, timeout=timeout)
