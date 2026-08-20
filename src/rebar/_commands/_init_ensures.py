"""Tickets-branch convergence units for the ensure registry.

These are the check-then-act, idempotent, drift-correcting **ensure units** that
:func:`rebar._store.ensures.run_ensures` sweeps on every ``init`` and re-init, plus the
two tickets-branch content templates the units are the sole readers of.

**Why they are their own module.** They are not part of the ``init`` command's call
graph: :mod:`rebar._commands.init` provisions the tracker worktree in the HOST repo
(resolve the repo root, write ``.git/info/exclude``, take the init lock, mount or create
the ``tickets`` branch, symlink from a linked worktree) and never calls a unit. Their
sole caller is :func:`rebar._store.ensures._registry`, which lazy-imports them and
dispatches them by id. They lived in ``init`` only because they own ``_GITIGNORE`` /
``_GITATTRIBUTES``; that ownership travels here with them.

Every unit is check-then-act, so a converged store reports ``ok`` and makes zero git
commits. ``run_ensures`` catches a raising unit (skip-and-continue → ``failed``), so a
unit may raise rather than defend against every failure itself.

The units stay reachable as ``init._<name>``: :mod:`rebar._commands.init` re-exports
them, because ``ensures._registry()``, the ensure drift-matrix interface test, ADR 0051,
``docs/migrations.md``, ``docs/scale-envelope.md`` and ``_store/sync.py`` all reach them
through that path.

``untrack-runtime-markers`` deliberately stays in :mod:`rebar._commands.init`: it is not
a content converger (it has no template — it repairs a legacy INDEX with
``git rm --cached``) and it reads the ``_UNTRACK_BATCH`` budget from that module's
globals.
"""

from __future__ import annotations

import os
import subprocess

from rebar._store.ensures import APPLIED_MARKER, HINTED_MARKER, EnsureOutcome
from rebar._store.gitutil import run_git_write
from rebar._store.lock import MKDIR_LOCK_NAME, WRITE_LOCK_NAME
from rebar.graph._cache import _GRAPH_CACHE_FILE
from rebar.reducer.marker import ARCHIVE_MARKER_NAME, MARKER_LOCK_NAME

# Runtime artifacts created in the tracker worktree that must never be committed.
# The lock/cache/marker names are sourced from their defining constants so this ignore
# list cannot drift from them (bug stem-ewe-tomb). The flock write-lock file is
# intentionally NOT unlinked on release (deleting it races other lockers), so it
# persists after every write; the graph cache is rewritten on every graph compile.
_GITIGNORE = f""".env-id
.closure-key
.signing-key
.opcert-key
.opcert-key.pub
.state-cache
.scratch/
.cache.json
..cache.json.*.tmp
{WRITE_LOCK_NAME}
{MKDIR_LOCK_NAME}/
{_GRAPH_CACHE_FILE}
*/{ARCHIVE_MARKER_NAME}
*/{MARKER_LOCK_NAME}
{APPLIED_MARKER}
{HINTED_MARKER}
"""

_GITATTRIBUTES = """# Shared mutable root files are per-pass derived CACHES the reconciler rebuilds,
# not ticket events (uuid-named ticket dirs never collide, so they never need a
# merge policy). On a union reconverge (sync.py) keep OUR copy and let the next
# reconciler pass rebuild the loser. merge=union is WRONG here — it line-unions
# JSON into invalid JSON. The 'ours' driver is defined in git config by init
# (merge.ours.driver=true); without it these patterns are silently ignored.
.bridge_state/* merge=ours
"""


# The units' git seam. A thin non-raising wrapper over the shared
# ``_store.gitutil.run_git_write`` seam — the same two-line form ``init`` keeps for its
# own provisioning calls, so neither module reaches into the other's globals.
# raw-git-ok: store-maintenance command, seam-internal
def _git(cwd: str, *args: str) -> subprocess.CompletedProcess:
    return run_git_write(cwd, *args, check=False)


