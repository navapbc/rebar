"""Pass-lock lifecycle for the reconciler orchestrator.

The steps ``__main__.main()`` runs to GET the pass lock, in the order it runs
them: the steal kill-switch, the held-lock steal/re-acquire resolution, the
one-time legacy lock-file migration, and the final adopt-or-acquire. They form
one cluster in the call graph — ``_post_pause_preflight`` consults
``_lock_steal_enabled``, whose answer decides whether ``_resolve_held_lock``
runs at all, whose returned OID ``_acquire_or_adopt_pass_lock`` then adopts.

Kept in a sibling module so ``__main__`` stays under the module-size cap, the
same reason ``_preflight.py`` and ``_heartbeat.py`` live beside it. The lock's
own mechanics stay where they were: ``_advisory_lock.py`` owns the ref backend,
``_ref_lock.py`` the CAS/lease primitives, ``_heartbeat.py`` the lease renewal.
This module holds only the orchestrator-side sequencing over them.

``__main__`` re-imports every name here into its own namespace, so the existing
``patch.object(main_mod, …)`` / ``monkeypatch.setattr(main_mod, …)`` targets in
the test suite keep resolving and ``main()`` keeps reading the patched globals.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_LEGACY_LOCK_FILES = (".reconciler-pass-lock", ".reconciler-phase-gate")


def _lock_steal_enabled() -> bool:
    """Whether the held-lock path may steal an expired lease (story 9622).

    Kill-switch ``REBAR_RECONCILER_LOCK_STEAL`` — default ON. Only an explicit
    falsy value (``0``/``false``/``no``/``off``/empty) reverts to the old
    unconditional exit-3 behavior (ops back-out without a deploy).
    """
    raw = os.environ.get("REBAR_RECONCILER_LOCK_STEAL", "1")  # read-via: kill-switch
    return raw.strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
        "",
    )


def _resolve_held_lock(advisory, pass_id, repo_root, *, acquire_fn):
    """Resolve a HELD pass lock via steal (story 9622). Steal-enabled precondition.

    Returns ``(exit_code, lock_oid, acquired)``:
      - steal wins (a new oid)              -> ``(None, stolen_oid, True)``  [case 1: adopt]
      - steal None + ref still held          -> ``(3, None, False)``          [case 2: live holder]
      - steal None + freed + acquire wins    -> ``(None, acquired_oid, True)``[case 3a]
      - steal None + freed + acquire loses   -> ``(3, None, False)``          [case 3b]

    ``steal()`` (via ``advisory.steal_pass_lock``) IS the skew-proof expiry test —
    a returned oid means the lease was stale. ``None`` means the holder is live OR
    the ref freed during the steal sleep; a re-read discriminates. On the freed
    fork we acquire normally via ``acquire_fn`` (a lost race raises
    ``advisory.ReconcileLockError`` -> yield).
    """
    stolen_oid = advisory.steal_pass_lock(pass_id, repo_root)
    if stolen_oid is not None:
        return (None, stolen_oid, True)
    if advisory.check_pass_lock(repo_root):
        return (3, None, False)
    # freed during our steal sleep -> acquire normally (win: proceed; lose: yield).
    try:
        return (None, acquire_fn(), True)
    except advisory.ReconcileLockError:
        return (3, None, False)


# The composition below runs entirely in a DETACHED temp index (GIT_INDEX_FILE, set at
# the call site) and publishes through a CAS ref-advance (update_ref with the observed
# old OID), so it never touches the worktree or the main index and a concurrent writer
# loses the CAS rather than corrupting state. The CAS *is* the transaction here; taking
# the tracker write lock around it would add no safety.
# raw-git-ok: detached temp index + CAS ref-advance; never touches the worktree or index
def _purge_committed_reconciler_locks(repo_root: Path) -> None:
    """Remove any legacy ``.reconciler-*`` lock files still committed on the tickets
    branch (epic dust-troth-naval / C4 migration).

    The lock moved to ``refs/reconciler/*``; a repo initialized under the old file
    backend may still carry committed ``.reconciler-pass-lock`` / ``.reconciler-phase-gate``
    blobs on the ``tickets`` branch. This deletes them once via a single ref-advance
    CAS commit. Idempotent (no-op when none are present) and best-effort: any git
    failure is logged and swallowed so it never aborts the pass.
    """
    from rebar_reconciler import git_adapter

    try:
        present = [
            f
            for f in _LEGACY_LOCK_FILES
            if git_adapter.cat_file_exists(repo_root, f"{git_adapter.TICKETS_BRANCH}:{f}")
        ]
        if not present:
            return
        old = git_adapter.rev_parse(
            repo_root, git_adapter.TICKETS_BRANCH, check=True
        ).stdout.strip()
        # Prune the legacy paths in a DETACHED temp index (read-tree → rm --cached →
        # write-tree → commit-tree), then CAS-advance refs/heads/tickets — the main
        # worktree/index is never touched, and the CAS makes a concurrent writer safe.
        env = {**os.environ, "GIT_INDEX_FILE": str(repo_root / ".git" / "reconciler-purge-index")}
        git_adapter.read_tree(repo_root, old, env=env)
        git_adapter.rm_cached(repo_root, *present, env=env)
        new_tree = git_adapter.write_tree(repo_root, env=env)
        new_commit = git_adapter.commit_tree(
            repo_root,
            new_tree,
            parent=old,
            message=(
                "chore(reconciler): drop legacy .reconciler-* lock files "
                "(moved to refs/reconciler/*)"
            ),
            env=env,
        )
        git_adapter.update_ref(repo_root, git_adapter.TICKETS_REF, new_commit, old)
        print(
            f"reconcile: purged legacy committed lock files {present} from the tickets branch",
            file=sys.stderr,
        )
    except Exception as exc:  # noqa: BLE001 — migration is best-effort, never aborts the pass
        print(f"WARN: legacy .reconciler-* purge skipped: {exc!r}", file=sys.stderr)


def _acquire_or_adopt_pass_lock(advisory, pass_id: str, repo_root: Path, adopted: str | None):
    """Adopt a lock acquired during steal resolution, or acquire it once here."""
    if adopted is not None:
        return adopted
    return advisory.acquire_pass_lock(pass_id, repo_root)
