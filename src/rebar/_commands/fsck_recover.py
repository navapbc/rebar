"""In-process ``fsck-recover`` — destructive tracker recovery (Tier E E4).

Ports ticket-fsck-recover.sh (bug 637b). Detects a paused rebase/merge in the
tracker worktree (rebase-merge / rebase-apply / REBASE_HEAD / MERGE_HEAD), tries
``git rebase --continue`` (with timeout), and on failure aborts + cherry-picks
dangling ``ticket: <EVENT> <id>`` commits in chronological order.

DESTRUCTIVE: modifies tracker git state (unlike fsck). Flags: --tracker-dir,
--detect-only, --recover-dangling, --timeout, --help. Exit 0 = nothing to do /
recovered; 1 = attempted but nothing recovered; 2 = fatal (no tracker / bad args);
3 = stale detected with --detect-only. Byte-parity pinned by
``tests/interfaces/test_e4_fsck_recover.py``.
"""

from __future__ import annotations

import os
import subprocess
import sys

from rebar._commands.fsck import _resolve_tracker_git_dir
from rebar._store.gitutil import run_git

_TICKET_COMMIT_RE = (
    r"^ticket: (CREATE|STATUS|COMMENT|LINK|UNLINK|EDIT|FILE_IMPACT|VERIFY_COMMANDS|"
    r"ARCHIVED|SYNC|SNAPSHOT|REVERT|COMPACT|DELETE|TAG|UNTAG)"
)

_USAGE = """ticket-fsck-recover.sh
Destructive recovery for ticket-tracker stale-rebase state (bug 637b-63fe-9d44-4aab).

Detects paused rebase state in the ticket-tracker git worktree using the
correct modern-git markers (rebase-merge/ directory and rebase-apply/
directory, in addition to the legacy REBASE_HEAD file), then attempts to
drain the rebase via `git rebase --continue` with a timeout, falling back to
`git rebase --abort` and cherry-picking dangling commits that match the
ticket commit message pattern.

IMPORTANT — this script IS destructive. It modifies git state in the tracker
directory. The companion script ticket-fsck.sh remains strictly
non-destructive. Use this script ONLY when ticket-tracker corruption from
the stale-rebase bug has been confirmed (e.g., `rebar show <id>` returns
"no events" for tickets that were recently created or modified).

Usage:
  ticket-fsck-recover.sh [--tracker-dir <path>] [--detect-only]
                         [--recover-dangling] [--timeout <seconds>]
                         [--help]

Flags:
  --tracker-dir <path>   Path to the tracker worktree (default: $REPO_ROOT/.tickets-tracker)
  --detect-only          Report stale rebase state and exit; do not attempt recovery
  --recover-dangling     Skip the --continue step; only run the dangling-commit
                         cherry-pick recovery (use this when --continue has already
                         failed and you want to re-attempt the cherry-pick phase)
  --timeout <seconds>    Timeout for `git rebase --continue` (default: 30)
  --help                 Print usage and exit 0
"""


def _git(tracker: str, *args: str, **kw) -> subprocess.CompletedProcess:
    return run_git(tracker, *args, check=False, **kw)


def _detect_stale_rebase(git_dir: str) -> str:
    if os.path.isdir(os.path.join(git_dir, "rebase-merge")):
        return "rebase-merge"
    if os.path.isdir(os.path.join(git_dir, "rebase-apply")):
        return "rebase-apply"
    if os.path.isfile(os.path.join(git_dir, "REBASE_HEAD")):
        return "REBASE_HEAD"
    if os.path.isfile(os.path.join(git_dir, "MERGE_HEAD")):
        return "MERGE_HEAD"
    return ""


