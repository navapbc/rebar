"""In-process ``init`` — bootstrap the event-sourced ticket store (Tier E E4).

Port of ticket-init.sh. Creates (or mounts) the orphan ``tickets`` branch as a
linked worktree at ``.tickets-tracker/``, commits ``.gitignore`` +
``.pre-commit-config.yaml`` on it, generates ``.env-id`` + ``.signing-key``, sets
``gc.auto=0``, and excludes the tracker from the host repo. Idempotent: re-running
on an initialized repo recovers any stale rebase/merge on the tickets branch and
returns 0. A 30s mkdir lock (``.git/ticket-init.lock``) serializes concurrent
inits.

init resolves the repo from the git toplevel of ``repo_root`` (or cwd) — it
deliberately ignores PROJECT_ROOT/REBAR_ROOT (it must initialize the target repo,
not a shim's project root), matching the bash script's ``unset PROJECT_ROOT``.

Byte-parity pinned by ``tests/interfaces/test_e4_init.py``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid

_GITIGNORE = """.env-id
.closure-key
.signing-key
.state-cache
.scratch/
.cache.json
*/.cache.json
"""

_PRECOMMIT = """# No-op pre-commit config for the tickets orphan branch.
# The tickets branch carries event-sourced ticket data only — no source
# code to lint — so no hooks are needed. This empty config exists solely
# so the pre-commit framework (when installed as a pre-push hook in the
# host repo) accepts pushes from the .tickets-tracker linked worktree
# without requiring PRE_COMMIT_ALLOW_NO_CONFIG=1 on every caller.
repos: []
"""


def _git(cwd: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True)


def _git_ok(cwd: str, *args: str) -> bool:
    return _git(cwd, *args).returncode == 0


def _realpath(p: str) -> str:
    return os.path.realpath(p)


def _ensure_env_id(tracker: str) -> None:
    real = _realpath(tracker)
    if os.path.isdir(real) and not os.path.isfile(os.path.join(real, ".env-id")):
        with open(os.path.join(real, ".env-id"), "w", encoding="utf-8") as f:
            f.write(str(uuid.uuid4()) + "\n")


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


def _resolve_repo_root(repo_root) -> str | None:
    base = str(repo_root) if repo_root else "."
    cp = subprocess.run(
        ["git", "-C", base, "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
        env={**os.environ, "GIT_DISCOVERY_ACROSS_FILESYSTEM": "1"},
    )
    return cp.stdout.strip() if cp.returncode == 0 and cp.stdout.strip() else None


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
    tracker = os.path.join(repo, ".tickets-tracker")

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
                    except Exception:
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
            _ensure_env_id(tracker)
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
        _exclude(host_git, ".tickets-tracker", ".scratch/")

    # ── Init lock (mkdir, 30s) ───────────────────────────────────────────────
    lock_dir = _init_lock_dir(repo)
    if not _acquire_init_lock(lock_dir):
        sys.stderr.write("Error: could not acquire ticket-init lock within 30s\n")
        return 1
    try:
        rc = _mount_or_create_branch(repo, tracker)
        if rc != 0:
            return rc
        _ensure_branch_user_config(repo, tracker)
        _commit_gitignore(tracker)
        _exclude_scratch_in_tracker(tracker)
        _commit_precommit(tracker)
        _gen_local_files(tracker)
        _git(tracker, "config", "gc.auto", "0")
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
        return line[len("gitdir: "):] if line.startswith("gitdir: ") else ""
    return git_path


def _init_lock_dir(repo: str) -> str:
    base = os.path.join(repo, ".git")
    if os.path.isfile(base):
        with open(base, encoding="utf-8") as f:
            line = f.read().strip()
        gd = line[len("gitdir: "):] if line.startswith("gitdir: ") else base
        common = _git(gd if os.path.isdir(gd) else repo, "rev-parse", "--git-common-dir").stdout.strip()
        if common:
            base = os.path.realpath(os.path.join(gd, common)) if not os.path.isabs(common) else common
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
    local = _git_ok(repo, "rev-parse", "--verify", "tickets")
    remote = _git_ok(repo, "rev-parse", "--verify", "origin/tickets")
    if local:
        cp = _git(repo, "worktree", "add", tracker, "tickets")
        if cp.returncode != 0:
            sys.stderr.write(f"ERROR: git worktree add (local branch) failed: {cp.stderr}\n")
            return 1
        return 0
    if remote:
        _git(repo, "fetch", "origin", "tickets")
        cp = _git(repo, "worktree", "add", tracker, "tickets")
        if cp.returncode != 0:
            sys.stderr.write(f"ERROR: git worktree add (remote branch) failed: {cp.stderr}\n")
            return 1
        return 0
    # Orphan branch.
    cp = _git(repo, "worktree", "add", "--orphan", "-b", "tickets", tracker)
    if cp.returncode != 0:
        # Fallback for git < 2.40.
        cp2 = _git(repo, "worktree", "add", "--detach", tracker)
        if cp2.returncode != 0:
            sys.stderr.write(f"ERROR: git worktree add --orphan failed: {cp.stderr}\n")
            return 1
        _git(tracker, "checkout", "--orphan", "tickets")
        _git(tracker, "rm", "-rf", ".", "--quiet")
    _ensure_branch_user_config(repo, tracker)
    _git(tracker, "config", "commit.gpgsign", "false")
    _git(tracker, "config", "tag.gpgsign", "false")
    _git(tracker, "commit", "--allow-empty", "-q", "--no-verify", "-m",
         "chore: initialize ticket tracker")
    return 0


def _ensure_branch_user_config(repo: str, tracker: str) -> None:
    if _git(tracker, "config", "user.email").returncode != 0:
        email = _git(repo, "config", "user.email").stdout.strip() or "ticket-system@localhost"
        name = _git(repo, "config", "user.name").stdout.strip() or "Ticket System"
        _git(tracker, "config", "user.email", email)
        _git(tracker, "config", "user.name", name)


def _commit_gitignore(tracker: str) -> None:
    if _git(tracker, "show", "tickets:.gitignore").returncode != 0:
        with open(os.path.join(tracker, ".gitignore"), "w", encoding="utf-8") as f:
            f.write(_GITIGNORE)
        _git(tracker, "add", ".gitignore")
        _git(tracker, "commit", "-q", "--no-verify", "-m",
             "chore: add .gitignore for env-id, state-cache, scratch, and reducer cache")


def _exclude_scratch_in_tracker(tracker: str) -> None:
    git_file = os.path.join(tracker, ".git")
    git_dir = ""
    if os.path.isfile(git_file):
        with open(git_file, encoding="utf-8") as f:
            line = f.read().strip()
        gd = line[len("gitdir: "):] if line.startswith("gitdir: ") else ""
        if gd and not os.path.isabs(gd):
            gd = os.path.join(tracker, gd)
        git_dir = gd
    if not git_dir:
        return
    _exclude(git_dir, ".scratch/")


def _commit_precommit(tracker: str) -> None:
    if _git(tracker, "show", "tickets:.pre-commit-config.yaml").returncode != 0:
        with open(os.path.join(tracker, ".pre-commit-config.yaml"), "w", encoding="utf-8") as f:
            f.write(_PRECOMMIT)
        _git(tracker, "add", ".pre-commit-config.yaml")
        _git(tracker, "commit", "-q", "--no-verify", "-m",
             "chore: add no-op .pre-commit-config.yaml (bug 27d8-b230)")


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


def _init_via_symlink(repo: str, tracker: str, silent: bool) -> int:
    wl = _git(repo, "worktree", "list", "--porcelain").stdout
    main_wt = ""
    for line in wl.splitlines():
        if line.startswith("worktree "):
            main_wt = line[len("worktree "):]
            break
    if not main_wt:
        sys.stderr.write("Error: could not detect main worktree path via git worktree list\n")
        return 1
    main_tracker = os.path.join(main_wt, ".tickets-tracker")
    if not os.path.isdir(main_tracker):
        sys.stderr.write(
            "Error: Run ticket init from the main repo first, then re-run from the worktree.\n"
        )
        return 1
    if os.path.islink(tracker):
        if _realpath(tracker) == _realpath(main_tracker):
            _ensure_env_id(tracker)
            _emit("Ticket system already initialized.", silent)
            return 0
        os.remove(tracker)
    if os.path.isdir(tracker) and not os.path.islink(tracker):
        if os.path.isfile(os.path.join(tracker, ".git")):
            sys.stderr.write(
                "Error: .tickets-tracker/ is a real git worktree in this worktree "
                "checkout. Remove it manually first.\n"
            )
            return 1
        _rmtree(tracker)
    os.symlink(main_tracker, tracker)
    wt_git = _resolve_git_dir(repo)
    if wt_git:
        _exclude(wt_git, ".tickets-tracker")
    _ensure_env_id(tracker)
    _emit("Ticket system initialized (symlink to main repo).", silent)
    return 0


def init_cli(argv: list[str], *, repo_root=None) -> int:
    silent = "--silent" in argv
    return init_core(repo_root, silent=silent)
