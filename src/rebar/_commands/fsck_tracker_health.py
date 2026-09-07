"""Run tracker-wide ``fsck`` checks through ``_tracker_health``.

Checks cover origin divergence, configured branch mismatch, foreign store paths, environment
identity mismatch, and dirty tracker state. Divergence, pollution, identity mismatch, tracked
deletions, and regenerable leftovers are counted. A pending push, branch mismatch, and
temporary event staging files are informational. ``fsck_scan._scan`` is the sole caller.
"""

from __future__ import annotations

import os
import shlex
import subprocess

from rebar import config
from rebar._store import env_identity
from rebar._store.gitutil import path_is_foreign_to_branch, run_git
from rebar.reducer._cache import RETIRED_SUFFIX

# Watchdog on fsck's read-only local git calls (bug 9305): NOT a latency budget — these
# are sub-second rev-parse/log/symbolic-ref reads, so 120s only distinguishes a wedged
# filesystem/lock from slowness (deliberately not copied from the 30s/300s network values).
_FSCK_GIT_TIMEOUT = 120


def _tracker_health(tracker: str, repo_root=None, authorship=None) -> tuple[list[str], int]:
    """Return tracker-level report lines and their counted issue total.

    A pending push and branch mismatch are informational. Origin divergence, foreign paths,
    environment identity divergence, and counted dirty-tree classes increment the total.
    Dirty state can contribute one line per class through :func:`_dirty_tracker_lines`.
    """
    lines: list[str] = []
    issues = 0
    pairs = authorship.identity_pairs() if authorship is not None else set()
    sync_line, sync_is_issue = _tracker_sync_status(tracker)
    # NOTE: any pair added here with ``is_issue=False`` (e.g. a new WARN/informational
    # check) MUST also be reflected in fsck._NEVER_COUNTED_KINDS so the JSON ``issue_count``
    # stays in agreement with this exit-code tally — see that constant's drift guard.
    for line, is_issue in (
        (sync_line, sync_is_issue),
        (_branch_mismatch(tracker, repo_root), False),
        (_foreign_store_paths(tracker), True),
        (env_identity.divergence_report(env_identity.read_env_id(tracker), pairs), True),
        *_dirty_tracker_lines(tracker),
    ):
        if not line:
            continue
        lines.append(line)
        issues += int(is_issue)
    return lines, issues


