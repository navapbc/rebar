"""Create or mount the event-sourced ticket store.

``init`` attaches the tickets branch at configured ``tracker.dir`` (``.tickets-tracker/`` by
default). Main-worktree mounts and creations ensure local identity and signing material; new
stores also commit bootstrap files. Existing stores converge through the ensure registry.
Unknown remote reachability fails closed, and a 30-second directory lock serializes the
main-worktree mount/create/bootstrap path; already-mounted and linked-worktree paths return
earlier.

Repository resolution prefers an explicit ``repo_root``, then ``REBAR_ROOT``, then the git
toplevel of the current directory. Explicit and environment paths are realpath-normalized but
not replaced by their git toplevel. The output contract is covered by ``test_e4_init.py``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid

from rebar._commands import _init_probe

# Re-export convergence units from ``_init_ensures``. The registry dispatches through these
# module attributes, and tests plus maintained documentation reference this access path.
from rebar._commands._init_ensures import (  # noqa: F401  (re-export)
    _GITATTRIBUTES,
    _GITIGNORE,
    _gc_config_unit,
    _gitattributes_unit,
    _gitignore_unit,
    _merge_ours_unit,
    _store_compat_unit,
)
from rebar._snapshot.git_fetch import fetch_timeout as _fetch_timeout
from rebar._store.ensures import EnsureOutcome, run_ensures
from rebar._store.env_identity import mint_env_id_guarded
from rebar._store.gitutil import run_git, run_git_write
from rebar.reducer.marker import ARCHIVE_MARKER_NAME, MARKER_LOCK_NAME

_PRECOMMIT = """# No-op pre-commit config for the tickets orphan branch.
# The tickets branch carries event-sourced ticket data only — no source
# code to lint — so no hooks are needed. This empty config exists solely
# so the pre-commit framework (when installed as a pre-push hook in the
# host repo) accepts pushes from the .tickets-tracker linked worktree
# without requiring PRE_COMMIT_ALLOW_NO_CONFIG=1 on every caller.
repos: []
"""


# raw-git-ok: store-maintenance command, seam-internal
def _git(cwd: str, *args: str) -> subprocess.CompletedProcess:
    return run_git_write(cwd, *args, check=False)


# A cold branch fetch can transfer a large event history through a proxy. Use the tunable
# materialization timeout rather than the 30-second incremental bound. Throughput detection
# handles stalls, and timeout returns ``CompletedProcess(124)`` to the caller's failure path.
def _git_fetch(cwd: str, *args: str) -> subprocess.CompletedProcess:
    return _init_probe.run_bounded_git(
        cwd,
        *args,
        timeout=_fetch_timeout(),
        run_git_fn=run_git,
    )


# raw-git-ok: store-maintenance command, seam-internal
def _git_ok(cwd: str, *args: str) -> bool:
    return _git(cwd, *args).returncode == 0


def _realpath(p: str) -> str:
    return os.path.realpath(p)


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
    from rebar import config

    return config.repo_root_or_none(repo_root)


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


def init_core(repo_root=None, *, silent: bool = False, force_new_store: bool = False) -> int:
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
            # Run idempotent ensure units so re-init corrects store configuration drift.
            _run_ensures_logged(tracker, silent)
            _emit("Ticket system already initialized.", silent)
            return 0

    # ── Host repo is itself a linked worktree (.git is a file) → symlink ──────
    if os.path.isfile(os.path.join(repo, ".git")):
        return _init_via_symlink(repo, tracker, silent, force_new_store=force_new_store)

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
        rc = _mount_or_create_branch(repo, tracker, force_new_store=force_new_store)
        if rc != 0:
            return rc
        # Fresh-init-only bootstrap (NOT ensure-registry units): these run once at
        # genesis and are not idempotent drift-correctors.
        _ensure_branch_user_config(repo, tracker)
        _exclude_scratch_in_tracker(tracker)
        _commit_precommit(tracker)
        _gen_local_files(tracker)  # writes .env-id (env-id unit then no-ops below)
        # Run gitignore, attributes, GC, merge-driver, and environment ensures after bootstrap.
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


def _warn_force_no_effect(reason: str) -> None:
    sys.stderr.write(
        f"WARNING: --force-new-store has no effect because {reason}; continuing normally.\n"
    )


# raw-git-ok: store-maintenance command, seam-internal
def _mount_or_create_branch(repo: str, tracker: str, *, force_new_store: bool = False) -> int:
    from rebar.config import tickets_branch, tickets_remote

    # Pin the auto-maintenance posture BEFORE the store's first commit, not after it.
    # ``_gc_config_unit`` is also run by the post-bootstrap ensure sweep, but that sweep
    # lands AFTER this function and ``_commit_precommit`` have already committed. Inside
    # that window git is at its DEFAULTS, so each ``git commit`` spawns
    # ``git maintenance run --auto --quiet --detach`` -- a background child that OUTLIVES
    # the command and repacks the object database this tracker worktree SHARES with the
    # host repo, outside rebar's write lock. That is exactly the hazard ADR 0051 / bug 88eb
    # forced foreground, and the concurrent mutator behind the fixture-remote flakes
    # documented in ``tests/_git_upkeep.py`` (bugs dca1, b394, 5b74, 57d2). Measured with
    # GIT_TRACE2_EVENT on git 2.55, a fresh ``rebar init`` spawned two ``--detach`` children,
    # both attributed to a ``commit`` in this window; with this pin it spawns none.
    #
    # The unit is addressed at *repo* because the tracker does not exist yet -- a linked
    # worktree shares the host repo's ``.git/config``, so this writes the same three
    # settings to the same file the later sweep targets, which then converges as a no-op.
    # ``_gc_config_unit`` stays their single definition; nothing is restated here.
    _gc_config_unit(repo)

    branch = tickets_branch(repo)  # configured tracker.branch (default "tickets")
    remote_name = tickets_remote(repo)  # configured sync.remote (default "origin")
    _init_probe.require_s3_helper_if_s3_remote(repo, remote_name, run_git_fn=run_git)
    local = _git_ok(repo, "rev-parse", "--verify", branch)
    remote = _git_ok(repo, "rev-parse", "--verify", f"{remote_name}/{branch}")
    if local:
        if force_new_store:
            _warn_force_no_effect("the local ticket branch exists")
        cp = _git(repo, "worktree", "add", tracker, branch)
        if cp.returncode != 0:
            sys.stderr.write(f"ERROR: git worktree add (local branch) failed: {cp.stderr}\n")
            return 1
        return 0
    if remote:
        if force_new_store:
            _warn_force_no_effect("the remote-tracking ticket branch exists")
        fetch = _git_fetch(repo, "fetch", remote_name, branch)
        if fetch.returncode != 0:
            # Non-fatal: the remote-tracking ref already exists (that is why this arm
            # ran), so the worktree still mounts off it and sync self-heals later. But
            # surface it actionably rather than swallowing it — a timeout must never be
            # a silent hang (bug 983f / AC2).
            sys.stderr.write(
                f"WARNING: could not refresh {remote_name}/{branch} before mount "
                f"({fetch.stderr.strip() or 'fetch failed'}); mounting the existing "
                "tracking ref — the store will reconverge on the next sync\n"
            )
        cp = _git(repo, "worktree", "add", tracker, branch)
        if cp.returncode != 0:
            sys.stderr.write(f"ERROR: git worktree add (remote branch) failed: {cp.stderr}\n")
            return 1
        return 0
    if _init_probe.remote_exists(repo, remote_name, run_git_fn=run_git):
        state = _init_probe.probe_remote_branch(repo, remote_name, branch, run_git_fn=run_git)
        if state == _init_probe.ADVERTISED:
            if force_new_store:
                _warn_force_no_effect("the remote ticket branch is advertised")
            fetch = _git_fetch(repo, "fetch", remote_name, branch)
            if fetch.returncode != 0:
                sys.stderr.write(
                    f"ERROR: could not fetch advertised ticket store {remote_name}/{branch} "
                    f"({fetch.stderr.strip() or 'fetch failed'}); retry after connectivity "
                    "returns. Refusing to create an orphan store.\n"
                )
                return 1
            cp = _git(repo, "worktree", "add", "-b", branch, tracker, "FETCH_HEAD")
            if cp.returncode != 0:
                sys.stderr.write(
                    f"ERROR: git worktree add (advertised branch) failed: {cp.stderr}\n"
                )
                return 1
            return 0
        if state == _init_probe.UNREACHABLE:
            if not force_new_store:
                sys.stderr.write(
                    f"ERROR: could not determine whether {remote_name}/{branch} exists within "
                    f"{_init_probe.REMOTE_PROBE_TIMEOUT}s; retry after connectivity returns "
                    "or use 'rebar init --force-new-store' to explicitly create a new store.\n"
                )
                return 1
            sys.stderr.write(
                f"WARNING: {remote_name}/{branch} could not be reached within "
                f"{_init_probe.REMOTE_PROBE_TIMEOUT}s; --force-new-store is creating a new store.\n"
            )
        elif force_new_store:
            _warn_force_no_effect("the reachable remote has no ticket branch")
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


# Per-call path budget for the batched ``git rm --cached`` below (keeps each argv
# far below ARG_MAX on a store with hundreds of tracked markers).
_UNTRACK_BATCH = 200


# raw-git-ok: store-maintenance command, seam-internal
def _untrack_runtime_markers_unit(tracker: str) -> EnsureOutcome:
    """Untrack archived and write-lock runtime markers through an ensure unit.

    One ``git ls-files`` call detects tracked markers. A batched ``git rm --cached`` preserves
    local copies and produces one commit. Enumerating matches avoids unmatched pathspec errors.
    ARCHIVED events remain authoritative, and readers recreate optional cache markers.
    """
    uid = "untrack-runtime-markers"
    ls = _git(tracker, "ls-files", "--", f"*/{ARCHIVE_MARKER_NAME}", f"*/{MARKER_LOCK_NAME}")
    tracked = [ln for ln in ls.stdout.splitlines() if ln]
    if not tracked:
        return EnsureOutcome(uid, "ok", "no tracked runtime markers")
    for i in range(0, len(tracked), _UNTRACK_BATCH):
        _git(tracker, "rm", "--cached", "--quiet", "--", *tracked[i : i + _UNTRACK_BATCH])
    _git(
        tracker,
        "commit",
        "-q",
        "--no-verify",
        "-m",
        "chore: untrack per-ticket runtime markers (.archived, .write.lock)",
    )
    return EnsureOutcome(uid, "changed", f"untracked {len(tracked)} runtime marker file(s)")


# raw-git-ok: store-maintenance command, seam-internal
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
    # Mint .env-id only when absent; the guard warns if existing events identify other
    # environments. Create the mode-600 signing key when absent. The retired closure key is
    # not minted here and remains covered by the store's ignore configuration.
    mint_env_id_guarded(tracker)
    key_path = os.path.join(tracker, ".signing-key")
    if not os.path.isfile(key_path):
        with open(key_path, "w", encoding="utf-8") as f:
            f.write(str(uuid.uuid4()) + "\n")
        try:
            os.chmod(key_path, 0o600)
        except OSError:
            pass


def _main_worktree_tracker(repo: str) -> str | None:
    """Return the main worktree's configured tracker path, or ``None``.

    The caller decides whether the resolved path exists and is initialized.
    """
    from rebar.config import tracker_dir

    wl = _git(repo, "worktree", "list", "--porcelain").stdout
    for line in wl.splitlines():
        if line.startswith("worktree "):
            return str(tracker_dir(line[len("worktree ") :]))
    return None


def pending_init_is_symlink(repo_root=None) -> bool:
    """Return whether init would try to link this worktree to a main tracker directory.

    The predicate requires a linked worktree and checks only that the main tracker path is a
    directory; it does not validate the tracker's contents. This is one consent-free reuse
    signal; :func:`pending_init_attaches_to_existing` covers branch attachment. Reusing shared
    state needs no consent even when mounting it creates a worktree, symlink, or exclude entry.
    Only creating a new orphan store requires consent.
    """
    repo = _resolve_repo_root(repo_root)
    if repo is None:
        return False
    if not os.path.isfile(os.path.join(repo, ".git")):
        return False
    main_tracker = _main_worktree_tracker(repo)
    return main_tracker is not None and os.path.isdir(main_tracker)


def pending_init_attaches_to_existing(repo_root=None) -> bool:
    """Return whether init can mount an existing local or remote tickets branch.

    Attaching existing shared state creates no ticket history and may proceed without interactive
    consent. Creating an orphan store remains a first-time mutation that requires consent.
    """
    repo = _resolve_repo_root(repo_root)
    if repo is None:
        return False
    from rebar.config import tickets_branch, tickets_remote

    branch = tickets_branch(repo)
    remote_name = tickets_remote(repo)
    if _git_ok(repo, "rev-parse", "--verify", branch) or _git_ok(
        repo, "rev-parse", "--verify", f"{remote_name}/{branch}"
    ):
        return True
    return _init_probe.remote_exists(repo, remote_name, run_git_fn=run_git) and (
        _init_probe.probe_remote_branch(repo, remote_name, branch, run_git_fn=run_git)
        == _init_probe.ADVERTISED
    )


def pending_init_remote_unreachable(repo_root=None) -> bool:
    """Whether missing-tracker auto-init must fail closed before prompting.

    A locally absent remote is a known greenfield case.  A configured remote whose
    branch cannot be classified is not: prompting there could otherwise create a
    divergent orphan store merely because the network or credentials are unavailable.
    """
    repo = _resolve_repo_root(repo_root)
    if repo is None:
        return False
    from rebar.config import tickets_branch, tickets_remote

    branch = tickets_branch(repo)
    remote_name = tickets_remote(repo)
    return _init_probe.remote_branch_unreachable(
        repo,
        remote_name,
        branch,
        has_ref=lambda ref: _git_ok(repo, "rev-parse", "--verify", ref),
        run_git_fn=run_git,
    )


def _init_via_symlink(
    repo: str, tracker: str, silent: bool, *, force_new_store: bool = False
) -> int:
    if force_new_store:
        # FAIL CLOSED. A linked worktree's tracker is a SYMLINK to the main repo's LIVE
        # store, and rebar's worktree model deliberately SHARES one tracker across every
        # worktree. So this branch cannot honour "create a new store" without silently
        # DETACHING this worktree from the shared store — a split-brain strictly worse
        # than the bug being fixed. Refusing is the only answer that is neither.
        #
        # What must never happen again is the SILENT degrade: this branch used to return
        # before force_new_store was consulted at all, handing a caller that explicitly
        # demanded a new store the REAL one without a word. A replay harness stepped on
        # that and wrote 425 duplicate tickets into the live tracker
        # (rebar-ticket 00c1-dc03-1f1f-4ffc).
        sys.stderr.write(
            "Error: --force-new-store cannot create a new store in a linked git "
            "worktree — its tracker is a symlink to the main repo's existing store, "
            "which every worktree deliberately shares. Run init in the main worktree "
            "to mount that store, or use a standalone clone if you need an isolated "
            "one.\n"
        )
        return 1
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
    from rebar._cli._parsers.core.bootstrap import build_init

    # Parser of record for init's accepted grammar; the membership scan below is
    # retained because it owns the bespoke ``unknown init option`` reject/exit code.
    build_init(prog="rebar init").parse_known_args(argv)
    allowed = {"--silent", "--force-new-store"}
    unknown = [arg for arg in argv if arg not in allowed]
    if unknown:
        sys.stderr.write(f"Error: unknown init option: {unknown[0]}\n")
        return 1
    silent = "--silent" in argv
    return init_core(repo_root, silent=silent, force_new_store="--force-new-store" in argv)
