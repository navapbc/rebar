"""Tracker-LEVEL fsck checks — the store as a whole, not ticket by ticket.

Checks 4.5–4.7 and 4.9, extracted from ``fsck.py`` (which fused four concerns and sat at the
800-LOC hard cap), plus check 4.10 (the dirty-tracker wedge class). They belong together
because they share a shape the per-ticket validators in ``fsck_scan`` do not: each inspects
the tracker as a whole through git/config, and each decides for itself whether its lines are
counted integrity issues or informational. ``_tracker_health`` is the single entry point;
``fsck_scan._scan`` is its only caller.

* 4.5 tracker-vs-origin sync status — PUSH_PENDING informational, DIVERGED a counted issue;
* 4.6 configured-vs-mounted ``tracker.branch`` — informational;
* 4.7 FOREIGN_STORE_PATH — source paths polluting the tracker (bug 2fa6), a counted issue;
* 4.9 ENV_ID_MISMATCH — environment-identity divergence, a counted issue;
* 4.10 TRACKER_DIRTY_* — the dirty working-tree wedge class (ticket c925-7669-ded8-43a3):
  tracked deletions restorable from HEAD and untracked regenerable compaction leftovers are
  counted issues (``rebar doctor --repair`` heals them); orphaned ``.tmp-event-*`` staging
  files are informational (report-only, never auto-touched).
"""

from __future__ import annotations