def _branch_mismatch(tracker: str, repo_root=None) -> str | None:
    """Informational WARN when the tracker worktree's actually-checked-out branch
    differs from the configured ``tracker.branch``. 'configured' = the precedence-
    resolved config (from ``repo_root`` when known, else the CODE repo root discovered
    the config way — ``rebar.toml`` lives in the checkout, NOT beside a relocated store);
    'mounted' = the branch the worktree has checked out. This catches a
    ``tracker.branch`` changed in project config AFTER init: the store is NOT
    auto-migrated, so it stays on the old branch. Best-effort: skip on a malformed
    config or a detached/unreadable HEAD."""
    root = repo_root if repo_root is not None else config.repo_root_or_none()
    try:
        configured = config.tickets_branch(root)
    except config.ConfigError:
        return None
    try:
        cp = subprocess.run(
            ["git", "-C", tracker, "symbolic-ref", "--quiet", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=_FSCK_GIT_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None  # watchdog (9305): a hung fs yields the best-effort no-report path
    mounted = cp.stdout.strip()
    if cp.returncode != 0 or not mounted or mounted == configured:
        return None  # detached/unreadable, or a match — nothing to report
    return (
        f"WARN: configured tracker.branch '{configured}' does not match the mounted "
        f"branch '{mounted}' — the store was initialized on '{mounted}' and is NOT "
        "auto-migrated. Revert the config, or re-init on the new branch."
    )


def foreign_store_path_list(tracker: str) -> list[str]:
    """List top-level entries that cannot be ticket data.

    This shared classifier drives both ``fsck`` reporting and ``tracker-maintenance`` repair.
    Ticket directories contain an active or retired event file. Dot-prefixed store artifacts
    are excluded.
    """

    def _holds_events(path: str) -> bool:
        try:
            return any(
                n.endswith(".json") or n.endswith(RETIRED_SUFFIX)
                for n in os.listdir(path)
                if not n.startswith(".")
            )
        except OSError:
            return False

    try:
        entries = sorted(os.listdir(tracker))
    except OSError:
        return []
    return [
        n for n in entries if not n.startswith(".") and not _holds_events(os.path.join(tracker, n))
    ]


def _foreign_store_paths(tracker: str) -> str | None:
    """Report top-level tracker entries that cannot be ticket data.

    The tickets branch may contain ticket directories and dot-prefixed store artifacts, but
    not source paths. Classification is structural because ticket directory names need not
    resemble ticket IDs. Tracked foreign entries are identified separately because they will
    propagate with the branch.
    """

    strays = foreign_store_path_list(tracker)
    if not strays:
        return None
    committed = [n for n in strays if not path_is_foreign_to_branch(tracker, n)]
    shown = ", ".join(strays[:10]) + (" …" if len(strays) > 10 else "")
    detail = (
        f" {len(committed)} of them are COMMITTED to the tickets branch "
        f"({', '.join(committed[:10])}) and will propagate on the next push."
        if committed
        else " None are committed — they are working-tree strays."
    )
    return (
        f"FOREIGN_STORE_PATH: the tickets tracker holds {len(strays)} top-level "
        f"path(s) that are not ticket data: {shown}.{detail} The store must be mutated "
        "through rebar, never by raw git or a stash applied in the tracker worktree."
    )


def _tracker_sync_status(tracker: str) -> tuple[str | None, bool]:
    """Classify the local tracker against ``<remote>/<branch>`` and return
    ``(line, is_issue)``. Mirrors the divergence taxonomy in ``_store/sync.py``:

    * no common ancestor (unrelated histories) → ``DIVERGED`` **issue**;
    * common ancestor but neither side is an ancestor of the other → ``DIVERGED``
      **issue** (a non-fast-forwardable divergence — the local store will never push);
    * remote is an ancestor of HEAD and HEAD is ahead → ``PUSH_PENDING`` informational;
    * HEAD is an ancestor of remote (local merely behind) → nothing (sync ff-adopts).

    Best-effort: a malformed config or an absent remote/remote-ref yields no report
    rather than a crash.
    """

    # raw-git-ok: store-maintenance command, seam-internal
    def _git(*args: str) -> subprocess.CompletedProcess:
        try:
            return run_git(tracker, *args, check=False, timeout=_FSCK_GIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            # Watchdog, not a latency budget (9305): a hung fs must not hang fsck.
            return subprocess.CompletedProcess(
                ["git", "-C", tracker, *args],
                124,
                "",
                f"git timed out after {_FSCK_GIT_TIMEOUT}s",
            )

    # tickets.branch / tickets.remote are CODE-repo config (rebar.toml lives in the checkout,
    # not beside a relocated store) — resolve the code root the config way, NOT from the
    # tracker's parent, which holds no rebar.toml on a relocated store.
    try:
        base = config.repo_root_or_none()
        branch = config.tickets_branch(base)
        remote = config.tickets_remote(base)
    except config.ConfigError:
        return None, False
    remote_ref = f"{remote}/{branch}"
    if _git("remote", "get-url", remote).returncode != 0:
        return None, False
    if _git("rev-parse", "--verify", remote_ref).returncode != 0:
        return None, False

    diverged = (
        f"DIVERGED: local '{branch}' branch has diverged from {remote_ref} — no "
        "shared history / cannot fast-forward. The local store was built independently "
        "of the remote (e.g. init could not fetch the existing branch), so it hides "
        "remote tickets and its writes will never push. Recover: run `rebar "
        "fsck-recover`, or re-clone and re-init"
    )

    # Unrelated histories: no common ancestor at all.
    if _git("merge-base", "HEAD", remote_ref).returncode != 0:
        return diverged, True
    # Remote is an ancestor of HEAD → local is ahead (or level): benign push-pending.
    if _git("merge-base", "--is-ancestor", remote_ref, "HEAD").returncode == 0:
        cp = _git("rev-list", f"{remote_ref}..HEAD", "--count")
        try:
            ahead = int((cp.stdout or "0").strip() or "0")
        except ValueError:
            ahead = 0
        if ahead > 0:
            # PUSH_PENDING is is_issue=False (informational): mirrored in
            # fsck._NEVER_COUNTED_KINDS so the JSON ``issue_count`` excludes it too.
            return (
                f"PUSH_PENDING: local '{branch}' branch is ahead of {remote_ref} by "
                f"{ahead} commit(s) — push pending (run a ticket write to retry the "
                "push, or check connectivity to origin)",
                False,
            )
        return None, False
    # HEAD is an ancestor of remote → local merely behind; sync will ff-adopt.
    if _git("merge-base", "--is-ancestor", "HEAD", remote_ref).returncode == 0:
        return None, False
    # Common ancestor, but neither side is an ancestor of the other → true divergence.
    return diverged, True


# ── check 4.10: the dirty-tracker wedge class (ticket c925-7669-ded8-43a3) ──────────────

_TMP_EVENT_PREFIX = ".tmp-event-"

# (classes key, finding kind, blurb, counted-issue?). Classes 1 and 2 are counted:
# each wedges reconverge until healed (`rebar doctor --repair`). Class 3 is
# informational — an in-flight append legitimately holds a live ``.tmp-event-*`` for a
# moment, so counting it would make fsck flake against concurrent writers; it is
# reported for MANUAL triage and never auto-touched.
_DIRTY_LINE_SPECS: tuple[tuple[str, str, str, bool], ...] = (
    (
        "deletions",
        "TRACKER_DIRTY_DELETION",
        "tracked store file(s) deleted in the working tree; bytes intact at HEAD "
        "(heal: rebar doctor --repair restores them)",
        True,
    ),
    (
        "leftovers",
        "TRACKER_DIRTY_LEFTOVER",
        "untracked regenerable compaction leftover(s) "
        "(heal: rebar doctor --repair quarantines them — moved, never deleted)",
        True,
    ),
    (
        "tmp_events",
        "TRACKER_DIRTY_TMP_EVENT",
        "orphaned event staging file(s) — never auto-touched; triage manually",
        False,
    ),
)


def dirty_tracker_classes(tracker: str) -> dict[str, list[str]]:
    """Classify porcelain status into sorted deletions, leftovers, and temporary events.

    Deletions are tracked files removed from the worktree or index. Leftovers are untracked
    snapshots and retired files whose source is already folded. Temporary ``.tmp-event-*``
    files are report-only because an append may own them. This classifier is shared by
    ``fsck`` and ``doctor --repair``.

    ``--untracked-files=all`` preserves files inside untracked directories. NUL-delimited
    porcelain preserves path bytes and permits skipping original rename records. A failed or
    hung status command returns empty classes.
    """
    empty: dict[str, list[str]] = {"deletions": [], "leftovers": [], "tmp_events": []}
    try:
        cp = run_git(
            tracker,
            "status",
            "--porcelain",
            "-z",
            "--untracked-files=all",
            check=False,
            timeout=_FSCK_GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return empty  # watchdog (9305): a hung fs yields the best-effort no-report path
    if cp.returncode != 0:
        return empty
    classes = empty
    entries = iter(cp.stdout.split("\0"))
    for entry in entries:
        if len(entry) < 4:
            continue
        state, rel = entry[:2], entry[3:]
        if state[0] in "RC":
            next(entries, None)  # the rename/copy source path — a separate NUL record
        name = os.path.basename(rel)
        if state in (" D", "D "):
            classes["deletions"].append(rel)
        elif state == "??" and name.startswith(_TMP_EVENT_PREFIX):
            classes["tmp_events"].append(rel)
        elif state == "??" and name.endswith("-SNAPSHOT.json"):
            classes["leftovers"].append(rel)
        elif (
            state == "??" and name.endswith(RETIRED_SUFFIX) and _retired_source_folded(tracker, rel)
        ):
            classes["leftovers"].append(rel)
    return {key: sorted(paths) for key, paths in classes.items()}


def _retired_source_folded(tracker: str, rel: str) -> bool:
    """Is the untracked ``*.retired`` at *rel* a regenerable leftover — i.e. are its
    retired-source's bytes already preserved in a COMMIT?

    True when the source path (``rel`` minus ``.retired``) still exists at HEAD (a
    crashed local fold: the rename's deletion side is restorable, so the stray adds
    nothing), or when the configured remote branch already carries this same ``.retired``
    path (a peer's fold committed it; the local stray is a duplicate that blocks the
    union merge). Anything else could be the only copy of an event, so it is NOT
    classified — quarantining it might orphan event bytes."""
    source = rel[: -len(RETIRED_SUFFIX)]
    if _exists_in_ref(tracker, "HEAD", source):
        return True
    remote_ref = _configured_remote_ref()
    return remote_ref is not None and _exists_in_ref(tracker, remote_ref, rel)


def _exists_in_ref(tracker: str, ref: str, rel: str) -> bool:
    try:
        cp = run_git(
            tracker, "cat-file", "-e", f"{ref}:{rel}", check=False, timeout=_FSCK_GIT_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        return False
    return cp.returncode == 0


def _configured_remote_ref() -> str | None:
    """``<remote>/<branch>`` from the CODE repo config (rebar.toml in the checkout, resolved
    the config way — NOT the tracker's parent), or None on a malformed config — matching
    :func:`_tracker_sync_status`."""
    base = config.repo_root_or_none()
    try:
        return f"{config.tickets_remote(base)}/{config.tickets_branch(base)}"
    except config.ConfigError:
        return None


def _dirty_tracker_lines(tracker: str) -> list[tuple[str, bool]]:
    """The dirty-tracker findings as fsck ``(line, is_issue)`` pairs, one per non-empty
    class. The line shape ``KIND: <n> path(s): <paths> — <detail>`` is a contract:
    ``fsck._transform_json`` parses the count and paths back out of it so text and JSON
    can never drift. Paths are ``shlex.quote``d (identity for ordinary store paths) so
    a path containing spaces survives the round-trip."""
    classes = dirty_tracker_classes(tracker)
    lines: list[tuple[str, bool]] = []
    for key, kind, blurb, is_issue in _DIRTY_LINE_SPECS:
        paths = classes[key]
        if paths:
            joined = " ".join(shlex.quote(p) for p in paths)
            lines.append((f"{kind}: {len(paths)} path(s): {joined} — {blurb}", is_issue))
    return lines