def fsck_recover_cli(argv: list[str], *, repo_root=None) -> int:
    tracker_dir = ""
    detect_only = False
    recover_dangling = False
    continue_timeout = 30

    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--tracker-dir":
            tracker_dir = argv[i + 1] if i + 1 < len(argv) else ""
            i += 2
        elif a.startswith("--tracker-dir="):
            tracker_dir = a[len("--tracker-dir=") :]
            i += 1
        elif a == "--detect-only":
            detect_only = True
            i += 1
        elif a == "--recover-dangling":
            recover_dangling = True
            i += 1
        elif a == "--timeout":
            continue_timeout = int(argv[i + 1]) if i + 1 < len(argv) else 30
            i += 2
        elif a.startswith("--timeout="):
            continue_timeout = int(a[len("--timeout=") :])
            i += 1
        elif a in ("--help", "-h"):
            sys.stdout.write(_USAGE)
            return 0
        else:
            sys.stderr.write(f"Error: unknown argument '{a}'\n")
            sys.stderr.write(_USAGE)
            return 2

    if not tracker_dir:
        # Default: config-resolved tracker (REBAR_TRACKER_DIR > repo_root >
        # REBAR_ROOT > git toplevel) so the library's repo_root and the
        # dispatcher's tracker-dir override translation both work. In production
        # (cwd=repo, no env override) this equals the bash git-toplevel resolution.
        from rebar import config

        tracker_dir = str(config.tracker_dir(repo_root))

    if not os.path.isdir(tracker_dir):
        sys.stderr.write(
            f"Error: tracker dir '{tracker_dir}' does not exist or is not a directory\n"
        )
        return 2

    # Story 21dd: fsck-recover drives raw git (rebase/merge --continue) on the store
    # WITHOUT the write lock, so the lock.acquire() gate never fires here — gate it
    # explicitly, failing closed on an incompatible store before any git mutation.
    # `--detect-only` is a read-only diagnostic (no git mutation), so it stays available
    # under an incompatible record, mirroring the fsck diagnostic read-allowance.
    if not detect_only:
        from rebar._store.compat import StoreIncompatibleError, check_store_compat

        try:
            check_store_compat(tracker_dir)
        except StoreIncompatibleError as exc:
            sys.stderr.write(str(exc) + "\n")
            return exc.returncode

    git_dir = _resolve_tracker_git_dir(tracker_dir)
    if not git_dir:
        sys.stderr.write(f"Error: could not resolve git directory for tracker '{tracker_dir}'\n")
        return 2

    rebase_kind = _detect_stale_rebase(git_dir)

    if detect_only:
        if not rebase_kind:
            sys.stdout.write(
                f"No stale rebase or merge state detected in tracker '{tracker_dir}'\n"
            )
            return 0
        sys.stdout.write(
            f"Stale rebase detected: marker_kind={rebase_kind} tracker='{tracker_dir}' "
            f"gitdir='{git_dir}'\n"
        )
        if rebase_kind == "rebase-merge":
            msgnum_p = os.path.join(git_dir, "rebase-merge", "msgnum")
            end_p = os.path.join(git_dir, "rebase-merge", "end")
            if os.path.isfile(msgnum_p) and os.path.isfile(end_p):
                msgnum = _read_or(msgnum_p, "?")
                end = _read_or(end_p, "?")
                sys.stdout.write(f"  Progress: {msgnum} / {end} picks completed\n")
        return 3

    if not rebase_kind and not recover_dangling:
        sys.stdout.write("No stale rebase or merge state detected — nothing to recover\n")
        return 0

    recovered_count = 0

    # ── Attempt rebase --continue (skip when --recover-dangling) ─────────────
    if rebase_kind and not recover_dangling:
        if rebase_kind in ("rebase-merge", "rebase-apply", "REBASE_HEAD"):
            sys.stdout.write(
                f"Stale rebase detected ({rebase_kind}); attempting 'git rebase "
                f"--continue' with timeout={continue_timeout}s\n"
            )
            try:
                cp = _git(
                    tracker_dir,
                    "-c",
                    "rebase.autostash=true",
                    "rebase",
                    "--continue",
                    timeout=continue_timeout,
                )
                continue_exit = cp.returncode
            except subprocess.TimeoutExpired:
                continue_exit = 124
            if continue_exit == 0:
                if not _detect_stale_rebase(git_dir):
                    sys.stdout.write("Recovery successful: rebase drained via --continue\n")
                    return 0
                after = _detect_stale_rebase(git_dir)
                sys.stdout.write(
                    f"WARN: rebase --continue exited 0 but rebase state still present "
                    f"(marker={after}); falling back to abort + cherry-pick\n"
                )
            else:
                sys.stdout.write(
                    f"WARN: rebase --continue failed (exit={continue_exit}); falling back "
                    "to abort + cherry-pick of dangling commits\n"
                )
        elif rebase_kind == "MERGE_HEAD":
            sys.stdout.write("Stale merge state detected; aborting merge\n")
            _git(tracker_dir, "merge", "--abort")

    # ── Abort remaining state to enable cherry-pick ──────────────────────────
    final_kind = _detect_stale_rebase(git_dir)
    if final_kind in ("rebase-merge", "rebase-apply", "REBASE_HEAD"):
        sys.stdout.write("Aborting rebase to enable cherry-pick recovery\n")
        _git(tracker_dir, "rebase", "--abort")
    elif final_kind == "MERGE_HEAD":
        sys.stdout.write("Aborting merge to enable cherry-pick recovery\n")
        _git(tracker_dir, "merge", "--abort")

    # ── Find dangling ticket commits ─────────────────────────────────────────
    import re

    sys.stdout.write("Scanning for dangling ticket commits to cherry-pick\n")
    fsck_out = _git(tracker_dir, "fsck", "--no-reflogs").stdout or ""
    pat = re.compile(_TICKET_COMMIT_RE)
    dangling: list[str] = []
    for line in fsck_out.splitlines():
        if line.startswith("dangling commit "):
            sha = line[len("dangling commit ") :]
            subject = _git(tracker_dir, "log", "-1", "--format=%s", sha).stdout.strip()
            if pat.search(subject):
                dangling.append(sha)

    if not dangling:
        sys.stdout.write("No dangling ticket commits found\n")
        if rebase_kind and recovered_count == 0:
            return 1
        return 0

    # Sort by committer date (chronological).
    dated = []
    for sha in dangling:
        cd = _git(tracker_dir, "log", "-1", "--format=%ct %H", sha).stdout.strip()
        if cd:
            dated.append(cd)
    sorted_shas = [line.split()[1] for line in sorted(dated, key=lambda x: int(x.split()[0]))]

    sys.stdout.write(
        f"Found {len(sorted_shas)} dangling ticket commits — cherry-picking in "
        "chronological order\n"
    )
    for sha in sorted_shas:
        cp = _git(
            tracker_dir,
            "cherry-pick",
            "--allow-empty",
            "--strategy=recursive",
            "-X",
            "theirs",
            sha,
        )
        short = _git(tracker_dir, "rev-parse", "--short", sha).stdout.strip()
        if cp.returncode == 0:
            subject = _git(tracker_dir, "log", "-1", "--format=%s", sha).stdout.strip()
            sys.stdout.write(f"  cherry-picked {short}: {subject}\n")
            recovered_count += 1
        else:
            _git(tracker_dir, "cherry-pick", "--abort")
            sys.stdout.write(
                f"  skipped {short} (cherry-pick conflict — manual recovery required)\n"
            )

    sys.stdout.write(f"Recovery complete: {recovered_count} commits cherry-picked\n")
    if recovered_count == 0 and rebase_kind:
        return 1
    return 0


def _read_or(path: str, default: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return default
