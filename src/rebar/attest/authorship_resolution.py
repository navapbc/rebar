"""Git-ancestry and event-position resolution for authorship attestations."""

from __future__ import annotations

import os


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


def build_introducing_commit_map(*, repo_root: str | None = None) -> dict[str, str]:
    """Map tracker event paths to oldest adding commits in one Git walk.

    A full-history, add-only, no-merge/no-rename scan uses record separators to
    pair hashes with paths and disables signature display. Streaming newest to
    oldest with overwrite matches per-event oldest-add semantics. Callers use the
    fail-closed resolver for missing paths. Any scan failure returns ``{}``.
    """
    try:
        from rebar._commands._seam import tracker_dir

        tracker = str(tracker_dir(repo_root))
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
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if proc.returncode != 0:
            return {}
        commit_map: dict[str, str] = {}
        for record in proc.stdout.split("\x1e"):
            if not record.strip():
                continue
            lines = record.split("\n")
            sha = lines[0].strip()
            if len(sha) != 40:
                continue
            for path in lines[1:]:
                path = path.strip()
                if path:
                    # newest→oldest walk; overwrite so the OLDEST add wins (matches
                    # resolve_event_commit's lines[-1]).
                    commit_map[path] = sha
        return commit_map
    except Exception:  # noqa: BLE001 — ANY git/lookup failure → empty map, never raise (fail-closed)
        return {}


def build_position_commit_map(*, repo_root: str | None = None) -> dict[str, str]:
    """Map all event positions to oldest adding commits in one Git walk.

    This sibling of :func:`build_introducing_commit_map` strips the dash-free
    event type from each filename. The same full-history, add-only,
    no-merge/no-rename traversal and newest-to-oldest overwrite preserve
    oldest-add semantics. Callers fall back per position on a miss. Any scan
    failure returns ``{}``.
    """
    try:
        from rebar._commands._seam import tracker_dir

        tracker = str(tracker_dir(repo_root))
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
            ],
            capture_output=True,
            text=True,
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
                # basename is ``{position}-{TYPE}.json``; TYPE is dash-free, so stripping ``.json``
                # and the trailing ``-{TYPE}`` recovers the ``{timestamp}-{uuid}`` position.
                base = os.path.basename(path)[: -len(".json")]
                position = base.rsplit("-", 1)[0] if "-" in base else base
                if position:
                    # newest→oldest walk; overwrite so the OLDEST add wins (matches
                    # resolve_position_commit's lines[-1]).
                    position_map[position] = sha
        return position_map
    except Exception:  # noqa: BLE001 — ANY git/lookup failure → empty map, never raise (fail-closed)
        return {}
