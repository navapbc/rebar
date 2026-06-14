"""In-process ``purge-bridge`` (Tier E E5).

Ports the bash ``ticket-purge-bridge.sh``: remove Jira-sourced tickets (``jira-*``
prefix, materialized by the reconciler's inbound applier) whose CREATE-event Jira
project key does NOT match ``--keep``. Native / migrated tickets (non-``jira-*``)
are never touched. After deletion the removal is committed on the tickets branch.

Byte-parity with the dispatcher arm (verified empirically):

* missing ``--keep`` → ``Error: --keep=<PROJECT_KEY> is required`` exit 1.
* unknown arg → ``Usage: ticket-purge-bridge.sh --keep=<PROJECT_KEY> [--dry-run]``
  exit 1.
* the ``Scanning…`` line, the three-line ``Results:`` block, then ``Nothing to
  delete.`` / ``[DRY RUN] Would delete N…`` / the deletion + commit narration.

The project key is ``jira_key.split('-')[0]`` (empty when the CREATE event has no
hyphenated ``jira_key`` → counted as *skip*). Pinned by
``tests/interfaces/test_e5_purge_bridge.py``.

One intentional divergence from the bash arm: the bash ``git commit`` ran WITHOUT
``-q``, so it leaked git's plumbing summary (``[tickets <hash>] …``, file list) —
nondeterministic and not part of the contract. Like the E3 ``delete`` port, this
impl suppresses that chatter; the human narration lines match byte-for-byte.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

from rebar import config

_USAGE = "Usage: ticket-purge-bridge.sh --keep=<PROJECT_KEY> [--dry-run]"


def _project_key(ticket_dir: str) -> str:
    """Jira project key from the first CREATE event; '' on miss/no-hyphen/error."""
    creates = sorted(
        p.path for p in os.scandir(ticket_dir) if p.name.endswith("-CREATE.json")
    )
    if not creates:
        return ""
    try:
        with open(creates[0], encoding="utf-8") as fh:
            ev = json.load(fh)
        jira_key = ev.get("data", {}).get("jira_key", "")
        return jira_key.split("-")[0] if "-" in jira_key else ""
    except Exception:
        return ""


def purge_bridge_cli(argv: list[str], *, repo_root=None) -> int:
    """``rebar purge-bridge --keep=<KEY> [--dry-run]`` entry."""
    keep_project = ""
    dry_run = False
    for arg in argv:
        if arg.startswith("--keep="):
            keep_project = arg[len("--keep="):]
        elif arg == "--dry-run":
            dry_run = True
        else:
            sys.stderr.write(_USAGE + "\n")
            return 1

    if not keep_project:
        sys.stderr.write("Error: --keep=<PROJECT_KEY> is required\n")
        return 1

    tracker = str(config.tracker_dir(repo_root))
    if not os.path.isdir(tracker):
        sys.stderr.write(f"Error: tracker directory not found at {tracker}\n")
        return 1

    sys.stdout.write(
        f"Scanning for non-{keep_project} Jira-sourced tickets (jira-* prefix)...\n"
    )

    delete_list: list[str] = []
    keep_count = skip_count = 0
    for entry in sorted(os.scandir(tracker), key=lambda e: e.name):
        if not (entry.is_dir() and entry.name.startswith("jira-")):
            continue
        project_key = _project_key(entry.path)
        if not project_key:
            skip_count += 1
        elif project_key == keep_project:
            keep_count += 1
        else:
            delete_list.append(entry.path)

    delete_count = len(delete_list)
    sys.stdout.write("Results:\n")
    sys.stdout.write(f"  Keep ({keep_project}): {keep_count}\n")
    sys.stdout.write(f"  Delete (non-{keep_project}): {delete_count}\n")
    sys.stdout.write(f"  Skip (no project key): {skip_count}\n")

    if delete_count == 0:
        sys.stdout.write("Nothing to delete.\n")
        return 0

    if dry_run:
        sys.stdout.write(f"[DRY RUN] Would delete {delete_count} ticket directories.\n")
        return 0

    sys.stdout.write(f"Deleting {delete_count} ticket directories...\n")
    deleted = 0
    for ticket_dir in delete_list:
        shutil.rmtree(ticket_dir, ignore_errors=True)
        deleted += 1
        if deleted % 500 == 0:
            sys.stdout.write(f"  Deleted {deleted} / {delete_count}...\n")
    sys.stdout.write(f"Deleted {deleted} ticket directories.\n")

    _commit_deletion(tracker, deleted, keep_project)
    sys.stdout.write("Done.\n")
    return 0


def _commit_deletion(tracker: str, deleted: int, keep_project: str) -> None:
    """Commit the removal on the tickets branch (best-effort, parity with bash).

    Mirrors ``git add -A && git commit --no-verify || echo 'Nothing to commit'``:
    a failed commit (nothing staged) prints ``Nothing to commit`` rather than
    erroring, since the deleted dirs may have been untracked.
    """
    in_worktree = subprocess.run(
        ["git", "-C", tracker, "rev-parse", "--git-dir"],
        capture_output=True, text=True,
    ).returncode == 0
    if not in_worktree:
        return
    sys.stdout.write("Committing deletion on tickets branch...\n")
    subprocess.run(["git", "-C", tracker, "add", "-A"], capture_output=True, text=True)
    cp = subprocess.run(
        ["git", "-C", tracker, "commit", "--no-verify", "-m",
         f"purge: remove {deleted} non-{keep_project} Jira-sourced (jira-*) tickets"],
        capture_output=True, text=True,
    )
    if cp.returncode != 0:
        sys.stdout.write("Nothing to commit\n")
