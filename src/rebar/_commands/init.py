"""In-process ``init`` — bootstrap the event-sourced ticket store.

Port of ticket-init.sh. Creates (or mounts) the orphan ``tickets`` branch as a
linked worktree at ``.tickets-tracker/``, commits ``.gitignore`` +
``.pre-commit-config.yaml`` + ``.gitattributes`` (``merge=ours`` for the per-pass
mutable root files) on it, generates ``.env-id`` + ``.signing-key``,
normalizes gc config (``--unset gc.auto`` + ``gc.autoDetach=true`` — rebar trusts
stock ``git gc`` now that recovery is non-destructive, see the ``gc-config`` ensure
unit ``_gc_config_unit`` run via ``rebar._store.ensures.run_ensures``),
and excludes the tracker from the host repo. Idempotent: re-running on an
initialized repo recovers any stale rebase/merge on the tickets branch, re-applies
the gc-config migration, and returns 0. A 30s mkdir lock
(``.git/ticket-init.lock``) serializes concurrent inits.

init resolves the repo from the git toplevel of ``repo_root`` (or cwd) — it
deliberately ignores an inherited repo-root override (it must initialize the
target repo, not a shim's project root), matching the bash script's repo-root unset.

Byte-parity pinned by ``tests/interfaces/test_e4_init.py``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid

from rebar._store.ensures import APPLIED_MARKER, EnsureOutcome, run_ensures
from rebar._store.gitutil import run_git
from rebar._store.lock import MKDIR_LOCK_NAME, WRITE_LOCK_NAME
from rebar.graph._cache import _GRAPH_CACHE_FILE

# Runtime artifacts created in the tracker worktree that must never be committed.
# The lock/cache names are sourced from their defining constants so this ignore
# list cannot drift from them (bug stem-ewe-tomb). The flock write-lock file is
# intentionally NOT unlinked on release (deleting it races other lockers), so it
# persists after every write; the graph cache is rewritten on every graph compile.
_GITIGNORE = f""".env-id
.closure-key
.signing-key
.state-cache
.scratch/
.cache.json
*/.cache.json
{WRITE_LOCK_NAME}
{MKDIR_LOCK_NAME}/
{_GRAPH_CACHE_FILE}
{APPLIED_MARKER}
"""

_GITATTRIBUTES = """# Shared mutable root files are per-pass derived CACHES the reconciler rebuilds,
# not ticket events (uuid-named ticket dirs never collide, so they never need a
# merge policy). On a union reconverge (sync.py) keep OUR copy and let the next
# reconciler pass rebuild the loser. merge=union is WRONG here — it line-unions
# JSON into invalid JSON. The 'ours' driver is defined in git config by init
# (merge.ours.driver=true); without it these patterns are silently ignored.
.bridge_state/* merge=ours
"""

# The retired ``.reconciler-*`` merge=ours line (epic dust-troth-naval / C4): the
# reconciler pass-lock/phase-gate moved off the tickets tree onto refs/reconciler/*,
# so it no longer needs a union-merge carve-out. Kept as a constant so the
# already-initialized-tracker migration can strip it from committed .gitattributes.
_RETIRED_GITATTRIBUTES_LINES = (".reconciler-* merge=ours",)

_PRECOMMIT = """# No-op pre-commit config for the tickets orphan branch.
# The tickets branch carries event-sourced ticket data only — no source
# code to lint — so no hooks are needed. This empty config exists solely
# so the pre-commit framework (when installed as a pre-push hook in the
# host repo) accepts pushes from the .tickets-tracker linked worktree
# without requiring PRE_COMMIT_ALLOW_NO_CONFIG=1 on every caller.
repos: []
"""


def _git(cwd: str, *args: str) -> subprocess.CompletedProcess:
    return run_git(cwd, *args, check=False)


def _git_ok(cwd: str, *args: str) -> bool:
    return _git(cwd, *args).returncode == 0


def _realpath(p: str) -> str:
    return os.path.realpath(p)


def _gc_config_unit(tracker: str) -> EnsureOutcome:
    """Trust stock ``git gc`` on the tickets worktree (epic 97e7 / P1.4).

    rebar no longer forces ``gc.auto=0``: union recovery (sync.py) keeps every
    ticket commit ref-reachable, so stock background gc is safe by construction and
    only ever collects truly unreachable objects. Two idempotent steps, so an
    existing tracker self-heals on any ensure sweep:

    - ``--unset gc.auto`` sheds the stale ``gc.auto=0`` an older rebar wrote.
    - ``gc.autoDetach=true`` ensures a triggered background gc forks and never
      serializes a foreground ticket write.

    Check-then-act: acts only when either value is off the desired state, so a
    converged tracker reports ``ok`` and mutates nothing (ensure-registry unit)."""
    changed = False
    if _git(tracker, "config", "--get", "gc.auto").returncode == 0:
        _git(tracker, "config", "--unset", "gc.auto")
        changed = True
    if _git(tracker, "config", "--get", "gc.autoDetach").stdout.strip() != "true":
        _git(tracker, "config", "gc.autoDetach", "true")
        changed = True
    return EnsureOutcome(
        "gc-config",
        "changed" if changed else "ok",
        "gc.auto unset + gc.autoDetach=true",
    )


def _ensure_env_id_unit(tracker: str) -> EnsureOutcome:
    """Ensure the store carries a stable ``.env-id`` (ensure-registry unit).

    Check-then-act: writes a fresh uuid only when absent, so it no-ops on a store
    that already has one (e.g. after fresh-init's ``_gen_local_files``)."""
    real = _realpath(tracker)
    if not os.path.isdir(real):
        return EnsureOutcome("env-id", "ok", "tracker dir absent")
    if os.path.isfile(os.path.join(real, ".env-id")):
        return EnsureOutcome("env-id", "ok", ".env-id present")
    with open(os.path.join(real, ".env-id"), "w", encoding="utf-8") as f:
        f.write(str(uuid.uuid4()) + "\n")
    return EnsureOutcome("env-id", "changed", "generated .env-id")


def _detect_stale(git_dir: str) -> str:
    if os.path.isdir(os.path.join(git_dir, "rebase-merge")):
        return "rebase-merge"
    if os.path.isdir(os.path.join(git_dir, "rebase-apply")):
        return "rebase-apply"
    if os.path.isfile(os.path.join(git_dir, "REBASE_HEAD")):
        return "REBASE_HEAD"
    if os.path.isfile(os.path.join(git_dir, "MERGE_HEAD")):
        return "MERGE_HEAD"
    return ""


def _emit(msg: str, silent: bool) -> None:
    if not silent:
        sys.stderr.write(msg + "\n")


def _run_ensures_logged(tracker: str, silent: bool) -> None:
    """Run the ensure registry at an init entry point and surface any ``failed``
    unit as a warning (init never aborts on an ensure failure — :func:`run_ensures`
    already skip-and-continues and never raises)."""
    for outcome in run_ensures(tracker):
        if outcome.status == "failed":
            _emit(f"WARNING: ensure '{outcome.id}' failed: {outcome.detail}", silent)


def _resolve_repo_root(repo_root) -> str | None:
    """Resolve the repo to initialize, matching ``config.repo_root`` precedence
    (explicit > REBAR_ROOT > git toplevel of cwd) so init writes the
    tracker exactly where every command (config.tracker_dir) and the auto-init gate
    look for it. Returns None only when no root resolves (→ "not a git repo")."""
    if repo_root:
        return os.path.realpath(str(repo_root))
    env = os.environ.get("REBAR_ROOT")
    if env:
        return os.path.realpath(env)
    cp = subprocess.run(
        ["git", "-C", ".", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_DISCOVERY_ACROSS_FILESYSTEM": "1"},
    )
    return cp.stdout.strip() if cp.returncode == 0 and cp.stdout.strip() else None


def _tracker_exclude_entry(repo: str, tracker: str) -> str | None:
    """The ``.git/info/exclude`` entry for the tracker: its path RELATIVE to the repo
    working tree (so git ignores the worktree/symlink), honoring a custom
    ``tracker.dir``. Returns ``None`` when the tracker lives OUTSIDE the repo (an
    absolute relocation) — there is nothing in the repo tree to exclude."""
    rel = os.path.relpath(tracker, repo)
    outside = rel == os.curdir or rel == os.pardir or rel.startswith(os.pardir + os.sep)
    return None if outside or os.path.isabs(rel) else rel


def _exclude(git_dir: str, *entries: str) -> None:
    exclude_file = os.path.join(git_dir, "info", "exclude")
    os.makedirs(os.path.dirname(exclude_file), exist_ok=True)
    existing = ""
    if os.path.isfile(exclude_file):
        with open(exclude_file, encoding="utf-8") as f:
            existing = f.read()
    lines = existing.splitlines()
    with open(exclude_file, "a", encoding="utf-8") as f:
        for e in entries:
            if e not in lines:
                f.write(e + "\n")
                lines.append(e)


def init_core(repo_root=None, *, silent: bool = False) -> int:
    """Bootstrap (or verify) the tracker. Returns 0 on success / already-init,
    1 on a fatal error (message already emitted to stderr)."""
    repo = _resolve_repo_root(repo_root)
    if repo is None:
        sys.stderr.write("Error: not inside a git repository\n")
        return 1
    from rebar.config import tracker_dir

    tracker = str(tracker_dir(repo))

    # ── Idempotency: valid worktree already mounted ──────────────────────────
    if os.path.isdir(tracker) and os.path.isfile(os.path.join(tracker, ".git")):
        if _git_ok(tracker, "rev-parse", "--is-inside-work-tree"):
            git_dir = _git(tracker, "rev-parse", "--git-dir").stdout.strip()
            kind = _detect_stale(git_dir) if git_dir else ""
            if kind:
                _emit(
                    f"WARNING: Stale {kind} state on tickets branch; attempting recovery",
                    silent,
                )
                if kind in ("rebase-merge", "rebase-apply", "REBASE_HEAD"):
                    try:
                        cp = _git(tracker, "-c", "rebase.autostash=true", "rebase", "--continue")
                        rc = cp.returncode
                    except Exception:  # noqa: BLE001 — rebase --continue failure surfaced as a WARNING + abort below
                        rc = 1
                    if rc != 0:
                        _emit(
                            "WARNING: rebase --continue failed; aborting rebase. Run "
                            "'rebar fsck-recover' to cherry-pick stranded commits.",
                            silent,
                        )
                        _git(tracker, "rebase", "--abort")
                elif kind == "MERGE_HEAD":
                    _emit("WARNING: Aborting stale merge on tickets branch", silent)
                    _git(tracker, "merge", "--abort")
            # Converge the store via the ensure registry (idempotent, drift-
            # correcting) so a config fix shipped after this store was initialized
            # reaches it on re-init — the migration these hand-listed calls once
            # performed, generalized (epic odd-vortex-elbow).
            _run_ensures_logged(tracker, silent)
            _emit("Ticket system already initialized.", silent)
            return 0

    # ── Host repo is itself a linked worktree (.git is a file) → symlink ──────
    if os.path.isfile(os.path.join(repo, ".git")):
        return _init_via_symlink(repo, tracker, silent)

    # ── Clean up a partial/stale tracker dir ─────────────────────────────────
    if os.path.isdir(tracker) and not _git_ok(tracker, "rev-parse", "--is-inside-work-tree"):
        _git(repo, "worktree", "prune")
        _rmtree(tracker)

    # ── Exclude tracker + scratch from the host repo ─────────────────────────
    host_git = _resolve_git_dir(repo)
    if host_git:
        entry = _tracker_exclude_entry(repo, tracker)
        _exclude(host_git, *([entry] if entry else []), ".scratch/")

    # ── Init lock (mkdir, 30s) ───────────────────────────────────────────────
    lock_dir = _init_lock_dir(repo)
    if not _acquire_init_lock(lock_dir):
        sys.stderr.write("Error: could not acquire ticket-init lock within 30s\n")
        return 1
    try:
        rc = _mount_or_create_branch(repo, tracker)
        if rc != 0:
            return rc
        # Fresh-init-only bootstrap (NOT ensure-registry units): these run once at
        # genesis and are not idempotent drift-correctors.
        _ensure_branch_user_config(repo, tracker)
        _exclude_scratch_in_tracker(tracker)
        _commit_precommit(tracker)
        _gen_local_files(tracker)  # writes .env-id (env-id unit then no-ops below)
        # Converge via the ensure registry (gitignore, gitattributes, gc-config,
        # merge-ours, env-id), AFTER the bootstrap so ordering is preserved. This
        # replaces the hand-listed _commit_gitignore/_commit_gitattributes/
        # _migrate_gc_config/_ensure_merge_ours_driver calls (epic odd-vortex-elbow).
        _run_ensures_logged(tracker, silent)
        _emit("Ticket system initialized.", silent)
        return 0
    finally:
        try:
            os.rmdir(lock_dir)
        except OSError:
            pass


def _rmtree(path: str) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


def _resolve_git_dir(repo: str) -> str:
    git_path = os.path.join(repo, ".git")
    if os.path.isfile(git_path):
        with open(git_path, encoding="utf-8") as f:
            line = f.read().strip()
        return line[len("gitdir: ") :] if line.startswith("gitdir: ") else ""
    return git_path


def _init_lock_dir(repo: str) -> str:
    base = os.path.join(repo, ".git")
    if os.path.isfile(base):
        with open(base, encoding="utf-8") as f:
            line = f.read().strip()
        gd = line[len("gitdir: ") :] if line.startswith("gitdir: ") else base
        common = _git(
            gd if os.path.isdir(gd) else repo, "rev-parse", "--git-common-dir"
        ).stdout.strip()
        if common:
            base = (
                os.path.realpath(os.path.join(gd, common)) if not os.path.isabs(common) else common
            )
    return os.path.join(base, "ticket-init.lock")


def _acquire_init_lock(lock_dir: str) -> bool:
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            os.mkdir(lock_dir)
            return True
        except FileExistsError:
            time.sleep(1)
        except OSError:
            time.sleep(1)
    return False


def _mount_or_create_branch(repo: str, tracker: str) -> int:
    from rebar.config import tickets_branch, tickets_remote

    branch = tickets_branch(repo)  # configured tracker.branch (default "tickets")
    remote_name = tickets_remote(repo)  # configured sync.remote (default "origin")
    local = _git_ok(repo, "rev-parse", "--verify", branch)
    remote = _git_ok(repo, "rev-parse", "--verify", f"{remote_name}/{branch}")
    if local:
        cp = _git(repo, "worktree", "add", tracker, branch)
        if cp.returncode != 0:
            sys.stderr.write(f"ERROR: git worktree add (local branch) failed: {cp.stderr}\n")
            return 1
        return 0
    if remote:
        _git(repo, "fetch", remote_name, branch)
        cp = _git(repo, "worktree", "add", tracker, branch)
        if cp.returncode != 0:
            sys.stderr.write(f"ERROR: git worktree add (remote branch) failed: {cp.stderr}\n")
            return 1
        return 0
    # Orphan branch.
    cp = _git(repo, "worktree", "add", "--orphan", "-b", branch, tracker)
    if cp.returncode != 0:
        # Fallback for git < 2.40.
        cp2 = _git(repo, "worktree", "add", "--detach", tracker)
        if cp2.returncode != 0:
            sys.stderr.write(f"ERROR: git worktree add --orphan failed: {cp.stderr}\n")
            return 1
        _git(tracker, "checkout", "--orphan", branch)
        _git(tracker, "rm", "-rf", ".", "--quiet")
    _ensure_branch_user_config(repo, tracker)
    _git(tracker, "config", "commit.gpgsign", "false")
    _git(tracker, "config", "tag.gpgsign", "false")
    _git(
        tracker,
        "commit",
        "--allow-empty",
        "-q",
        "--no-verify",
        "-m",
        "chore: initialize ticket tracker",
    )
    return 0


def _ensure_branch_user_config(repo: str, tracker: str) -> None:
    if _git(tracker, "config", "user.email").returncode != 0:
        email = _git(repo, "config", "user.email").stdout.strip() or "ticket-system@localhost"
        name = _git(repo, "config", "user.name").stdout.strip() or "Ticket System"
        _git(tracker, "config", "user.email", email)
        _git(tracker, "config", "user.name", name)


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


def _exclude_scratch_in_tracker(tracker: str) -> None:
    git_file = os.path.join(tracker, ".git")
    git_dir = ""
    if os.path.isfile(git_file):
        with open(git_file, encoding="utf-8") as f:
            line = f.read().strip()
        gd = line[len("gitdir: ") :] if line.startswith("gitdir: ") else ""
        if gd and not os.path.isabs(gd):
            gd = os.path.join(tracker, gd)
        git_dir = gd
    if not git_dir:
        return
    _exclude(git_dir, ".scratch/")


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


def _gitattributes_unit(tracker: str) -> EnsureOutcome:
    """Commit the tickets-branch ``.gitattributes`` (create-if-absent, idempotent),
    so a union merge keeps OUR copy of the per-pass mutable root files instead of
    wedging. Pairs with :func:`_merge_ours_unit` (the driver it names).

    Also runs a one-time migration (epic dust-troth-naval / C4): an already-committed
    ``.gitattributes`` predating the ref-lock still carries ``.reconciler-* merge=ours``;
    strip that retired line (the lock moved off the tickets tree onto refs/reconciler/*).

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

    # Migration arm: strip any retired line from an existing committed .gitattributes.
    lines = show.stdout.splitlines()
    kept = [ln for ln in lines if ln.strip() not in _RETIRED_GITATTRIBUTES_LINES]
    if len(kept) == len(lines):
        return EnsureOutcome("gitattributes", "ok", ".gitattributes converged")
    path = os.path.join(tracker, ".gitattributes")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(kept) + ("\n" if kept else ""))
    _git(tracker, "add", ".gitattributes")
    _git(
        tracker,
        "commit",
        "-q",
        "--no-verify",
        "-m",
        "chore(reconciler): drop retired .reconciler-* merge=ours (moved to refs/reconciler/*)",
    )
    return EnsureOutcome("gitattributes", "changed", "stripped retired merge=ours line")


def _commit_precommit(tracker: str) -> None:
    if _git(tracker, "show", "tickets:.pre-commit-config.yaml").returncode != 0:
        with open(os.path.join(tracker, ".pre-commit-config.yaml"), "w", encoding="utf-8") as f:
            f.write(_PRECOMMIT)
        _git(tracker, "add", ".pre-commit-config.yaml")
        _git(
            tracker,
            "commit",
            "-q",
            "--no-verify",
            "-m",
            "chore: add no-op .pre-commit-config.yaml (bug 27d8-b230)",
        )


def _gen_local_files(tracker: str) -> None:
    # .env-id: per-environment identity. .signing-key: the manifest-signature gate
    # key (chmod 600). The legacy .closure-key (verdict-hash gate) is NO LONGER
    # minted — the signature system supersedes it — but stays gitignored for
    # back-compat with stores that still carry one.
    env_path = os.path.join(tracker, ".env-id")
    if not os.path.isfile(env_path):
        with open(env_path, "w", encoding="utf-8") as f:
            f.write(str(uuid.uuid4()) + "\n")
    key_path = os.path.join(tracker, ".signing-key")
    if not os.path.isfile(key_path):
        with open(key_path, "w", encoding="utf-8") as f:
            f.write(str(uuid.uuid4()) + "\n")
        try:
            os.chmod(key_path, 0o600)
        except OSError:
            pass


def _main_worktree_tracker(repo: str) -> str | None:
    """Path to the MAIN worktree's tracker dir (the configured ``tracker.dir``,
    default ``.tickets-tracker``) — the real store a linked worktree symlinks to —
    or None when the main worktree can't be resolved. Does NOT check whether that
    path exists / is initialized; callers decide."""
    from rebar.config import tracker_dir

    wl = _git(repo, "worktree", "list", "--porcelain").stdout
    for line in wl.splitlines():
        if line.startswith("worktree "):
            return str(tracker_dir(line[len("worktree ") :]))
    return None


def pending_init_is_symlink(repo_root=None) -> bool:
    """True when initializing THIS repo would be a pure symlink to an
    already-initialized store — i.e. the host repo is a linked git worktree
    (``.git`` is a *file*) and the MAIN worktree already has a ``.tickets-tracker``.

    This is the predicate that tells the two init concepts apart. A *first-time*
    init materializes an orphan ``tickets`` branch + a linked worktree and edits
    ``.git/info/exclude`` — it mutates the host repo, so it needs consent. Creating
    this symlink, by contrast, only adds a local link to an EXISTING store and
    leaves the underlying repo's state untouched, so the auto-init gate may create
    it automatically, without a prompt."""
    repo = _resolve_repo_root(repo_root)
    if repo is None:
        return False
    if not os.path.isfile(os.path.join(repo, ".git")):
        return False
    main_tracker = _main_worktree_tracker(repo)
    return main_tracker is not None and os.path.isdir(main_tracker)


def pending_init_attaches_to_existing(repo_root=None) -> bool:
    """True when a ``tickets`` branch already exists locally or on ``origin``, so
    initializing THIS repo only MOUNTS that existing shared state (a linked
    worktree via ``_mount_or_create_branch``'s local/remote arms) rather than
    fabricating a brand-new orphan store.

    Like the worktree-symlink case, this is safe to do automatically — including
    non-interactively — because it does not create new ticket history; it attaches
    to a store that already exists. Distinguishes "attach to an existing
    origin/tickets" from a true first-time init, so the auto-init gate need not
    refuse it for lack of a TTY (bug wet-chair-peg)."""
    repo = _resolve_repo_root(repo_root)
    if repo is None:
        return False
    from rebar.config import tickets_branch, tickets_remote

    branch = tickets_branch(repo)
    remote_name = tickets_remote(repo)
    return _git_ok(repo, "rev-parse", "--verify", branch) or _git_ok(
        repo, "rev-parse", "--verify", f"{remote_name}/{branch}"
    )


def _init_via_symlink(repo: str, tracker: str, silent: bool) -> int:
    main_tracker = _main_worktree_tracker(repo)
    if main_tracker is None:
        sys.stderr.write("Error: could not detect main worktree path via git worktree list\n")
        return 1
    if not os.path.isdir(main_tracker):
        sys.stderr.write(
            "Error: Run ticket init from the main repo first, then re-run from the worktree.\n"
        )
        return 1
    if os.path.islink(tracker):
        if _realpath(tracker) == _realpath(main_tracker):
            # Already symlinked to the main store — converge it (idempotent) so a
            # worktree attach still reaches any pending ensure (epic odd-vortex-elbow).
            _run_ensures_logged(tracker, silent)
            _emit("Ticket system already initialized.", silent)
            return 0
        os.remove(tracker)
    if os.path.isdir(tracker) and not os.path.islink(tracker):
        if os.path.isfile(os.path.join(tracker, ".git")):
            sys.stderr.write(
                f"Error: {os.path.basename(tracker)}/ is a real git worktree in this worktree "
                "checkout. Remove it manually first.\n"
            )
            return 1
        _rmtree(tracker)
    os.symlink(main_tracker, tracker)
    wt_git = _resolve_git_dir(repo)
    if wt_git:
        entry = _tracker_exclude_entry(repo, tracker)
        if entry:
            _exclude(wt_git, entry)
    # Newly symlinked to the main store — converge it via the ensure registry
    # (idempotent; the env-id unit no-ops when the main store already has one).
    _run_ensures_logged(tracker, silent)
    _emit("Ticket system initialized (symlink to main repo).", silent)
    return 0


def init_cli(argv: list[str], *, repo_root=None) -> int:
    silent = "--silent" in argv
    return init_core(repo_root, silent=silent)
