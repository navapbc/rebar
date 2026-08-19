"""In-process ``tracker-maintenance`` — the SUPPORTED door for raw git in the tracker.

Bug 2fa6. The rule this implements is "no AD-HOC raw git in the tracker", not "no raw git
ever". The distinction matters because a hard prohibition with no sanctioned door is what
produced the incident: the store was wedged, every rebar write failed, and improvised
``git add -A`` / ``commit`` / ``merge`` / ``rm`` in the tracker was the only way out. That
improvisation is also how source files and stray state reach the tickets branch.

Most of what used to need a human is now automatic — ``event_append`` self-heals a
stranded index on paths the branch does not track, and the push recovery no longer touches
the repo-global stash stack. This command exists for what auto-heal deliberately REFUSES to
touch, and its value is the envelope around the operation rather than the operation itself:

1. **A backup ref BEFORE the first write.** ``refs/rebar-maintenance/<utc>`` is created at
   the current HEAD before anything is mutated. The predecessor of this command tagged
   ``pre-a3-remediation`` after two of four batches had already run, which made it useless
   as a rollback point — that is the specific mistake being designed out.
2. **A refusal when unpushed ticket commits exist.** ``rev-list origin/tickets..HEAD``
   being non-empty is the one condition that separates a recoverable local mess from real
   event loss, so it stops the command rather than producing a warning nobody reads.
3. **A durable audit record.** What ran, when, by whom, what it changed, and whether the
   break-glass was used — appended to the tracker's git dir, which survives the operation
   and is not itself store content that could conflict.

``--force=<reason>`` is the break-glass for the case the envelope does not cover. It
demands a written reason, is reported loudly in the output, and is recorded in the audit
line with ``forced: true`` so a reviewer can see later that it was used and why. Same
posture AGENTS.md takes for the claim/close gates: an escape hatch for a human operator's
judgment call, not a routine agent move.

Exit 0 = nothing to do / repaired; 1 = refused (unpushed commits, no ``--force``);
2 = fatal (no tracker / bad args).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time

from rebar._store.gitutil import discard_unmerged_paths, path_is_foreign_to_branch, run_git

_AUDIT_BASENAME = "rebar-maintenance-audit.jsonl"

_USAGE = """\
Usage: rebar tracker-maintenance [--status] [--clean] [--force=<reason>]

  --status          report what would change; makes NO writes (default)
  --clean           perform the repair (backup ref first, then heal)
  --force=<reason>  BREAK-GLASS: proceed even with unpushed ticket commits.
                    Requires a written reason; recorded in the audit log.

