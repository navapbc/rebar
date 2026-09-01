"""Shared store-usability predicate — the ONE place read and write agree on what
makes a tracker directory a *usable* store (bug rapt-dreadable-dromedary,
aefe-614a-2631-4117).

A directory that merely EXISTS is not a store. A store is usable iff it is a
directory AND EITHER:

1. it holds a git repository (``.git``) whose HEAD resolves — a live tracker
   clone/worktree; OR
2. it carries the rebar STORE STRUCTURE on disk — the committed
   ``.store-compat.json`` record (preferred: it is committed into the tickets
   tree and so survives ``materialize_tickets``' bare checkout, and is present
   even in a zero-ticket store), or, as a fallback for a store predating that
   record, at least one ticket event directory.

Keying on directory presence alone let a present-but-unusable store — a tracker
with no ``.git`` (the production shape: marker files only), or a ``.git`` whose
HEAD has not landed yet mid-clone — read as an EMPTY store instead of an
uninitialized one, so a broken store was indistinguishable from an empty one.

**Why clause 2 exists — the `.git`-less-but-VALID store (regression fix).** A
``.git`` + resolvable-HEAD predicate alone is WRONG for reads:
``rebar._snapshot.materialize_tickets`` checks the committed tickets tree out into
a pinned, read-only ``.tickets-tracker/`` that has NO ``.git`` at all, and the
code-review gate agents read that pinned snapshot (``LLMConfig.from_env`` points
their ticket tools at ``current_tickets_root()``) through this very predicate. A
``.git``-only clause rejected every such snapshot as ``store_uninitialized`` —
breaking the gate-agent read surface. Clause 2 recognises a materialized snapshot
(and a bare ``REBAR_TRACKER_DIR`` event store) as the usable store it is, while a
genuinely broken directory (no ``.git``, no committed record, no ticket dirs —
the production incident's marker-files-only shape) still fails BOTH clauses and
raises. A store with ZERO events is still usable via the committed record; the
predicate never keys on emptiness.

Two call sites share this predicate so the read chokepoint and the low-level
write guard cannot drift apart:

* ``rebar._reads._tracker`` — every library/MCP read funnels through it; a
  ``False`` here raises ``store_uninitialized`` rather than reducing to ``[]``.
* ``rebar._store.event_prepare._ensure_initialized`` — the write-commit guard,
  which gates on :func:`store_is_writable` = ``store_is_usable(tracker) AND .git
  present``. It shares the usability predicate so read and write cannot disagree on
  what an initialized store IS, then adds the write's inherent precondition: a write
  commits events, so it needs a real ``.git`` repository. This preserves the write
  gate's original ``.git`` requirement exactly — a ``.git``-less STRUCTURE-only
  snapshot is READABLE but NOT writable, and a mid-clone store (no resolvable HEAD,
  no structure) is rejected by :func:`store_is_usable` itself — while keeping ONE
  usability seam (no second predicate, no bypass flag). The net effect matches the
  prior write gate for every write target.

**Why the HEAD probe reuses the store's bounded git runner (write-path safety +
advisory A1).** The probe runs on the LOCKED write-commit path too
(``_ensure_initialized`` → here), so it must not be an UNBOUNDED ``git`` child
that could hold the store's write lock indefinitely. It goes through
``gitutil.run_git_bounded`` — the SAME wall-clock-bounded runner (rc-124 timeout
fold) ``push.py`` and the event-commit path already use — rather than a bare
``subprocess.run``. ``gitutil`` is itself a stdlib-only leaf (no ``rebar.*``
module imports, none of the write-contention ``flock``/index-lock machinery that
advisory A1 warned against pulling into the read chokepoint), so reusing its
bounded runner adds the bound without the coupling.

**Failure taxonomy (advisory T5b).** A CLEAN non-zero ``rev-parse`` exit means
'HEAD unresolvable' → clause 1 fails (fall through to clause 2). An
:class:`OSError` (the ``git`` binary is missing, or another environment fault)
is NOT collapsed into 'not usable' — ``run_git_bounded`` folds only
:class:`subprocess.TimeoutExpired`, so an ``OSError`` PROPAGATES and an
environment failure stays distinguishable from a genuinely mid-clone store
instead of masquerading as ``store_uninitialized``.

**Three gates, deliberately NOT unified (advisory T5e).** rebar has three
initialization gates that check DIFFERENT things on purpose: reads (here) and
``event_prepare._ensure_initialized`` gate on store USABILITY (this predicate);
the authoritative write-command gate ``_commands._seam.append_event`` gates on
``.env-id``. Reads MUST NOT adopt ``.env-id``: it is git-ignored LOCAL provenance
state that is absent immediately after a valid clone (events are versioned,
``.env-id`` is not), so a just-cloned store must stay READABLE while its first
WRITE is still correctly rejected until the provenance stamp is minted. This
predicate therefore leaves the ``.env-id`` asymmetry intact by design.

**Back-out (advisory T4).** The behavior change is isolated to this one leaf and
its two callers: reverting :func:`store_is_usable` to a bare
``os.path.isdir(tracker)`` restores the prior read/write behavior exactly, and
the seeded-mutation regression in
``tests/unit/test_reads_uninitialized_store.py`` pins both clauses against a
revert (dropping clause 2 returns the materialized-snapshot read to RAISE).
"""