def _gc_config_unit(tracker: str) -> EnsureOutcome:
    """Keep stock ``git gc`` but run it FOREGROUND on the tickets worktree, never
    detached (epic 97e7 / P1.4, corrected by bug 88eb / amicable-unsure-barasinga).

    The tickets store is a linked worktree that SHARES the parent repo's object
    database. WU-1 originally set ``gc.autoDetach=true`` so a triggered gc would fork
    and "never serialize a foreground ticket write" — but that is exactly the hazard:
    a DETACHED ``git gc`` / ``git maintenance run --auto`` (git >= 2.47 runs the latter)
    repacks the SHARED object DB in the background, OUTSIDE rebar's write lock, racing
    concurrent writers and corrupting the store (``invalid object`` / ``Error building
    trees`` -> dropped writes; bug 88eb, proven on git 2.54). git's own docs warn a
    concurrent ``git gc`` "may corrupt the repository". WU-1's "safe by construction"
    argument only covers SERIAL reachability; it never accounted for a CONCURRENT
    background repack.

    Fix: keep auto-gc ENABLED (so loose growth is still bounded — the WU-1 goal) but
    force it to run in the FOREGROUND of the write command that triggers it. That
    command already holds rebar's write lock, so the repack runs serialized under the
    lock (no concurrent writer) instead of detaching past it. Three idempotent steps
    (an existing tracker self-heals on any ensure sweep):

    - ``--unset gc.auto`` sheds any stale ``gc.auto=0`` (auto-gc stays at git's default
      threshold, so repack still fires and bounds loose-object growth).
    - ``gc.autoDetach=false`` — a triggered ``git gc --auto`` runs foreground.
    - ``maintenance.autoDetach=false`` — git >= 2.47 routes auto-maintenance through
      ``git maintenance run --auto`` and honors THIS knob (``gc.autoDetach`` is only its
      fallback), so BOTH must be false or the background repack still detaches.

    Check-then-act: acts only when a value is off the desired state, so a converged
    tracker reports ``ok`` and mutates nothing (ensure-registry unit)."""
    changed = False
    if _git(tracker, "config", "--get", "gc.auto").returncode == 0:
        _git(tracker, "config", "--unset", "gc.auto")
        changed = True
    for key in ("gc.autoDetach", "maintenance.autoDetach"):
        if _git(tracker, "config", "--get", key).stdout.strip() != "false":
            _git(tracker, "config", key, "false")
            changed = True
    return EnsureOutcome(
        "gc-config",
        "changed" if changed else "ok",
        "gc.auto unset + gc.autoDetach=false + maintenance.autoDetach=false",
    )


def _merge_ours_unit(tracker: str) -> EnsureOutcome:
    """Define the ``ours`` merge driver the ``.gitattributes`` references (epic 97e7
    / WU-3). ``true`` always exits 0, leaving OUR version of a conflicted path in
    place. Without this, ``merge=ours`` in ``.gitattributes`` is silently ignored
    and the shared mutable root files conflict on a union reconverge. Local config
    (per clone; shared by symlinked worktrees via the common git dir).

    Check-then-act: sets the driver only when it is not already ``true`` (ensure-
    registry unit), so a converged clone reports ``ok``."""
    if _git(tracker, "config", "--get", "merge.ours.driver").stdout.strip() == "true":
        return EnsureOutcome("merge-ours", "ok", "merge.ours.driver=true")
    _git(tracker, "config", "merge.ours.driver", "true")
    return EnsureOutcome("merge-ours", "changed", "set merge.ours.driver=true")


