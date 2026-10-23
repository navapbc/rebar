"""Tickets-branch ensure units and their content templates.
The store registry calls these idempotent, drift-correcting units on init.
Converged units make no commit. init re-exports them for existing callers.
Runtime-marker index repair stays in init because it has no template."""

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
    """Keep automatic garbage collection enabled but in the write lock's
    foreground. Clear stale gc.auto=0 and set gc.autoDetach and
    maintenance.autoDetach false. Both auto routes must avoid detached
    repacking of the shared object database while concurrent writers run.
    Act only when the tracked configuration differs."""
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
    """Set the local ours merge driver to true when needed.
    This makes merge=ours keep the local version of shared root files and
    lets a converged clone report ok without a write."""
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
    # Append missing lock and cache ignore lines to older ticket stores.
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
    """Ensure the committed .store-compat.json record exists.
    Check the tree first so a converged store makes no commit. An absent
    record remains readable as legacy. An unrecognized record blocks writes."""
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
