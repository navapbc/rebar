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

import os
import re
import subprocess

from rebar._alias import compute_alias
from rebar._commands.verify_commit import extract_ticket_refs
from rebar._engine_support import field_reads
from rebar._engine_support.resolver import resolve_ticket_id
from rebar.reducer import reduce_ticket

# Watchdog on the read-only culprit-analysis git walks (bug 9305): log walks over a
# long-lived branch are legitimately slow and hold no store lock, so this is generous
# (research 5b rec 3) — it only converts a wedged filesystem into the existing
# best-effort ``None`` (the broad except below absorbs ``TimeoutExpired``).
_GIT_TIMEOUT = 300


# raw-git-ok: read-oriented git helper, variable subcommand
def _git(repo_root: str, *args: str) -> str | None:
    """Run ``git -C <repo_root> <args>`` and return stdout, or ``None`` on any failure."""
    try:
        proc = subprocess.run(
            ["git", "-C", repo_root, *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            check=False,
        )
    except Exception:  # noqa: BLE001 — best-effort: any git/OS error → no culprit
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


_HEX4_RE = re.compile(r"^[0-9a-f]{4}$")


def _is_prefix_only_form(ref: str) -> bool:
    """True iff ``ref`` is a hex-quad shape that :func:`resolve_ticket_id` can only ever
    satisfy by matching the CANONICAL id (exactly, or as a leading prefix) — never via the
    alias scan or the Jira binding store.

    Restricted to 1-, 2-, and 4-quad forms, each of which is prefix-only by the resolver's
    own control flow (``rebar._ids.resolve_ticket_id``):

    * 4 quads — matches ``_FULL_ID_RE``, which returns the exact-name lookup and returns
      before the alias scan.
    * 2 quads — matches ``_SHORT_ID_RE``, whose branch also returns before the alias scan.
    * 1 quad — falls through to the generic prefix branch; the alias scan runs first, so the
      caller additionally excludes the target's own alias (see :func:`_cannot_resolve_to`).

    3-quad forms are deliberately EXCLUDED: they reach the alias scan and a wordlist alias is
    ``adj-noun-noun``, so a hex-looking 3-quad ref could legitimately resolve by alias.
    """
    parts = ref.split("-")
    return len(parts) in (1, 2, 4) and all(_HEX4_RE.match(p) for p in parts)


def _cannot_resolve_to(ref: str, target: str, target_alias: str) -> bool:
    """True iff ``ref`` provably cannot resolve to ``target``, WITHOUT touching the store.

    A necessary-condition short-circuit, not a re-implementation of resolution: it only ever
    skips refs for which the real resolver could not have returned ``target`` anyway, so it
    changes no outcome. This is what keeps the full-history walk off the O(tickets) alias scan
    (``rebar._ids._scan_alias`` opens up to two JSON files PER ticket directory) for the bare
    4-hex commit subjects that dominate a long history.
    """
    if not _is_prefix_only_form(ref):
        return False  # alias / Jira / 3-quad shapes: let the real resolver decide
    if target_alias and ref == target_alias:
        return False  # a stored alias could take a hex shape; never skip the target's own
    return not target.startswith(ref)


def _effective_alias(target: str, tracker: str) -> str:
    """The alias the resolver's alias scan would match for ``target`` — stored if present,
    else the computed fallback (mirroring ``rebar._ids._scan_alias``). Best-effort: ``""``."""
    state: dict = {}
    path = os.path.join(tracker, target)
    if os.path.isdir(path):
        try:
            state = reduce_ticket(path) or {}
        except Exception:  # noqa: BLE001 — best-effort: an unreadable ticket is not fatal
            state = {}
    # Stored alias wins, else the computed fallback — the same precedence the resolver's
    # alias scan applies, so an unreadable ticket still yields the computable alias.
    return state.get("alias") or compute_alias(target) or ""


def _resolves_to(
    message: str, target: str, tracker: str, cache: dict[str, str], target_alias: str
) -> bool:
    """True iff any ticket ref in ``message`` resolves to the canonical id ``target``.

    ``target`` is pre-resolved by the caller (it is loop-invariant across the history walk)
    and ``cache`` memoizes ref -> resolution for the whole derivation, so a candidate that
    recurs across commits costs one lookup, not one per commit.
    """
    for ref in extract_ticket_refs(message):
        if ref == target:
            return True
        if _cannot_resolve_to(ref, target, target_alias):
            continue
        if ref not in cache:
            # quiet: these candidates are harvested from commit messages the user never
            # supplied, so an unrelated ambiguity is noise, not a diagnostic (bug
            # postwar-bardic-walleye) — the same treatment close_precheck already applies.
            cache[ref] = resolve_ticket_id(ref, tracker, quiet=True) or ref
        if cache[ref] == target:
            return True
    return False


def _find_fixing_commit(repo_root: str, bug_id: str, tracker: str) -> str | None:
    """Most recent commit whose message resolves to ``bug_id`` (the fix), else ``None``."""
    out = _git(repo_root, "log", "--format=%H%x1f%B%x1e")
    if out is None:
        return None
    target = resolve_ticket_id(bug_id, tracker, quiet=True) or bug_id
    target_alias = _effective_alias(target, tracker)
    cache: dict[str, str] = {}
    for record in out.split("\x1e"):
        record = record.strip()
        if not record or "\x1f" not in record:
            continue
        sha, message = record.split("\x1f", 1)
        if _resolves_to(message, target, tracker, cache, target_alias):
            return sha.strip()
    return None


def _blame_file_commits(repo_root: str, ref: str, path: str) -> list[str] | None:
    """Per-line introducing-commit SHAs, or ``None`` if blame could not run.

    A successful blame of an empty file returns ``[]``.  Callers must retain that
    distinction so they do not derive a culprit from only a subset of file impacts.
    """
    out = _git(repo_root, "blame", "-l", ref, "--", path)
    if out is None:
        return None
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
        # quiet: the culprit commit's own subject is likewise not a user-supplied id.
        resolved = resolve_ticket_id(ref, tracker, quiet=True)
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
        shas = _blame_file_commits(repo_root, prefix_ref, path)
        if shas is None:
            return None
        for sha in shas:
            tally[sha] = tally.get(sha, 0) + 1
            total += 1
    if total == 0:
        return None

    top_sha, top_lines = max(tally.items(), key=lambda kv: kv[1])
    if top_lines * 2 <= total:  # not a STRICT majority (> 50%)
        return None

    culprit = _commit_ticket(repo_root, top_sha, tracker)
    if culprit is None or (resolve_ticket_id(bug_id, tracker, quiet=True) or bug_id) == culprit:
        return None
    return culprit
