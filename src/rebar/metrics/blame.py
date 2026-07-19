"""Blame-derived single-culprit resolver for the bug-close ``caused_by`` link (ticket 555e).

On a bug close with NO explicit ``--caused-by`` override, :func:`derive_caused_by`
best-effort points at the change/ticket that most likely INTRODUCED the bug:

1. Find the FIXING commit — the most recent ``git log`` commit whose message resolves
   (via :func:`rebar._commands.verify_commit.extract_ticket_refs`) to THIS bug id.
2. Blame the PRE-fix tree (``<fixing-commit>~1``) for each file in the bug's recorded
   ``file_impact`` — never the post-fix HEAD, which would blame the fix itself.
3. Tally blamed lines per introducing commit across those files; if a STRICT MAJORITY
   (> 50%) belong to ONE commit AND that commit's message resolves to a ticket, return
   that ticket id. Otherwise (ambiguous / no dominant culprit / no file_impact / no
   resolvable trailer) return ``None``.

Everything is best-effort: any git error, a missing fixing commit, or an unresolvable
culprit returns ``None`` so the caller never blocks or fails the close.
"""

from __future__ import annotations

import subprocess

from rebar._commands.verify_commit import extract_ticket_refs
from rebar._engine_support import field_reads
from rebar._engine_support.resolver import resolve_ticket_id


def _git(repo_root: str, *args: str) -> str | None:
    """Run ``git -C <repo_root> <args>`` and return stdout, or ``None`` on any failure."""
    try:
        proc = subprocess.run(
            ["git", "-C", repo_root, *args],
            capture_output=True,
            text=True,
        )
    except Exception:  # noqa: BLE001 — best-effort: any git/OS error → no culprit
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _resolves_to(message: str, bug_id: str, tracker: str) -> bool:
    """True iff any ticket ref in ``message`` resolves to the same canonical id as ``bug_id``."""
    target = resolve_ticket_id(bug_id, tracker) or bug_id
    for ref in extract_ticket_refs(message):
        if (resolve_ticket_id(ref, tracker) or ref) == target:
            return True
    return False


def _find_fixing_commit(repo_root: str, bug_id: str, tracker: str) -> str | None:
    """Most recent commit whose message resolves to ``bug_id`` (the fix), else ``None``."""
    out = _git(repo_root, "log", "--format=%H%x1f%B%x1e")
    if out is None:
        return None
    for record in out.split("\x1e"):
        record = record.strip()
        if not record or "\x1f" not in record:
            continue
        sha, message = record.split("\x1f", 1)
        if _resolves_to(message, bug_id, tracker):
            return sha.strip()
    return None


def _blame_file_commits(repo_root: str, ref: str, path: str) -> list[str]:
    """Per-line introducing-commit SHAs for ``path`` at ``ref`` (``git blame -l``)."""
    out = _git(repo_root, "blame", "-l", ref, "--", path)
    if out is None:
        return []
    shas: list[str] = []
    for line in out.splitlines():
        if not line:
            continue
        tok = line.lstrip("^").split(" ", 1)[0]
        if tok:
            shas.append(tok)
    return shas


def _commit_ticket(repo_root: str, sha: str, tracker: str) -> str | None:
    """Resolve the culprit commit's message to a ticket id (the introduced-by ticket)."""
    msg = _git(repo_root, "log", "-1", "--format=%B", sha)
    if msg is None:
        return None
    for ref in extract_ticket_refs(msg):
        resolved = resolve_ticket_id(ref, tracker)
        if resolved is not None:
            return resolved
    return None


def derive_caused_by(bug_id: str, repo_root: str, tracker: str) -> str | None:
    """Best-effort single-culprit ticket id for ``bug_id``, or ``None`` (see module docstring)."""
    fixing = _find_fixing_commit(repo_root, bug_id, tracker)
    if not fixing:
        return None

    impacts = field_reads.file_impact(bug_id, tracker)
    paths = [p for entry in impacts if (p := (entry or {}).get("path"))]
    if not paths:
        return None

    tally: dict[str, int] = {}
    total = 0
    prefix_ref = f"{fixing}~1"
    for path in paths:
        for sha in _blame_file_commits(repo_root, prefix_ref, path):
            tally[sha] = tally.get(sha, 0) + 1
            total += 1
    if total == 0:
        return None

    top_sha, top_lines = max(tally.items(), key=lambda kv: kv[1])
    if top_lines * 2 <= total:  # not a STRICT majority (> 50%)
        return None

    culprit = _commit_ticket(repo_root, top_sha, tracker)
    if culprit is None or (resolve_ticket_id(bug_id, tracker) or bug_id) == culprit:
        return None
    return culprit
