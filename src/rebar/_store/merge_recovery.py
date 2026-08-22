"""One merge-abort recovery toolkit for the tickets store.

Two callers reconverge the tickets branch by merging the remote ref, and BOTH have to
survive the same handful of recoverable merge aborts: :mod:`rebar._store.sync`
(``reconverge``, the read/write freshness path) and :mod:`rebar._store.push_recovery`
(the non-fast-forward push retry). Their merge CONTROL FLOWS are genuinely different —
sync unions, handles unrelated histories, restores from HEAD and warns; push_recovery
sets the dirty tree aside as a stash COMMIT, rides out transport faults and raises
strict-mode reason codes — so they are deliberately NOT fused into one merge function.
What they share is the recovery TOOLKIT, and that is what lives here:

* parsing the paths git names in an abort message (:func:`untracked_overwrite_paths`,
  :func:`local_change_paths`);
* relocating those paths into the durable quarantine (:func:`quarantine_untracked`);
* restoring tracked files git says have local changes (:func:`restore_local_changes`).

Before this module the parser and the quarantine PATH arithmetic lived in ``sync`` and
``push_recovery`` reached sideways for them, while the quarantine MOVER was written
TWICE — and the two copies had already drifted: only push_recovery's verified that a
named path is genuinely UNTRACKED (``??``) before moving it. Bugs
``small-delicious-loris`` (6ccc) and ``sulfuryl-suicidal-osprey`` (573a) are that one
defect landing in the two paths one at a time. The single mover here keeps the STRICTER
shape, so the fence now covers every caller.

**The git seam is a parameter, not an import.** Every function that shells out takes
``git`` — any ``(path, *args) -> CompletedProcess`` callable. That is what lets both
callers keep their own seam: ``sync`` hands its module-level ``_git`` (patched by name in
its tests), and ``push_recovery`` hands the late-bound ``core._git`` that the ~25
``push._git`` monkeypatch sites depend on. Neither loses its patch point, and this module
imports from neither.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

#: Any ``(path, *args) -> CompletedProcess`` git runner. Supplied by the caller so this
#: module never binds a seam that a caller's tests need to intercept.
GitRunner = Callable[..., subprocess.CompletedProcess]

# Lines that terminate git's indented path list inside an abort message. "Please …"
# covers both "Please move or remove them" (untracked) and "Please commit your
# changes or stash them" (local changes); ort appends "Merge with strategy … failed."
_ABORT_TRAILER_PREFIXES = ("Please ", "Aborting", "Merge with strategy", "error:", "fatal:")

_UNTRACKED_MARKER = "untracked working tree files would be overwritten by merge"
_LOCAL_CHANGE_MARKER = "Your local changes to the following files would be overwritten by merge"


def abort_named_paths(merge: subprocess.CompletedProcess, marker: str) -> list[str]:
    """The repo-relative paths git names under ``marker`` in a merge-abort message (one
    per indented line between the marker line and the trailer) — or ``[]`` if the marker
    is absent, which fences recovery to exactly the recoverable case."""
    combined = f"{merge.stdout or ''}\n{merge.stderr or ''}"
    lines = combined.splitlines()
    start = next((i for i, line in enumerate(lines) if marker in line), None)
    if start is None:
        return []
    paths: list[str] = []
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(_ABORT_TRAILER_PREFIXES):
            break
        paths.append(stripped)
    return paths


def untracked_overwrite_paths(merge: subprocess.CompletedProcess) -> list[str]:
    """Recovery variant (a): untracked local files origin wants to create."""
    return abort_named_paths(merge, _UNTRACKED_MARKER)


def local_change_paths(merge: subprocess.CompletedProcess) -> list[str]:
    """Recovery variant (b): tracked local files with uncommitted changes origin
    wants to touch."""
    return abort_named_paths(merge, _LOCAL_CHANGE_MARKER)


def quarantine_dir_under(common: str, tracker: str) -> Path:
    """Pure path computation for the shared quarantine dir (no subprocess): resolve a
    ``--git-common-dir`` answer (relative ones resolve against the tracker root) to a
    fresh ``<common>/reconverge-quarantine/<utc-ts>/``. The quarantine sits OUTSIDE any
    working tree and is the durable safety copy; it is never pruned."""
    common_dir = Path(common)
    if not common_dir.is_absolute():
        common_dir = Path(tracker) / common_dir
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return common_dir.resolve() / "reconverge-quarantine" / stamp


def quarantine_dir(git: GitRunner, tracker: str) -> Path | None:
    """A fresh timestamped quarantine dir for this tracker, or ``None`` when git cannot
    name the common dir — a quarantine path computed from ``''`` would land INSIDE the
    working tree. Materialized lazily by the first write."""
    common = git(tracker, "rev-parse", "--git-common-dir").stdout.strip()
    if not common:
        return None
    return quarantine_dir_under(common, tracker)


def quarantine_untracked(git: GitRunner, tracker: str, paths: list[str]) -> bool:
    """Relocate (move, never delete) each named UNTRACKED path into quarantine so it
    cannot re-collide on a merge retry.

    Three fences, all of which answer ``False`` with nothing moved, because a
    mis-identified file must stay exactly where it is:

    * git cannot name the common dir;
    * a named path is not genuinely untracked (``??``) — checked for ALL paths BEFORE
      any move, so a mis-parse can never relocate TRACKED data;
    * a path vanished between the status check and the move (a concurrent writer).

    Returns ``True`` only when every path was moved.
    """
    common = git(tracker, "rev-parse", "--git-common-dir").stdout.strip()
    if not common:
        return False
    for rel in paths:
        status = git(tracker, "status", "--porcelain", "-uall", "--", rel).stdout
        if not status.startswith("??"):
            return False
    quarantine = quarantine_dir_under(common, tracker)
    tracker_root = Path(tracker)
    for rel in paths:
        src = tracker_root / rel
        if not src.exists():
            return False
        dest = quarantine / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
    return True


def restore_local_changes(git: GitRunner, tracker: str, paths: list[str]) -> bool:
    """Non-destructively clear the local changes git named, per porcelain state:
    a DELETION of a tracked file — worktree (`` D``) or staged (``D ``, what an
    interrupted compaction fold leaves after its ``git add -A``) — is restored from
    HEAD (the bytes are already committed, nothing can be lost); a worktree
    MODIFICATION (`` M``) is first copied (never moved) into quarantine, then
    restored. Any other state — a staged modification, a conflict, an unparsed
    line — answers False so the caller keeps the abort-only net."""
    quarantine = quarantine_dir(git, tracker)
    if quarantine is None:
        return False
    tracker_root = Path(tracker)
    for rel in paths:
        state = git(tracker, "status", "--porcelain", "--", rel).stdout[:2]
        if state == " M":
            dest = quarantine / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(tracker_root / rel), str(dest))
        elif state not in (" D", "D "):
            return False
        if git(tracker, "checkout", "HEAD", "--", rel).returncode != 0:
            return False
    return True