The supported alternative to ad-hoc `git` in the tickets tracker. Routine ticket
writes must go through rebar; this is for a store that rebar itself cannot write.
"""


# raw-git-ok: this IS the sanctioned maintenance surface (bug 2fa6)
def _git(tracker: str, *args: str):
    return run_git(tracker, *args, check=False)


def _unpushed_commits(tracker: str) -> int | None:
    """Count commits on HEAD not yet on ``origin/tickets``; ``None`` if unknowable.

    Unknowable is NOT treated as zero. A store whose remote-tracking ref is missing cannot
    prove its local commits are safe, so the caller refuses exactly as it would for a
    positive count — the guard is only meaningful if it fails closed."""
    if _git(tracker, "rev-parse", "--verify", "--quiet", "origin/tickets").returncode != 0:
        return None
    res = _git(tracker, "rev-list", "--count", "origin/tickets..HEAD")
    out = (res.stdout or "").strip()
    if res.returncode != 0 or not out.isdigit():
        return None
    return int(out)


def _stranded_paths(tracker: str) -> list[str]:
    """Paths left unmerged in the index (the state that blocks every ticket write)."""
    res = _git(tracker, "diff", "--name-only", "--diff-filter=U")
    if res.returncode != 0:
        return []
    return sorted(set(res.stdout.split()))


def _foreign_worktree_paths(tracker: str) -> list[str]:
    """Top-level entries that cannot be ticket data.

    Delegates to fsck's classifier so the repair acts on EXACTLY the set fsck reports — a
    second copy of the rule could drift and delete something the report never named."""
    from rebar._commands.fsck import foreign_store_path_list

    return foreign_store_path_list(tracker)


def _audit_path(tracker: str) -> str | None:
    from rebar._store.gitutil import _resolve_tracker_git_dir

    git_dir = _resolve_tracker_git_dir(tracker)
    return os.path.join(git_dir, _AUDIT_BASENAME) if git_dir else None


def _record_audit(tracker: str, record: dict) -> str | None:
    """Append one JSON line to the tracker's durable maintenance log.

    Lives in the GIT DIR, not the worktree: the log must survive the operation without
    becoming store content that a later merge could conflict on, and it must be writable
    even when the store itself is too wedged to accept an event."""
    path = _audit_path(tracker)
    if path is None:
        return None
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        return None
    return path


def _actor(tracker: str) -> str:
    """Who ran this, for the audit line.

    Prefers the configured git identity over ``$USER``: the audit record has to survive
    being read months later by someone reconstructing what happened to the store, and
    ``$USER`` is often a shared/system account (and simply unset in CI), whereas the git
    email is the same identity rebar already attributes events to."""
    from rebar import config

    return config.resolve_os_actor(tracker)


def _take_backup_ref(tracker: str) -> str | None:
    """Create the rollback ref at the CURRENT HEAD. Must precede every mutation."""
    head = _git(tracker, "rev-parse", "HEAD")
    if head.returncode != 0:
        return None
    ref = f"refs/rebar-maintenance/{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    # Creates the ROLLBACK point: the one write that must precede every other. It adds a
    # ref and destroys nothing.
    made = _git(  # raw-git-ok: sanctioned maintenance surface (2fa6)
        tracker, "update-ref", ref, head.stdout.strip()
    )
    if made.returncode != 0:
        return None
    return ref


def _report(tracker: str, stranded: list[str], foreign: list[str], unpushed: int | None) -> None:
    sys.stdout.write(f"tracker: {tracker}\n")
    unpushed_txt = "unknown (no origin/tickets)" if unpushed is None else str(unpushed)
    sys.stdout.write(f"unpushed ticket commits : {unpushed_txt}\n")
    sys.stdout.write(f"stranded index entries  : {len(stranded)}\n")
    for p in stranded:
        sys.stdout.write(f"  U {p}\n")
    sys.stdout.write(f"foreign top-level paths : {len(foreign)}\n")
    for p in foreign:
        sys.stdout.write(f"  ? {p}\n")


def _parse(argv: list[str]) -> tuple[bool, str, int | None]:
    """Return ``(clean, force_reason, early_rc)``."""
    clean = False
    force_reason = ""
    for a in argv:
        if a == "--clean":
            clean = True
        elif a == "--status":
            clean = False
        elif a.startswith("--force="):
            force_reason = a[len("--force=") :].strip()
        elif a == "--force":
            sys.stderr.write("Error: --force requires a written reason: --force=<reason>\n")
            return clean, force_reason, 2
        elif a in ("--help", "-h"):
            sys.stdout.write(_USAGE)
            return clean, force_reason, 0
        else:
            sys.stderr.write(f"Error: unknown option '{a}'\n{_USAGE}")
            return clean, force_reason, 2
    return clean, force_reason, None


def tracker_maintenance_cli(argv: list[str], *, repo_root=None) -> int:
    """``rebar tracker-maintenance`` entry."""
    from rebar import config

    clean, force_reason, early_rc = _parse(argv)
    if early_rc is not None:
        return early_rc

    tracker = str(config.tracker_dir(repo_root))
    if not os.path.isdir(tracker):
        sys.stderr.write(f"Error: tracker dir not found: {tracker}\n")
        return 2

    stranded = _stranded_paths(tracker)
    foreign = _foreign_worktree_paths(tracker)
    unpushed = _unpushed_commits(tracker)
    _report(tracker, stranded, foreign, unpushed)

    if not clean:
        sys.stdout.write("\n(--status: no changes made; re-run with --clean to repair)\n")
        return 0
    if not stranded and not foreign:
        sys.stdout.write("\nNothing to repair.\n")
        return 0

    # REFUSAL — the one condition that separates a recoverable local mess from real event
    # loss. Checked BEFORE the backup ref so a refused run makes no writes at all.
    if unpushed is None or unpushed > 0:
        detail = (
            "origin/tickets is missing, so unpushed work cannot be ruled out"
            if unpushed is None
            else f"{unpushed} ticket commit(s) on HEAD are not on origin/tickets"
        )
        if not force_reason:
            sys.stderr.write(
                f"\nRefusing to repair: {detail}.\n"
                "Repairing now could destroy events that exist nowhere else. Push them first\n"
                "(the store's own path), or re-run with --force=<reason> to break glass.\n"
            )
            return 1
        sys.stderr.write(
            "\n*** BREAK-GLASS: --force was used to override the unpushed-commits refusal ***\n"
            f"*** {detail}\n"
            f"*** reason: {force_reason}\n"
        )

    # BACKUP BEFORE THE FIRST WRITE — not mid-run. This is the rollback point.
    backup_ref = _take_backup_ref(tracker)
    if backup_ref is None:
        sys.stderr.write("Error: could not create the backup ref — refusing to mutate.\n")
        return 2
    sys.stdout.write(f"\nbackup ref: {backup_ref} (rollback: git reset --hard {backup_ref})\n")

    regenerable = [p for p in stranded if not path_is_foreign_to_branch(tracker, p)]
    stranded_foreign = [p for p in stranded if p not in regenerable]
    discard_unmerged_paths(tracker, regenerable, stranded_foreign)
    for name in foreign:
        # Unindexes a path the tickets branch does not track, which by construction
        # cannot be ticket data.
        _git(  # raw-git-ok: sanctioned maintenance surface (2fa6)
            tracker, "rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", name
        )
        full = os.path.join(tracker, name)
        try:
            if os.path.isdir(full):
                shutil.rmtree(full)
            else:
                os.remove(full)
        except OSError:
            sys.stderr.write(f"WARN: could not remove {name}\n")

    audit = _record_audit(
        tracker,
        {
            "timestamp": time.time(),
            "actor": _actor(tracker),
            "argv": argv,
            "backup_ref": backup_ref,
            "unpushed_commits": unpushed,
            "forced": bool(force_reason),
            "force_reason": force_reason,
            "stranded_paths": stranded,
            "foreign_paths": foreign,
        },
    )
    sys.stdout.write(
        f"repaired: {len(stranded)} stranded, {len(foreign)} foreign path(s) removed\n"
    )
    sys.stdout.write(f"audit: {audit or '(could not write audit log)'}\n")
    return 0
