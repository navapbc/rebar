"""ONE derivation of every store-relative path (story ``6f18-05de-beaf-42be``).

Five modules independently answered "where is ``.rebar`` for this store?", and three of them
carried the SAME defect in turn: a bare ``dirname`` that stops at the CALLER. A ``make
worktree`` worktree's ``.tickets-tracker`` is a SYMLINK to the canonical store while its
``.rebar`` is a real per-worktree directory, so an unresolved ``dirname`` keys every sidecar
on the *view* instead of the *store*. Each copy was fixed one site per ticket — bug
``da68-fc7c-068c-4c53`` (``nuclear-calm-heron``, the enrich drain lock and log),
``93a9-66cf-e681-4f49`` (``intangible-ladyish-vicuna``, the compaction worker lock, sweep
stamp and log) and ``conscious-weighable-spittlebug`` (the enrichment gate marker) — which is
the signature of a construct that has no owner: the same bug keeps re-entering by imitation.

The defect defeated each sidecar differently, and that variety is the argument for one owner:

* the drain/worker **locks stopped excluding anything** — two worktree views of one store each
  took "the" lock and drained (or compacted) the SAME queue concurrently;
* the sweep **stamp stopped being a store-wide clock** — a view read its own empty stamp and
  re-fired a sweep the store had just had;
* the gate **marker asserted quiet for a store another worktree had just made noisy**, and a
  mutation's clear unlinked the WRONG file, leaving the stale claim standing until its TTL;
* the **logs** were written into — and deleted with — an ephemeral worktree.

**The resolution lives INSIDE the derivation, not at the call sites.** That is the whole point:
a caller cannot defeat an invariant it never gets to express. Every path here is derived from
:attr:`StorePaths.canonical` — the tracker resolved through symlinks by
:func:`rebar._store.lock.canonical_tracker`, the very same resolution the store write lock
uses — so a caller reaching the store through a symlink lands on exactly the paths a caller
holding its real path does.

Resolution **never raises**. These derivations run on best-effort background paths (the tail of
a close, the drain gate on ordinary writes), where a background concern must not fail the
operation that triggered it; an ``OSError`` degrades to the raw tracker value, which is the
pre-existing behaviour. This module is a low-level leaf — it imports nothing from
``rebar.llm`` or ``rebar._commands``, and it reaches
:mod:`rebar._store.lock` lazily THROUGH THE MODULE so that a test holding
``monkeypatch.setattr(lock, "canonical_tracker", ...)`` is honoured at call time (the
late-binding discipline :mod:`rebar._store.ensures` documents) and so importing this module
cannot cycle.
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

#: The atoms that, IN CONJUNCTION with the ``.rebar`` literal, constitute the tracker-sibling
#: derivation this module owns. The bare literal is deliberately NOT the signature: roughly
#: three dozen sites legitimately join ``.rebar`` to an explicit ``repo_root`` (prompts,
#: scratch, the usage log, run snapshots), and those carry no symlink hazard. Only a
#: parent-of-the-tracker step makes it THIS construct.
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
