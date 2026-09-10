"""Derive every store-relative path from the canonical tracker.

Worktree tracker symlinks must share locks, stamps, markers, and logs with the underlying store.
:class:`StorePaths` therefore resolves the tracker before deriving its sibling ``.rebar``
directory. Resolution failures fall back to the supplied tracker and never raise. The lock
module is imported lazily through its module object, which avoids a cycle and preserves
call-time monkeypatching of ``canonical_tracker``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

#: The per-clone state directory, always a sibling of the CANONICAL tracker dir.
_REBAR_DIR_NAME = ".rebar"


def _canonical_tracker(tracker: str | os.PathLike[str]) -> str:
    """*tracker* resolved through symlinks, degrading to the raw value.

    Delegates to :func:`rebar._store.lock.canonical_tracker` rather than re-deriving the
    resolution, so store paths and the store write lock can never disagree. Imported lazily
    and THROUGH THE MODULE (never ``from ... import canonical_tracker``) so the lookup happens
    at call time and a monkeypatched resolver is captured.
    """
    try:
        from rebar._store import lock as _lock

        return _lock.canonical_tracker(tracker)
    except OSError:
        return str(tracker)


def _rebar_dir(tracker: str | os.PathLike[str]) -> str:
    """The repo's ``.rebar/`` — the sibling of the CANONICAL ``.tickets-tracker`` dir.

    The single owner of the tracker→``.rebar`` sibling convention; see the module docstring
    for why resolving first is load-bearing rather than cosmetic.
    """
    return os.path.join(os.path.dirname(_canonical_tracker(tracker)), _REBAR_DIR_NAME)


#: The escape marker for a legitimate second tracker-sibling derivation. A reason is
#: MANDATORY -- a bare marker would let the exception hide, so it is a violation in its own
#: right (the rule ``scripts/check_raw_git_writes.py`` enforces for ``# raw-git-ok:``).
_STORE_PATH_OK_RE = re.compile(r"#\s*store-path-ok:(.*)$")

#: A tracker-parent operation combined with ``.rebar`` identifies the owned sibling derivation.
#: The literal alone remains valid when joined to an explicit repository root.
_PARENT_ATOMS = ("os.path.dirname(", ".parent")


def _offending_line(line: str) -> str | None:
    """Why *line* is an unsanctioned tracker-sibling ``.rebar`` derivation, else ``None``.

    Split out from the tree scan in ``tests/unit/store/test_store_paths.py`` so the guard can
    be proven to FLAG, not merely to pass: a scan that only ever reports "no offender exists
    today" reports exactly the same thing when its matcher is broken.
    """
    if '".rebar"' not in line and "'.rebar'" not in line:
        return None
    if not any(atom in line for atom in _PARENT_ATOMS):
        return None
    marker = _STORE_PATH_OK_RE.search(line)
    if marker is None:
        return "tracker-sibling '.rebar' derivation outside rebar._store.paths"
    if marker.group(1).strip():
        return None
    return "store-path-ok marker requires a reason"


def _resolve_pointer(target: str, base: str) -> str:
    """A git ``gitdir:``/``commondir`` pointer made absolute against *base*."""
    if not os.path.isabs(target):
        target = os.path.join(base, target)
    return os.path.normpath(target)


@dataclass(frozen=True)
class StorePaths:
    """Every path derived from one store, keyed on the CANONICAL tracker.

    Constructed from whatever tracker path the caller happens to hold — a real path, or a
    worktree view's symlink — and answers the same paths either way.
    """

    tracker: str | os.PathLike[str]

    @property
    def canonical(self) -> str:
        """*tracker* resolved through symlinks; the raw value if resolution fails."""
        return _canonical_tracker(self.tracker)

    @property
    def rebar_dir(self) -> str:
        """The ``.rebar/`` state directory beside the canonical tracker."""
        return _rebar_dir(self.tracker)

    def sidecar(self, name: str) -> str:
        """A store-wide sidecar file (a lock, a stamp, a marker) inside ``.rebar/``."""
        return os.path.join(self.rebar_dir, name)

    def log(self, name: str) -> str:
        """A store-wide log file inside ``.rebar/``. Same directory as :meth:`sidecar`, named
        separately because the two have different lifetimes and a caller reads better for
        saying which it means."""
        return os.path.join(self.rebar_dir, name)

    @property
    def git_dir(self) -> str:
        """The tracker's git dir, resolved WITHOUT a git subprocess.

        ``<tracker>/.git`` is a directory in a normal clone and a FILE holding
        ``gitdir: <path>`` in a linked worktree or a submodule. Both are handled here rather
        than by shelling out to ``git rev-parse --git-dir``: this runs on the write path of
        every push, and a status read must stay a file read. Falls back to ``<tracker>/.git``
        if the pointer is unreadable — the caller's write then fails and is swallowed, which
        is the correct best-effort degradation.
        """
        dot_git = os.path.join(self.canonical, ".git")
        if os.path.isdir(dot_git):
            return dot_git
        try:
            with open(dot_git, encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("gitdir:"):
                        return _resolve_pointer(line.partition(":")[2].strip(), self.canonical)
        except OSError:
            pass
        return dot_git

    @property
    def git_common_dir(self) -> str:
        """The git COMMON dir — what a linked worktree shares with its main checkout.

        A linked worktree's git dir holds a ``commondir`` file pointing at it; without one
        (an ordinary clone) the git dir IS the common dir. Read as a file rather than via
        ``git rev-parse --git-common-dir`` for the same reason as :attr:`git_dir`.
        """
        git_dir = self.git_dir
        try:
            with open(os.path.join(git_dir, "commondir"), encoding="utf-8") as fh:
                target = fh.read().strip()
        except OSError:
            return git_dir
        return _resolve_pointer(target, git_dir) if target else git_dir
