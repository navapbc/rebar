"""Git-ancestry and event-position resolution for authorship attestations."""

from __future__ import annotations

import os
from collections.abc import Callable


def resolve_event_commit(
    position: str, ticket_dir: str, *, repo_root: str | None = None
) -> str | None:
    """Return the oldest commit that added the event at ``position``, or ``None``.

    Because the event type is unknown, a ticket-scoped glob and full-history,
    add-only Git walk locate the file across compaction topology. Every failure
    fails closed. Bulk callers should use the ticket or store maps below because
    each globbed lookup walks the entire history."""
    if not position or not ticket_dir:
        return None
    try:
        from rebar._commands._seam import tracker_dir

        tracker = str(tracker_dir(repo_root))
        rel = os.path.relpath(ticket_dir, tracker)
        pathspec = f"{rel}/{position}-*.json"
        import rebar.attest.authorship as _authorship

        proc = _authorship.subprocess.run(
            [
                "git",
                "-c",
                "log.showSignature=false",
                "-C",
                tracker,
                "log",
                "--diff-filter=A",
                "--full-history",
                "--no-renames",
                "--format=%H",
                "--",
                pathspec,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        if proc.returncode != 0:
            return None
        lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
        return lines[-1] if lines else None
    except Exception:  # noqa: BLE001 — ANY git/lookup failure → no commit, never raise (fail-closed)
        return None


def resolve_position_commit(
    position: str, tracker: str, *, repo_root: str | None = None
) -> str | None:
    """Return the oldest commit that added globally unique ``position``, or ``None``.

    This store-wide variant uses a full-history, add-only glob for callers without
    a ticket directory. It returns the oldest match and converts every failure to
    ``None``."""
    if not position or not tracker:
        return None
    try:
        pathspec = f"*{position}-*.json"
        import rebar.attest.authorship as _authorship

        proc = _authorship.subprocess.run(
            [
                "git",
                "-c",
                "log.showSignature=false",
                "-C",
                tracker,
                "log",
                "--diff-filter=A",
                "--full-history",
                "--no-renames",
                "--format=%H",
                "--",
                pathspec,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        if proc.returncode != 0:
            return None
        lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
        return lines[-1] if lines else None
    except Exception:  # noqa: BLE001 — ANY git/lookup failure → no commit, never raise (fail-closed)
        return None


def build_ticket_position_commit_map(
    ticket_dir: str, *, repo_root: str | None = None
) -> dict[str, str]:
    """Map one ticket's event positions to oldest adding commits in one Git walk.

    The directory-scoped, full-history, add-only, no-merge/no-rename walk avoids
    one whole-history glob per signed event. Record framing recovers each
    ``{timestamp}-{uuid}``. Newest-to-oldest traversal with overwrite preserves
    oldest-add semantics. Callers fall back per event on a miss. Any scan failure
    returns ``{}``."""
    if not ticket_dir:
        return {}
    try:
        from rebar._commands._seam import tracker_dir

        tracker = str(tracker_dir(repo_root))
        rel = os.path.relpath(ticket_dir, tracker)
        import rebar.attest.authorship as _authorship

        proc = _authorship.subprocess.run(
            [
                "git",
                "-c",
                "log.showSignature=false",
                "-C",
                tracker,
                "log",
                "--diff-filter=A",
                "--full-history",
                "--no-merges",
                "--no-renames",
                "--format=%x1e%H",
                "--name-only",
                "--",
                f"{rel}/",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=300,
        )
        if proc.returncode != 0:
            return {}
        position_map: dict[str, str] = {}
        for record in proc.stdout.split("\x1e"):
            if not record.strip():
                continue
            lines = record.split("\n")
            sha = lines[0].strip()
            if len(sha) != 40:
                continue
            for path in lines[1:]:
                path = path.strip()
                if not path or not path.endswith(".json"):
                    continue
                # basename is ``{position}-{TYPE}.json``; TYPE is dash-free, so stripping
                # ``.json`` and the trailing ``-{TYPE}`` recovers the position — the exact
                # inverse of resolve_event_commit's ``<position>-*.json`` glob.
                base = os.path.basename(path)[: -len(".json")]
                position = base.rsplit("-", 1)[0] if "-" in base else base
                if position:
                    # newest→oldest walk; overwrite so the OLDEST add wins.
                    position_map[position] = sha
        return position_map
    except Exception:  # noqa: BLE001 — ANY git/lookup failure → empty map, never raise (fail-closed)
        return {}


_RETIRED_SUFFIX = ".json.retired"


def _scan_added_event_files(tracker: str) -> list[tuple[str, str]]:
    """Every ``(commit, path)`` add of an event file, newest commit first, in one Git walk.

    The full-history, add-only, no-merge/no-rename walk covers ``*.json`` and
    ``*.json.retired``. A reclaimed history adds below-checkpoint events directly as
    ``.json.retired`` (bug 84f6), so those adds are the only record of where such an event
    entered the rewritten graph. Record separators pair hashes with paths and signature
    display is disabled. Raises on a failed walk; callers convert that to an empty map."""
    import rebar.attest.authorship as _authorship

    proc = _authorship.subprocess.run(
        [
            "git",
            "-c",
            "log.showSignature=false",
            "-C",
            tracker,
            "log",
            "--diff-filter=A",
            "--full-history",
            "--no-merges",
            "--no-renames",
            "--format=%x1e%H",
            "--name-only",
            "--",
            "*.json",
            f"*{_RETIRED_SUFFIX}",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git log failed (exit {proc.returncode})")
    adds: list[tuple[str, str]] = []
    for record in proc.stdout.split("\x1e"):
        if not record.strip():
            continue
        lines = record.split("\n")
        sha = lines[0].strip()
        if len(sha) != 40:
            continue
        adds.extend((sha, path.strip()) for path in lines[1:] if path.strip())
    return adds


def _position_of(path: str) -> str:
    """The ``{timestamp}-{uuid}`` position of an event file path, or ``""``.

    The basename is ``{position}-{TYPE}.json`` (optionally ``.retired``) and TYPE is dash-free,
    so dropping the suffix and the trailing ``-{TYPE}`` recovers the position — the inverse of
    the ``<position>-*.json`` globs used by the per-event resolvers."""
    base = os.path.basename(path)
    if base.endswith(_RETIRED_SUFFIX):
        base = base[: -len(_RETIRED_SUFFIX)]
    elif base.endswith(".json"):
        base = base[: -len(".json")]
    else:
        return ""
    return base.rsplit("-", 1)[0] if "-" in base else base


def build_event_commit_maps(
    *, repo_root: str | None = None
) -> tuple[dict[str, str], dict[str, str]]:
    """``(path_map, position_map)`` from ONE add-walk of the tracker history.

    ``path_map`` keys each ``*.json`` event path to its oldest adding commit (the batched form
    of :func:`resolve_event_commit`). ``position_map`` keys each event position to the oldest
    commit that added it as ``.json`` or ``.json.retired``. On ordinary history an event's
    ``.json`` add predates the ``.retired`` add its compaction makes, so the oldest add is
    unchanged; on a reclaimed history the checkpoint's ``.retired`` add is the only one. The
    walk runs newest to oldest and overwrites, so the OLDEST add wins. Any scan failure
    returns two empty maps (callers fall back per event, fail-closed)."""
    try:
        from rebar._commands._seam import tracker_dir

        adds = _scan_added_event_files(str(tracker_dir(repo_root)))
    except Exception:  # noqa: BLE001 — ANY git/lookup failure → empty maps, never raise (fail-closed)
        return {}, {}
    path_map: dict[str, str] = {}
    position_map: dict[str, str] = {}
    for sha, path in adds:
        if path.endswith(".json"):
            path_map[path] = sha
        position = _position_of(path)
        if position:
            position_map[position] = sha
    return path_map, position_map


def build_introducing_commit_map(*, repo_root: str | None = None) -> dict[str, str]:
    """Map tracker ``*.json`` event paths to oldest adding commits in one Git walk.

    See :func:`build_event_commit_maps`; callers needing both maps should call it once. Callers
    use the fail-closed resolver for missing paths. Any scan failure returns ``{}``."""
    return build_event_commit_maps(repo_root=repo_root)[0]


def build_position_commit_map(*, repo_root: str | None = None) -> dict[str, str]:
    """Map all event positions to oldest adding commits in one Git walk.

    See :func:`build_event_commit_maps`; ``.json.retired`` adds count, so a reclaimed history's
    below-checkpoint positions resolve to its checkpoint. Callers fall back per position on a
    miss. Any scan failure returns ``{}``."""
    return build_event_commit_maps(repo_root=repo_root)[1]


def _subprocess_is_ancestor(tracker: str, ancestor: str, descendant: str) -> bool:
    """``git merge-base --is-ancestor``; any git failure is "not an ancestor" (fail-closed)."""
    import rebar.attest.authorship as _authorship

    try:
        run = _authorship.subprocess.run(
            ["git", "-C", tracker, "merge-base", "--is-ancestor", ancestor, descendant],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
    except Exception:  # noqa: BLE001 — ANY git failure → not an ancestor (fail-closed)
        return False
    return run.returncode == 0


class _AncestryOracle:
    """Ancestry over one parsed ``rev-list --parents`` graph; see :func:`build_ancestry_oracle`."""

    def __init__(self, tracker: str, rev_list_parents: str) -> None:
        self._tracker = tracker
        self._index: dict[str, int] = {}
        parents: list[list[str]] = []
        for line in rev_list_parents.splitlines():
            shas = line.split()
            if shas:
                self._index[shas[0]] = len(parents)
                parents.append(shas[1:])
        self._children: list[list[int]] = [[] for _ in parents]
        for child, plist in enumerate(parents):
            for parent in plist:
                pi = self._index.get(parent)
                if pi is not None:
                    self._children[pi].append(child)
        self._descendants: dict[int, bytearray] = {}

    def _descendants_of(self, start: int) -> bytearray:
        seen = self._descendants.get(start)
        if seen is not None:
            return seen
        seen = bytearray(len(self._children))
        seen[start] = 1
        stack = [start]
        while stack:
            for child in self._children[stack.pop()]:
                if not seen[child]:
                    seen[child] = 1
                    stack.append(child)
        self._descendants[start] = seen
        return seen

    def __call__(self, ancestor: str, descendant: str) -> bool:
        di = self._index.get(descendant)
        if di is None:
            return _subprocess_is_ancestor(self._tracker, ancestor, descendant)
        ai = self._index.get(ancestor)
        if ai is None:
            # A full SHA outside the graph cannot be an ancestor of a commit inside it; any
            # other spelling (an abbreviation or ref name) is left to git to resolve.
            if len(ancestor) == 40:
                return False
            return _subprocess_is_ancestor(self._tracker, ancestor, descendant)
        return bool(self._descendants_of(ai)[di])


def build_ancestry_oracle(tracker: str) -> Callable[[str, str], bool] | None:
    """An in-memory ``is_ancestor(ancestor, descendant)`` over the tracker's HEAD history.

    Reads ``git rev-list --parents HEAD`` once, then answers exactly as
    ``git merge-base --is-ancestor`` does: each distinct ancestor's descendant set is computed
    once and reused, so era checks for many events against a few keyring commits cost one
    graph walk per key instead of one subprocess per check (bug 84f6). Every ancestor of a
    commit in the graph is in the graph, so an ancestor absent from it is never an ancestor of
    such a commit. A descendant absent from the graph is answered by the subprocess. Returns
    ``None`` when the graph cannot be read; callers then keep their subprocess path."""
    import rebar.attest.authorship as _authorship

    try:
        proc = _authorship.subprocess.run(
            ["git", "-C", tracker, "rev-list", "--parents", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=300,
        )
    except Exception:  # noqa: BLE001 — ANY git failure → no oracle, callers use the subprocess
        return None
    if proc.returncode != 0:
        return None
    return _AncestryOracle(tracker, proc.stdout)