from __future__ import annotations

import os

from rebar._store.compat import COMPAT_FILENAME
from rebar._store.gitutil import run_git_bounded
from rebar._store.ticket_layout import iter_ticket_dirs

# The store's git plumbing bounds every child with this wall-clock timeout; the SEAM
# (run_git_bounded's rc-124 timeout fold) is shared with push.py / event_commit_git,
# the constant is module-local exactly as those modules keep theirs (all 30s). The
# HEAD probe runs on the LOCKED write-commit path, so it must never be unbounded.
_GIT_TIMEOUT = 30


def _has_git(tracker: str) -> bool:
    """Does *tracker* hold a git repository? ``.git`` is a directory for a normal clone
    but a FILE for a linked worktree, so this uses ``os.path.exists`` (not ``isdir``).

    Writes require this: rebar commits events, so a ``.git``-less read-only materialized
    snapshot is a readable store but NOT a writable one (see :func:`store_is_writable`).
    """
    return os.path.exists(os.path.join(tracker, ".git"))


def _head_resolves(tracker: str) -> bool:
    """Does *tracker*'s git repository have a resolvable HEAD?

    Only called once ``tracker/.git`` is known to exist, so ``git -C tracker`` binds
    to THIS repo and cannot walk up to an enclosing code checkout. Bounded via the
    shared ``run_git_bounded`` (a hung git folds to rc 124 → not resolvable); an
    OSError (git missing) propagates rather than reading as 'unresolvable'.
    """
    result = run_git_bounded(tracker, "rev-parse", "--verify", "-q", "HEAD", timeout=_GIT_TIMEOUT)
    return result.returncode == 0


def _looks_like_event_dir(path: str) -> bool:
    """A ticket event directory holds its events as ``NNN-EVENTTYPE.json`` files, so a
    directory carrying at least one ``.json`` is a well-formed ticket dir. Requiring an
    event file (not merely *any* non-dot subdirectory) keeps an unrelated stray
    subdirectory — or a mid-clone store whose empty ticket dirs exist before their events
    are checked out — from reading as store structure. ``os.listdir`` on a non-directory
    entry raises ``OSError`` (``NotADirectoryError``), which we treat as 'not an event dir'.
    """
    try:
        return any(name.endswith(".json") for name in os.listdir(path))
    except OSError:
        return False


def _carries_store_structure(tracker: str) -> bool:
    """Does *tracker* carry the rebar store structure on disk (independent of ``.git``)?

    Recognises a materialized/pinned snapshot and a bare event-dir store: the committed
    ``.store-compat.json`` record (preferred — committed into the tickets tree, so it
    survives ``materialize_tickets``' checkout and is present even with zero tickets), or,
    as a fallback, at least one WELL-FORMED ticket event directory (a non-dot subdirectory
    holding ≥1 event ``.json`` file — see :func:`_looks_like_event_dir`; this mirrors, but
    is stricter than, the reducer's ticket enumeration in ``reducer._api``). A genuinely
    broken store (marker FILES only, no committed record, no event dirs) carries neither
    and is not usable.
    """
    if os.path.exists(os.path.join(tracker, COMPAT_FILENAME)):
        return True
    return any(_looks_like_event_dir(entry.path) for entry in iter_ticket_dirs(tracker))


def store_is_usable(tracker: str) -> bool:
    """Return ``True`` iff *tracker* is a usable store (see the module docstring).

    isdir AND (a live ``.git`` repo with a resolvable HEAD OR a directory that carries
    the rebar store structure). The ``.git``-existence check MUST precede the HEAD probe:
    ``git -C tracker rev-parse`` walks UP to an enclosing repository, so probing HEAD on a
    ``.git``-less tracker nested inside a code checkout would resolve the WRONG (parent)
    HEAD and report a broken store as usable. ``os.path.exists`` (not ``isdir``) because
    ``.git`` is a directory for a normal clone but a FILE for a linked worktree.
    """
    if not os.path.isdir(tracker):
        return False
    if _has_git(tracker) and _head_resolves(tracker):
        return True
    return _carries_store_structure(tracker)


def store_is_writable(tracker: str) -> bool:
    """Return ``True`` iff *tracker* is a usable store AND writable.

    The write-commit guard shares the read side's usability predicate
    (:func:`store_is_usable`) so read and write cannot disagree on what an initialized
    store is — but a WRITE carries one inherent precondition a read does not: it commits
    events, so it needs a real git repository to commit into. The ``.git``-less STRUCTURE
    clause of :func:`store_is_usable` exists only to keep a materialized, read-only snapshot
    READABLE; such a snapshot must never satisfy the write gate. Composing usability with a
    ``.git`` presence check keeps ONE usability seam (no second predicate, no bypass flag)
    while preserving the write gate's original ``.git`` requirement: a ``.git``-less
    structured directory (a snapshot) is readable but NOT writable, and a mid-clone store
    (no resolvable HEAD, no structure) is rejected by :func:`store_is_usable` itself.
    """
    return store_is_usable(tracker) and _has_git(tracker)