# raw-git-ok: store-maintenance command, seam-internal
def _gitattributes_unit(tracker: str) -> EnsureOutcome:
    """Commit the tickets-branch ``.gitattributes`` (create-if-absent, idempotent),
    so a union merge keeps OUR copy of the per-pass mutable root files instead of
    wedging. Pairs with :func:`_merge_ours_unit` (the driver it names).

    Tree-checks the committed blob first, so a converged store makes zero commits and
    reports ``ok`` (ensure-registry unit; run_ensures catches any raise → ``failed``)."""
    show = _git(tracker, "show", "tickets:.gitattributes")
    if show.returncode != 0:
        with open(os.path.join(tracker, ".gitattributes"), "w", encoding="utf-8") as f:
            f.write(_GITATTRIBUTES)
        _git(tracker, "add", ".gitattributes")
        _git(
            tracker,
            "commit",
            "-q",
            "--no-verify",
            "-m",
            "chore: add .gitattributes merge=ours for shared mutable root files (epic 97e7)",
        )
        return EnsureOutcome("gitattributes", "changed", "created .gitattributes")

    return EnsureOutcome("gitattributes", "ok", ".gitattributes converged")


# raw-git-ok: store-maintenance command, seam-internal
def _gitignore_unit(tracker: str) -> EnsureOutcome:
    """Ensure the tickets-branch ``.gitignore`` carries every runtime-artifact entry
    (ensure-registry unit). Tree-checks the committed blob first, so it commits only
    when creating it or appending a missing line — a converged store reports ``ok``
    and makes zero commits."""
    show = _git(tracker, "show", "tickets:.gitignore")
    if show.returncode != 0:
        with open(os.path.join(tracker, ".gitignore"), "w", encoding="utf-8") as f:
            f.write(_GITIGNORE)
        _git(tracker, "add", ".gitignore")
        _git(
            tracker,
            "commit",
            "-q",
            "--no-verify",
            "-m",
            "chore: add .gitignore for env-id, state-cache, scratch, and reducer cache",
        )
        return EnsureOutcome("gitignore", "changed", "created .gitignore")
    # Migration (bug stem-ewe-tomb): an existing tracker's committed .gitignore may
    # predate the lock/cache entries. Append any missing lines (idempotent — the
    # sweep re-runs harmlessly) so existing stores stop surfacing the artifacts.
    existing = set(show.stdout.splitlines())
    missing = [ln for ln in _GITIGNORE.splitlines() if ln and ln not in existing]
    if not missing:
        return EnsureOutcome("gitignore", "ok", ".gitignore converged")
    path = os.path.join(tracker, ".gitignore")
    body = show.stdout if show.stdout.endswith("\n") else show.stdout + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(body + "\n".join(missing) + "\n")
    _git(tracker, "add", ".gitignore")
    _git(
        tracker,
        "commit",
        "-q",
        "--no-verify",
        "-m",
        "chore: gitignore write-lock and graph-cache runtime artifacts",
    )
    return EnsureOutcome("gitignore", "changed", f"added {len(missing)} .gitignore line(s)")


# raw-git-ok: store-maintenance command, seam-internal
def _store_compat_unit(tracker: str) -> EnsureOutcome:
    """Stamp the COMMITTED store-compatibility record ``.store-compat.json`` (story
    21dd). A v1.0 rebar reads this record before any mutating/publishing operation and
    fails CLOSED on a record it cannot interpret (see :mod:`rebar._store.compat`); an
    ABSENT record is implicit-legacy and passes through, so writing it is purely
    additive and rollback-safe.

    Tree-checks the committed blob first (like :func:`_gitignore_unit`) so a converged
    store makes zero commits and reports ``ok``. The record is a COMMITTED tickets-branch
    file — NOT gitignored — so it must be ``git add``ed + committed by the unit (the
    ensure sweep itself does not commit)."""
    from rebar._store import compat

    if _git(tracker, "show", f"tickets:{compat.COMPAT_FILENAME}").returncode == 0:
        return EnsureOutcome("store-compat", "ok", "compat record present")
    compat.write_compat_record(tracker)
    _git(tracker, "add", compat.COMPAT_FILENAME)
    _git(
        tracker,
        "commit",
        "-q",
        "--no-verify",
        "-m",
        "chore: add .store-compat.json store-compatibility record (story 21dd)",
    )
    return EnsureOutcome("store-compat", "changed", "wrote .store-compat.json")