import os
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
    """The four TRACKER-level checks (4.5–4.7, 4.9), as ``(lines, issue_count)``.

    Grouped out of ``_scan`` because they share a shape the per-ticket checks do not:
    each inspects the tracker as a whole, each yields at most one line, and each decides
    for itself whether that line is a counted integrity issue or informational. Keeping
    them inline grew ``_scan`` — already the largest branch cluster in this module — for
    every check added.

    * 4.5 tracker-vs-origin: PUSH_PENDING informational, DIVERGED a counted issue;
    * 4.6 configured-vs-mounted ``tracker.branch``: informational;
    * 4.7 source paths polluting the store (bug 2fa6): a counted issue, because
      ``origin/tickets`` holds no source tree, so any such path means something wrote to
      the store outside the event-append path;
    * 4.9 environment-identity divergence (bug gold-distinct-lacewing): a counted issue —
      a re-clone that dropped ``.env-id`` silently orphaned its own attestations;
    * 4.10 the dirty-tracker wedge class (ticket c925-7669-ded8-43a3): up to three lines,
      one per class — see :func:`_dirty_tracker_lines`.
    """
    lines: list[str] = []
    issues = 0
    pairs = authorship.identity_pairs() if authorship is not None else set()
    sync_line, sync_is_issue = _tracker_sync_status(tracker)
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
    resolved config (from ``repo_root`` when known, else the MAIN repo = the tracker's
    parent); 'mounted' = the branch the worktree has checked out. This catches a
    ``tracker.branch`` changed in project config AFTER init: the store is NOT
    auto-migrated, so it stays on the old branch. Best-effort: skip on a malformed
    config or a detached/unreadable HEAD."""
    root = repo_root if repo_root is not None else os.path.dirname(os.path.realpath(tracker))
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
    """Top-level tracker entries that cannot be ticket data, as a plain list.

    THE single classifier for "is this path store pollution?" — :func:`_foreign_store_paths`
    renders it for fsck's report and ``tracker-maintenance`` acts on it. Two copies of the
    rule would be free to drift, and a repair that disagreed with the report it was shown
    could delete something fsck never named.

    A top-level entry is ticket data iff it is a directory holding at least one event file
    (active or ``*.retired``); store artifacts all begin with a dot and are skipped."""

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
    """Report top-level tracker entries that cannot be ticket data (bug 2fa6).

    ``origin/tickets`` legitimately holds NOTHING but ticket directories and the store's
    own dotfiles, so a ``src/``, ``tests/`` or ``.rebar/…`` path in the tracker is
    pollution — the signature of raw git run in the store, or of a foreign ``git stash``
    applied there. The push recovery now HEALS such a path when it strands the index; this
    check exists so the condition is also REPORTED, because silent healing would hide the
    fact that something is writing source files into the store.

    Classification is deliberately structural rather than a filename denylist, and it uses
    the same "is this a ticket?" test as the rest of fsck: a top-level entry is ticket data
    if it is a directory holding at least one event file. Matching on the ticket-id SHAPE
    instead would be wrong — ticket directories are not required to be id-shaped, and doing
    so reports healthy stores as polluted. Store artifacts (``.git``, ``.bridge_state``,
    ``.env-id``, ``.opcert-key``…) all begin with a dot and are skipped. Entries the branch
    actually TRACKS are called out separately: those were committed into the tickets branch
    and will propagate on the next push, which is strictly worse than a working-tree stray."""

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

    # Branch + remote resolved from the MAIN repo config (the tracker's parent).
    try:
        base = os.path.dirname(os.path.realpath(tracker))
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
    """Classify the tracker's ``git status --porcelain`` into the dirty-tree wedge classes.

    THE single classifier — fsck renders it (:func:`_dirty_tracker_lines`) and
    ``doctor --repair`` acts on it (same one-rule discipline as
    :func:`foreign_store_path_list`). Returns ``{"deletions", "leftovers", "tmp_events"}``,
    each a sorted list of tracker-relative paths:

    * ``deletions`` — tracked files deleted in the worktree (`` D``) or index (``D ``).
      The tickets branch holds nothing but store data, so every such path is a store
      artifact whose bytes are intact at HEAD (the P0 wedge: 119 tracked deletions left
      by an interrupted compaction fold).
    * ``leftovers`` — untracked (``??``) regenerable compaction leftovers: any
      ``*-SNAPSHOT.json`` (a snapshot is derived state by definition), and a ``*.retired``
      only when its retired-source is already folded (:func:`_retired_source_folded`) —
      otherwise the stray could be the only copy of an event and stays unclassified.
    * ``tmp_events`` — orphaned ``.tmp-event-*`` staging files (``_store/staging``'s
      mkstemp prefix). Report-only: a live one belongs to an in-flight append.

    ``--untracked-files=all`` is load-bearing: without it git collapses an untracked
    directory to one ``dir/`` entry and the leftover files inside are never named.
    Best-effort: a failed/hung ``git status`` classifies nothing.
    """
    empty: dict[str, list[str]] = {"deletions": [], "leftovers": [], "tmp_events": []}
    try:
        cp = run_git(
            tracker,
            "status",
            "--porcelain",
            "--untracked-files=all",
            check=False,
            timeout=_FSCK_GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return empty  # watchdog (9305): a hung fs yields the best-effort no-report path
    if cp.returncode != 0:
        return empty
    classes = empty
    for line in cp.stdout.splitlines():
        if len(line) < 4:
            continue
        state, rel = line[:2], line[3:]
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
    remote_ref = _configured_remote_ref(tracker)
    return remote_ref is not None and _exists_in_ref(tracker, remote_ref, rel)


def _exists_in_ref(tracker: str, ref: str, rel: str) -> bool:
    try:
        cp = run_git(
            tracker, "cat-file", "-e", f"{ref}:{rel}", check=False, timeout=_FSCK_GIT_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        return False
    return cp.returncode == 0


def _configured_remote_ref(tracker: str) -> str | None:
    """``<remote>/<branch>`` resolved from the MAIN repo config (the tracker's parent),
    or None on a malformed config — matching :func:`_tracker_sync_status`."""
    base = os.path.dirname(os.path.realpath(tracker))
    try:
        return f"{config.tickets_remote(base)}/{config.tickets_branch(base)}"
    except config.ConfigError:
        return None


def _dirty_tracker_lines(tracker: str) -> list[tuple[str, bool]]:
    """The dirty-tracker findings as fsck ``(line, is_issue)`` pairs, one per non-empty
    class. The line shape ``KIND: <n> path(s): <paths> — <detail>`` is a contract:
    ``fsck._transform_json`` parses the count and paths back out of it so text and JSON
    can never drift."""
    classes = dirty_tracker_classes(tracker)
    lines: list[tuple[str, bool]] = []
    for key, kind, blurb, is_issue in _DIRTY_LINE_SPECS:
        paths = classes[key]
        if paths:
            lines.append((f"{kind}: {len(paths)} path(s): {' '.join(paths)} — {blurb}", is_issue))
    return lines
